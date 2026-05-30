"""
Background poller — every `scan_interval_s` seconds (operator-configurable
from the AI-History page), for each watched camera:

  1. Ask Frigate if a person is currently in frame.
     - GET /api/events?cameras=<cam>&label=person&in_progress=1&limit=5
  2. If yes, fetch a fresh snapshot pair:
       full frame → /api/<cam>/latest.jpg?h=720
       person crop → /api/<cam>/person/snapshot.jpg?crop=1&h=320
  3. Save both JPEGs + append a synthetic ledger row.

The "scan every N seconds and snapshot the current state" model is
deliberately different from Frigate's event model. Frigate emits ONE
event per person-presence (start_time → end_time), so a cursor-based
poller would only save the first frame of a long presence. Periodic
scans give the operator a continuous timeline of activity, one row per
scan, with the snapshot at that instant.

Lifecycle:
  * `ensure_running()` is idempotent — called from FastAPI lifespan
    + from every relevant endpoint. Safe to call eagerly.
  * The loop self-pauses (no Frigate calls) when no cameras are
    selected OR when `state.frigate_url` is unset.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from ...core.state import state
from . import kitchen_ai_history as history

logger = logging.getLogger("demo_restaurant.kitchen_ai_history_task")

HTTP_TIMEOUT_S = 8.0
IDLE_POLL_S    = 5.0    # how long to wait before re-checking config
                         # when no cameras are selected / Frigate is down

_TASK: Optional[asyncio.Task] = None


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------

def ensure_running() -> None:
    """Start the background poller if it isn't already running."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _TASK = loop.create_task(_run_forever(), name="ai_history_poller")
    logger.info("kitchen AI-history poller started")


def is_running() -> bool:
    return _TASK is not None and not _TASK.done()


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

async def _run_forever() -> None:
    # Tiny startup delay so we don't race FastAPI's lifespan.
    await asyncio.sleep(2.0)
    while True:
        try:
            ran = await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI-history poller tick failed")
            ran = False
        # Sleep for the configured scan interval. If nothing actually
        # ran (no cameras selected or Frigate down) wait IDLE_POLL_S
        # instead so the operator's first-camera-selection is picked
        # up promptly.
        delay = history.get_scan_interval() if ran else IDLE_POLL_S
        await asyncio.sleep(delay)


async def _tick() -> bool:
    """Returns True if we tried any Frigate calls this tick."""
    if not state.frigate_url:
        return False
    cams = history.get_selected_cameras()
    if not cams:
        return False
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        for cam in cams:
            try:
                await _scan_camera(cam, client)
            except Exception:
                logger.exception("AI-history scan of %s failed", cam)
    return True


async def _scan_camera(camera: str, client: httpx.AsyncClient) -> None:
    """One scan tick for one camera: check person presence, save a full
    frame + one crop per detected person."""
    # 1) Persons in frame right now? With 3 persons, Frigate emits 3
    # separate in_progress events (one per tracked object), each with
    # its own bbox.
    events = await _current_person_events(camera, client)
    if not events:
        return

    base = state.frigate_url

    # 2) Full frame — always-fresh stream snapshot, no overlay.
    full_bytes = await _fetch_bytes(
        client, f"{base}/api/{camera}/latest.jpg", params={"h": 720},
    )
    if not full_bytes:
        # Stream hiccup — try again next tick.
        return

    # 3) Person crop. Single union image that includes every detected
    # person — operator confirmed this is the desired layout. Use the
    # union of all event bboxes if any are available; otherwise fall
    # back to the single highest-score event's crop, finally the full
    # frame so we never store a black image.
    events_sorted = sorted(
        events, key=lambda e: max(_event_score(e)), reverse=True,
    )
    boxes = [b for b in (_event_box(e) for e in events_sorted) if b is not None]

    crop_bytes = b""
    if boxes:
        crop_bytes = _crop_union_of_boxes(full_bytes, boxes)
    if not _looks_like_jpeg(crop_bytes) and events_sorted:
        primary_id = events_sorted[0].get("id")
        if primary_id:
            crop_bytes = await _fetch_bytes(
                client, f"{base}/api/events/{primary_id}/snapshot.jpg",
                params={"h": 320, "crop": 1, "quality": 85},
            )
            if not _looks_like_jpeg(crop_bytes):
                crop_bytes = await _fetch_bytes(
                    client, f"{base}/api/events/{primary_id}/thumbnail.jpg",
                    params={"h": 320},
                )
    if not _looks_like_jpeg(crop_bytes):
        crop_bytes = full_bytes

    # 4) Score extraction. Frigate 0.13+ moved score / top_score under
    # `data` — read both shapes and take the max across all events so
    # the badge reflects the most confident detection in frame.
    score, top_score = 0.0, 0.0
    for ev in events_sorted:
        s, ts = _event_score(ev)
        if s  > score:     score = s
        if ts > top_score: top_score = ts
    if top_score < score: top_score = score   # defensive

    # 5) Persist. Each scan gets a unique synthetic id so successive
    # captures of the SAME Frigate event still produce distinct rows.
    now = time.time()
    eid = f"scan-{camera}-{int(now * 1000)}"

    history.image_path(eid, "full").write_bytes(full_bytes)
    history.image_path(eid, "crop").write_bytes(crop_bytes)

    primary = events_sorted[0] if events_sorted else {}
    history.append_event({
        "id":            eid,
        "camera":        camera,
        "label":         "person",
        "score":         round(score, 3),
        "top_score":     round(top_score, 3),
        "start_time":    now,
        "end_time":      None,
        "box":           _union_box(boxes),
        "frigate_id":    primary.get("id"),
        "person_count":  len(events_sorted),
        "person_boxes":  boxes,
    })


