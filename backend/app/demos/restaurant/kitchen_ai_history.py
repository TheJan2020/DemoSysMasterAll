"""
Kitchen AI-Camera history — persistent ledger of person detections.

The Restaurant demo polls Frigate's `/api/events` for `label=person`
events on a configurable subset of cameras and saves two JPEGs per
event:
  - full frame snapshot (`<event_id>_full.jpg`)
  - person crop / thumbnail (`<event_id>_crop.jpg`)

The ledger lives at `data/demos/restaurant/ai_history.json` and the
images at `data/demos/restaurant/ai_history/`. Both directories carry
caller PII (faces, kitchen footage) so they are GITIGNORED — same
carve-out as `data/demos/*/calls/` and `whatsapp_inbox.json`.

Module shape mirrors the rest of the demo (load → coerce → save_locked
→ atomic .tmp replace), and the ledger is capped FIFO so a runaway
demo doesn't fill the disk.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.kitchen_ai_history")

_DATA_DIR     = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH         = _DATA_DIR / "ai_history.json"
_IMAGES_DIR   = _DATA_DIR / "ai_history"
_LOCK = threading.Lock()

# Cap the persisted event log. Once we hit this many rows, the oldest
# events get FIFO-evicted (both the JSON row and the JPEGs on disk).
EVENT_CAP = 500

# Periodic-scan cadence — the poller takes a fresh snapshot pair every
# `scan_interval_s` seconds (per camera) whenever a person is currently
# in frame. Frigate's event model only fires once per detection, so a
# time-based scan is what gives the operator a continuous timeline of
# activity (one row per scan, not one row per Frigate event).
DEFAULT_SCAN_INTERVAL_S = 10
SCAN_INTERVAL_MIN = 3
SCAN_INTERVAL_MAX = 300


def _default_state() -> dict:
    return {
        "selected_cameras": [],
        "scan_interval_s":  DEFAULT_SCAN_INTERVAL_S,
        "events":           [],
    }


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def load_state() -> dict:
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_state()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("ai_history.json corrupt — starting fresh")
        return _default_state()
    if not isinstance(d, dict):
        return _default_state()
    cams     = d.get("selected_cameras")
    events   = d.get("events")
    interval = d.get("scan_interval_s")
    try:
        interval_int = int(interval) if interval is not None else DEFAULT_SCAN_INTERVAL_S
    except Exception:
        interval_int = DEFAULT_SCAN_INTERVAL_S
    interval_int = max(SCAN_INTERVAL_MIN, min(SCAN_INTERVAL_MAX, interval_int))
    return {
        "selected_cameras": [str(c) for c in cams if isinstance(c, str)]
                              if isinstance(cams, list) else [],
        "scan_interval_s":  interval_int,
        "events":           [e for e in events if isinstance(e, dict)]
                              if isinstance(events, list) else [],
    }


def _save_locked(state: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return state


# ----------------------------------------------------------------------
# Selected cameras
# ----------------------------------------------------------------------

def get_selected_cameras() -> list[str]:
    return list(load_state().get("selected_cameras") or [])


def set_selected_cameras(cameras: list[str]) -> list[str]:
    """Replace the list of cameras the background poller watches. The
    poller picks the new list up on its next tick."""
    clean = sorted({str(c).strip() for c in (cameras or []) if str(c).strip()})
    with _LOCK:
        state = _load_locked()
        state["selected_cameras"] = clean
        _save_locked(state)
    return clean


# ----------------------------------------------------------------------
# Scan interval (seconds between periodic captures per camera)
# ----------------------------------------------------------------------

def get_scan_interval() -> int:
    return int(load_state().get("scan_interval_s") or DEFAULT_SCAN_INTERVAL_S)


def set_scan_interval(seconds: int) -> int:
    try:
        v = int(seconds)
    except Exception:
        v = DEFAULT_SCAN_INTERVAL_S
    v = max(SCAN_INTERVAL_MIN, min(SCAN_INTERVAL_MAX, v))
    with _LOCK:
        state = _load_locked()
        state["scan_interval_s"] = v
        _save_locked(state)
    return v


# ----------------------------------------------------------------------
# Event ledger
# ----------------------------------------------------------------------

def list_events(limit: int = 100, offset: int = 0,
                  camera: Optional[str] = None) -> dict:
    """Newest-first. `limit` capped at 200; `offset` for pagination."""
    if limit < 1:  limit = 1
    if limit > 200: limit = 200
    if offset < 0: offset = 0
    events = load_state().get("events") or []
    if camera:
        events = [e for e in events if e.get("camera") == camera]
    events.sort(key=lambda e: float(e.get("start_time") or 0), reverse=True)
    total = len(events)
    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "events": events[offset: offset + limit],
    }


def get_event(event_id: str) -> Optional[dict]:
    for e in load_state().get("events") or []:
        if e.get("id") == event_id:
            return e
    return None


def has_event(event_id: str) -> bool:
    return get_event(event_id) is not None


def append_event(event: dict) -> dict:
    """Append a new event row + enforce the FIFO cap. Skips silently if
    the event_id already exists (Frigate's events are unique by id, but
    our poller can refetch the same one if the cursor doesn't advance)."""
    eid = str(event.get("id") or "").strip()
    if not eid:
        raise ValueError("event needs an id")
    with _LOCK:
        state = _load_locked()
        if any(e.get("id") == eid for e in state["events"]):
            return event
        # Normalise the row shape so the UI doesn't have to defend.
        row = {
            "id":         eid,
            "camera":     str(event.get("camera") or ""),
            "label":      str(event.get("label") or "person"),
            "score":      round(float(event.get("score") or 0), 3),
            "top_score":  round(float(event.get("top_score") or 0), 3),
            "start_time": float(event.get("start_time") or 0),
            "end_time":   float(event.get("end_time") or 0) or None,
            "box":        event.get("box") if isinstance(event.get("box"), list) else None,
            "captured_at": int(time.time()),
            "snapshot":   f"image/{eid}?kind=full",
            "crop":       f"image/{eid}?kind=crop",
        }
        state["events"].append(row)
        # FIFO eviction.
        if len(state["events"]) > EVENT_CAP:
            evict = state["events"][: len(state["events"]) - EVENT_CAP]
            state["events"] = state["events"][-EVENT_CAP:]
            # Delete corresponding image files.
            for e in evict:
                _delete_image_files(e.get("id") or "")
        _save_locked(state)
        return row


def delete_event(event_id: str) -> bool:
    with _LOCK:
        state = _load_locked()
        before = len(state["events"])
        state["events"] = [e for e in state["events"] if e.get("id") != event_id]
        removed = len(state["events"]) < before
        if removed:
            _save_locked(state)
            _delete_image_files(event_id)
        return removed


def clear_all() -> None:
    """Wipe every event + every saved image. Selected-cameras list +
    scan interval are preserved so the poller keeps watching."""
    with _LOCK:
        state = _load_locked()
        for e in state.get("events") or []:
            _delete_image_files(e.get("id") or "")
        state["events"] = []
        _save_locked(state)


# ----------------------------------------------------------------------
# Image paths — used by the background poller (to save) and by the
# router (to serve via FileResponse).
# ----------------------------------------------------------------------

def image_dir() -> Path:
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return _IMAGES_DIR


def _safe_id(event_id: str) -> str:
    return "".join(c for c in (event_id or "") if c.isalnum() or c in "-_.")


def image_path(event_id: str, kind: str) -> Path:
    """Resolve the on-disk file for one of:
        full         → entire frame snapshot
        p0, p1, …    → per-person crops, one per detected in_progress event
        crop         → legacy alias for p0 (first person)
    Any other `kind` collapses to "full" defensively."""
    safe = _safe_id(event_id)
    k = (kind or "").strip().lower()
    if k == "crop":
        k = "p0"
    if k == "full":
        suffix = "full"
    elif k.startswith("p") and k[1:].isdigit() and 0 <= int(k[1:]) <= 99:
        suffix = k
    else:
        suffix = "full"
    return image_dir() / f"{safe}_{suffix}.jpg"


def _delete_image_files(event_id: str) -> None:
    """Remove every JPEG that belongs to this event — `<safe>_full.jpg`
    plus every `<safe>_p<N>.jpg` per-person crop. Uses a glob so the
    cleanup is correct regardless of how many persons were captured."""
    if not event_id: return
    safe = _safe_id(event_id)
    if not safe: return
    try:
        for p in image_dir().glob(f"{safe}_*.jpg"):
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


# ----------------------------------------------------------------------
# Stats — used by the SPA's headline cards.
# ----------------------------------------------------------------------

def stats() -> dict:
    events = load_state().get("events") or []
    if not events:
        return {
            "total":         0,
            "today":         0,
            "last_24h":      0,
            "by_camera":     {},
            "latest_event":  None,
        }
    now = time.time()
    midnight = _local_midnight()
    by_cam: dict[str, int] = {}
    today = 0
    last24 = 0
    latest = max(events, key=lambda e: float(e.get("start_time") or 0))
    for e in events:
        st = float(e.get("start_time") or 0)
        if st >= midnight: today += 1
        if st >= now - 24 * 3600: last24 += 1
        cam = str(e.get("camera") or "")
        if cam:
            by_cam[cam] = by_cam.get(cam, 0) + 1
    return {
        "total":         len(events),
        "today":         today,
        "last_24h":      last24,
        "by_camera":     by_cam,
        "latest_event":  latest,
    }


def _local_midnight() -> float:
    n = time.localtime()
    return time.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, n.tm_isdst))
