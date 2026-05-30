"""Frigate integration: config, health check, cameras, events, asset proxy."""
from __future__ import annotations

import re
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.state import state
from ..services.mqtt import mqtt_service

router = APIRouter()

_TIMEOUT = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
# Clip downloads can take a few seconds for larger events.
_CLIP_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)

# Small in-memory cache so repeated Range requests for the same clip
# don't re-download from Frigate every time. Sized for short event clips.
_CLIP_CACHE: dict[str, tuple[float, bytes, str]] = {}
_CLIP_CACHE_TTL = 300       # seconds
_CLIP_CACHE_MAX_ENTRIES = 6

_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def _require_url() -> str:
    if not state.frigate_url:
        raise HTTPException(status_code=503, detail="Frigate not configured")
    return state.frigate_url


class FrigateConfigIn(BaseModel):
    url: Optional[str] = None


class FrigateConfigOut(BaseModel):
    url: Optional[str] = None


def _normalize(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    u = raw.strip()
    if not u:
        return None
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "http://" + u
    return u.rstrip("/")


# ---------- configuration ----------------------------------------------------

@router.get("/config", response_model=FrigateConfigOut)
async def get_config() -> FrigateConfigOut:
    return FrigateConfigOut(url=state.frigate_url)


@router.post("/config", response_model=FrigateConfigOut)
async def set_config(payload: FrigateConfigIn) -> FrigateConfigOut:
    state.frigate_url = _normalize(payload.url)
    state.save()
    return FrigateConfigOut(url=state.frigate_url)


# ---------- health -----------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    if not state.frigate_url:
        return {"status": "idle", "message": "Not configured"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{state.frigate_url}/api/config")
            r.raise_for_status()
        return {"status": "ok", "message": "Connected"}
    except httpx.HTTPStatusError as e:
        return {"status": "err", "message": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"status": "err", "message": f"Unreachable: {type(e).__name__}"}


# ---------- cameras ----------------------------------------------------------

@router.get("/cameras")
async def list_cameras() -> list[dict]:
    if not state.frigate_url:
        raise HTTPException(status_code=503, detail="Frigate not configured")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{state.frigate_url}/api/config")
            r.raise_for_status()
            cfg = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    cameras = cfg.get("cameras", {}) or {}
    out = []
    for name, conf in cameras.items():
        out.append(
            {
                "name": name,
                "enabled": conf.get("enabled", True),
                "detect": conf.get("detect", {}),
                "snapshot_path": f"/api/frigate/snapshot/{name}",
            }
        )
    return out


# ---------- snapshot proxy ---------------------------------------------------

@router.get("/snapshot/{camera}")
async def snapshot(camera: str, h: int = 300) -> Response:
    base = _require_url()
    url = f"{base}/api/{camera}/latest.jpg"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, params={"h": h})
            r.raise_for_status()
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Snapshot fetch failed: {e}") from e


# ---------- live stream proxy ------------------------------------------------
# Frigate's per-camera live endpoint at /api/<camera> serves a continuous
# multipart/x-mixed-replace MJPEG stream (via the embedded go2rtc). Browsers
# render that natively when set as `<img src>` — much smoother than polling
# /latest.jpg every couple of seconds. We just relay the stream bytes + the
# upstream Content-Type so anything Frigate serves (MJPEG, fallback JPEG)
# Just Works for the client.

_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


@router.get("/stream/{camera}")
async def stream(camera: str) -> StreamingResponse:
    base = _require_url()
    upstream = f"{base}/api/{camera}"

    # Open the connection up front so we know the upstream is alive and we
    # can pass back the real Content-Type (MJPEG vs JPEG).
    client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT)
    try:
        req = client.build_request("GET", upstream)
        upstream_resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(502, f"Frigate stream open failed: {e}") from e

    if upstream_resp.status_code != 200:
        status = upstream_resp.status_code
        await upstream_resp.aclose()
        await client.aclose()
        raise HTTPException(502, f"Frigate stream returned HTTP {status}")

    content_type = upstream_resp.headers.get("content-type", "image/jpeg")

    async def relay():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        except (httpx.HTTPError, GeneratorExit):
            return
        finally:
            try:
                await upstream_resp.aclose()
            finally:
                await client.aclose()

    return StreamingResponse(
        relay(),
        media_type=content_type,
        headers={
            "Cache-Control": "no-store",
            # MJPEG streams should never be sniffed/transformed by intermediaries.
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------- live motion (push-based via MQTT) --------------------------------

@router.websocket("/motion/ws")
async def motion_ws(websocket: WebSocket) -> None:
    """Push motion + detected-object state to the browser as soon as the
    MQTT service receives it. No polling required."""
    await websocket.accept()

    # Send the current snapshot first so the UI doesn't blink while it waits
    # for the next MQTT message.
    try:
        await websocket.send_json({
            "type": "snapshot",
            "mqtt_connected": mqtt_service.connected,
            "cameras": mqtt_service.snapshot(),
        })
    except Exception:
        await _close_quietly(websocket)
        return

    queue = mqtt_service.add_subscriber()
    try:
        while True:
            msg = await queue.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        mqtt_service.remove_subscriber(queue)
        await _close_quietly(websocket)


async def _close_quietly(ws: WebSocket) -> None:
    try:
        await ws.close()
    except Exception:
        pass


# ---------- per-camera motion state (legacy HTTP fallback) -------------------

@router.get("/motion")
async def cameras_motion(cameras: str = "") -> dict[str, dict]:
    """Per-camera live motion/detection state, derived from Frigate's
    in-progress events. `cameras` is a comma-separated list of names."""
    base = _require_url()
    cam_list = [c.strip() for c in (cameras or "").split(",") if c.strip()]
    if not cam_list:
        return {}

    params: dict[str, Any] = {
        "cameras": ",".join(cam_list),
        "in_progress": 1,
        "limit": 100,
        "include_thumbnails": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/api/events", params=params)
            r.raise_for_status()
            events = r.json() or []
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    out: dict[str, dict] = {c: {"motion": False, "objects": []} for c in cam_list}
    for ev in events:
        c = ev.get("camera")
        if c not in out:
            continue
        out[c]["motion"] = True
        label = ev.get("label")
        if label and label not in out[c]["objects"]:
            out[c]["objects"].append(label)
    return out


# ---------- labels (for filter dropdown) -------------------------------------

@router.get("/labels")
async def list_labels() -> list[str]:
    base = _require_url()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/api/labels")
            r.raise_for_status()
            data = r.json()
        # Frigate returns a list of strings. Defensive in case it's wrapped.
        if isinstance(data, list):
            return sorted({str(x) for x in data})
        if isinstance(data, dict):
            return sorted({str(x) for x in data.keys()})
        return []
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e


# ---------- events -----------------------------------------------------------

@router.get("/events")
async def list_events(
    camera: Optional[str] = None,
    label: Optional[str] = None,
    after: Optional[float] = None,   # unix seconds
    before: Optional[float] = None,  # unix seconds
    limit: int = 50,
    has_snapshot: Optional[bool] = None,
    has_clip: Optional[bool] = None,
) -> list[dict[str, Any]]:
    base = _require_url()

    params: dict[str, Any] = {
        "limit": max(1, min(limit, 500)),
        "include_thumbnails": 0,   # we serve thumbnails via dedicated endpoint
    }
    if camera:        params["cameras"] = camera
    if label:         params["labels"] = label
    if after  is not None: params["after"]  = after
    if before is not None: params["before"] = before
    if has_snapshot is not None: params["has_snapshot"] = 1 if has_snapshot else 0
    if has_clip     is not None: params["has_clip"]     = 1 if has_clip else 0

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/api/events", params=params)
            r.raise_for_status()
            events = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}") from e

    out: list[dict[str, Any]] = []
    for ev in events or []:
        eid = ev.get("id")
        out.append({
            "id":           eid,
            "camera":       ev.get("camera"),
            "label":        ev.get("label"),
            "sub_label":    ev.get("sub_label"),
            "start_time":   ev.get("start_time"),
            "end_time":     ev.get("end_time"),
            "top_score":    ev.get("top_score") or (ev.get("data") or {}).get("top_score"),
            "has_clip":     bool(ev.get("has_clip")),
            "has_snapshot": bool(ev.get("has_snapshot")),
            "zones":        ev.get("zones") or [],
            "thumbnail_path": f"/api/frigate/events/{eid}/thumbnail",
            "snapshot_path":  f"/api/frigate/events/{eid}/snapshot" if ev.get("has_snapshot") else None,
            "clip_path":      f"/api/frigate/events/{eid}/clip"     if ev.get("has_clip")     else None,
        })
    return out


async def _proxy_event_asset(event_id: str, path: str) -> Response:
    base = _require_url()
    url = f"{base}/api/events/{event_id}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=60"},
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Asset fetch failed: {e}") from e


@router.get("/events/{event_id}/thumbnail")
async def event_thumbnail(event_id: str) -> Response:
    return await _proxy_event_asset(event_id, "thumbnail.jpg")


@router.get("/events/{event_id}/snapshot")
async def event_snapshot(event_id: str) -> Response:
    return await _proxy_event_asset(event_id, "snapshot.jpg")


async def _fetch_clip(event_id: str) -> tuple[bytes, str]:
    """Download a clip from Frigate, caching briefly. Frigate ignores Range, so
    we always pull the full file and serve byte ranges ourselves."""
    now = time.monotonic()
    cached = _CLIP_CACHE.get(event_id)
    if cached and (now - cached[0]) < _CLIP_CACHE_TTL:
        return cached[1], cached[2]

    base = _require_url()
    url = f"{base}/api/events/{event_id}/clip.mp4"
    try:
        async with httpx.AsyncClient(timeout=_CLIP_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Clip fetch failed: {e}") from e

    content = r.content
    media_type = r.headers.get("content-type", "video/mp4")

    # Evict oldest entries if cache is full.
    if len(_CLIP_CACHE) >= _CLIP_CACHE_MAX_ENTRIES:
        oldest = min(_CLIP_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _CLIP_CACHE.pop(oldest, None)
    _CLIP_CACHE[event_id] = (now, content, media_type)
    return content, media_type


@router.get("/events/{event_id}/clip")
async def event_clip(event_id: str, request: Request) -> Response:
    """Serve the event clip with proper Range support so HTML5 <video> can play and seek."""
    content, media_type = await _fetch_clip(event_id)
    total = len(content)

    range_header = request.headers.get("range", "")
    if range_header:
        match = _RANGE_RE.match(range_header.strip())
        if not match:
            # Malformed Range — fall through to a full 200 response.
            range_header = ""
        else:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else total - 1
            if start >= total or start < 0 or end < start:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
                )
            end = min(end, total - 1)
            chunk = content[start:end + 1]
            return Response(
                content=chunk,
                status_code=206,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                    "Cache-Control": "private, max-age=300",
                },
            )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(total),
            "Cache-Control": "private, max-age=300",
        },
    )