async def _current_person_events(camera: str, client: httpx.AsyncClient) -> list[dict]:
    """List the person events Frigate considers "currently visible" on
    this camera.

    Frigate caveat: when a person stops moving, Frigate's stationary
    detection kicks in (~10s at default settings) and the event is
    marked ended even though Frigate keeps drawing the bbox. So a room
    with two seated people often shows 2 bboxes on the live feed but
    only 0–1 in_progress events at any given moment.

    To get an accurate count we therefore widen the window AND merge
    two parallel queries:

      1. `in_progress=1` → events actively being tracked right now
      2. `after=now-180` → every event from the last 3 minutes,
                          including ones that ended due to stationarity
                          but whose person may still be in frame

    Then we keep anything that ended within the configured `freshness`
    window (default 30s) — enough to bridge typical stationary gaps.
    Dedup is by event id."""
    url = f"{state.frigate_url}/api/events"
    queries = [
        {"cameras": camera, "label": "person",
          "in_progress": 1, "limit": 50, "include_thumbnails": 0},
        {"cameras": camera, "label": "person",
          "after": time.time() - 180, "limit": 100, "include_thumbnails": 0},
    ]

    merged: dict[str, dict] = {}
    for params in queries:
        try:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                logger.debug("Frigate /api/events returned %d for %s (params=%s)",
                              r.status_code, camera, params)
                continue
            body = r.json()
            if not isinstance(body, list):
                continue
        except httpx.HTTPError as e:
            logger.debug("person-presence check for %s failed: %s", camera, e)
            continue
        for ev in body:
            if not isinstance(ev, dict): continue
            eid = ev.get("id")
            if not eid: continue
            # Prefer the version with the freshest end_time / score data.
            if eid in merged:
                # Take whichever has end_time = None (still active).
                if merged[eid].get("end_time") is None: continue
            merged[eid] = ev

    now = time.time()
    # Freshness window: a stationary event might have ended seconds ago
    # but the person is still on-screen. 30s covers Frigate's default
    # stationary.threshold without making us hold on to ghosts.
    freshness_s = 30.0
    out: list[dict] = []
    for ev in merged.values():
        if (ev.get("label") or "").lower() != "person":
            continue
        if ev.get("false_positive"):
            continue
        end_t = ev.get("end_time")
        if end_t is None:
            out.append(ev)
            continue
        try:
            if (now - float(end_t)) <= freshness_s:
                out.append(ev)
        except Exception:
            continue
    return out


async def _fetch_bytes(client: httpx.AsyncClient, url: str,
                        params: Optional[dict] = None) -> bytes:
    try:
        r = await client.get(url, params=params or {})
        if r.status_code != 200:
            return b""
        return r.content
    except httpx.HTTPError as e:
        logger.debug("AI-history fetch %s failed: %s", url, e)
        return b""


# ----------------------------------------------------------------------
# Helpers — JPEG sniff + best-effort full-frame crop using PIL if it's
# installed. Without PIL we just return empty so the caller falls back
# to using the full frame as the crop.
# ----------------------------------------------------------------------

def _looks_like_jpeg(b: bytes) -> bool:
    # Real JPEGs start with the SOI marker 0xFF 0xD8. Frigate-served
    # snapshots that are "not ready yet" tend to be sub-1KB HTML or
    # empty; this sniff rules them out cleanly.
    return bool(b) and len(b) > 1024 and b[:2] == b"\xff\xd8"


def _crop_from_full(full_jpeg: bytes, box: Optional[list]) -> bytes:
    """Best-effort: crop the full frame to `box` (Frigate gives
    [x, y, w, h] in source pixels OR normalised 0..1 — handle both).
    Returns empty bytes if Pillow isn't available or the crop fails."""
    if not full_jpeg or not box or len(box) < 4:
        return b""
    try:
        from PIL import Image  # type: ignore
        import io
    except Exception:
        return b""
    try:
        img = Image.open(io.BytesIO(full_jpeg))
        w, h = img.size
        x, y, bw, bh = (float(v) for v in box[:4])
        # Detect normalised coordinates (all ≤ 1.0).
        if max(x, y, bw, bh) <= 1.0:
            x, y, bw, bh = x * w, y * h, bw * w, bh * h
        # Add ~10% padding so the crop isn't tight to the bbox.
        pad_x, pad_y = bw * 0.1, bh * 0.1
        left   = max(0, int(x - pad_x))
        top    = max(0, int(y - pad_y))
        right  = min(w, int(x + bw + pad_x))
        bottom = min(h, int(y + bh + pad_y))
        if right - left < 4 or bottom - top < 4:
            return b""
        cropped = img.crop((left, top, right, bottom))
        # Cap height at 320 for storage.
        if cropped.height > 320:
            ratio = 320 / cropped.height
            cropped = cropped.resize(
                (int(cropped.width * ratio), 320), Image.LANCZOS,
            )
        buf = io.BytesIO()
        cropped.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.debug("AI-history bbox crop failed: %s", e)
        return b""


# ----------------------------------------------------------------------
# Frigate schema helpers — robust against 0.13+ where score / box moved
# under a nested `data` dict, while still supporting older deployments
# that kept them at the top level.
# ----------------------------------------------------------------------

def _event_score(ev: dict) -> tuple[float, float]:
    """Return (score, top_score) for one event, walking both schemas.
    Top_score falls back to score if absent — for an in-progress
    event Frigate sometimes only fills one of them."""
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    s   = ev.get("score")     if ev.get("score")     is not None else data.get("score")
    ts  = ev.get("top_score") if ev.get("top_score") is not None else data.get("top_score")
    try: s_f  = float(s)  if s  is not None else 0.0
    except Exception: s_f  = 0.0
    try: ts_f = float(ts) if ts is not None else s_f
    except Exception: ts_f = s_f
    return s_f, ts_f


def _event_box(ev: dict) -> Optional[list]:
    """Pick the event's bounding box. Frigate 0.13+ tends to put it
    under data.box; older versions keep it at the top level. Returns
    a four-element [x, y, w, h] list or None."""
    candidates = []
    if isinstance(ev.get("data"), dict):
        candidates.append(ev["data"].get("box"))
    candidates.append(ev.get("box"))
    for b in candidates:
        if isinstance(b, list) and len(b) >= 4:
            try:
                return [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            except Exception:
                continue
    return None


def _union_box(boxes: list[list]) -> Optional[list]:
    """Smallest rectangle that contains every box. Boxes are [x,y,w,h]."""
    if not boxes:
        return None
    lefts   = [b[0]          for b in boxes]
    tops    = [b[1]          for b in boxes]
    rights  = [b[0] + b[2]   for b in boxes]
    bottoms = [b[1] + b[3]   for b in boxes]
    left, top    = min(lefts),  min(tops)
    right, bot   = max(rights), max(bottoms)
    return [left, top, right - left, bot - top]


def _crop_union_of_boxes(full_jpeg: bytes, boxes: list[list]) -> bytes:
    """Crop the full frame to the smallest rectangle that contains every
    person's bounding box, with ~12% padding. Single-person cases
    collapse naturally to a tight crop around that one person."""
    union = _union_box(boxes)
    if union is None:
        return b""
    if not full_jpeg:
        return b""
    try:
        from PIL import Image  # type: ignore
        import io
    except Exception:
        return b""
    try:
        img = Image.open(io.BytesIO(full_jpeg))
        w, h = img.size
        x, y, bw, bh = union
        # Frigate sometimes ships normalised coordinates (0..1).
        if max(x, y, bw, bh) <= 1.0:
            x, y, bw, bh = x * w, y * h, bw * w, bh * h
        pad_x, pad_y = bw * 0.12, bh * 0.12
        left   = max(0, int(x - pad_x))
        top    = max(0, int(y - pad_y))
        right  = min(w, int(x + bw + pad_x))
        bottom = min(h, int(y + bh + pad_y))
        if right - left < 8 or bottom - top < 8:
            return b""
        cropped = img.crop((left, top, right, bottom))
        if cropped.height > 360:
            ratio = 360 / cropped.height
            cropped = cropped.resize(
                (int(cropped.width * ratio), 360), Image.LANCZOS,
            )
        buf = io.BytesIO()
        cropped.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.debug("AI-history union crop failed: %s", e)
        return b""
