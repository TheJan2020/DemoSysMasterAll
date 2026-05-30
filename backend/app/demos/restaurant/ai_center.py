"""
AI-Center Playground history — persistent ledger of free-form vision
questions the operator asked over a Frigate camera snapshot OR an
uploaded image, plus the model's answer.

Storage shape mirrors `kitchen_ai_history.py`:
  - JSON ledger at `data/demos/restaurant/ai_center.json`
  - Thumbnails at `data/demos/restaurant/ai_center/<id>.jpg`

Both are GITIGNORED (operator-supplied images + kitchen footage are PII)
— see the same carve-out used for `ai_history/` and `kitchen_rules/`.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.ai_center")

_DATA_DIR   = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH       = _DATA_DIR / "ai_center.json"
_IMAGES_DIR = _DATA_DIR / "ai_center"
_LOCK = threading.Lock()

# Cap the persisted log. Beyond this many entries, oldest are FIFO-evicted.
ENTRY_CAP = 300


# ----------------------------------------------------------------------
# Default + load/save
# ----------------------------------------------------------------------

def _default_state() -> dict:
    return {"entries": []}


def load_state() -> dict:
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_state()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("ai_center.json corrupt — starting fresh")
        return _default_state()
    if not isinstance(d, dict):
        return _default_state()
    entries = d.get("entries")
    return {"entries": [e for e in entries if isinstance(e, dict)]
                          if isinstance(entries, list) else []}


def _save_locked(state: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return state


# ----------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------

def list_entries(limit: int = 100, offset: int = 0) -> dict:
    if limit < 1: limit = 1
    if limit > 200: limit = 200
    if offset < 0: offset = 0
    entries = load_state().get("entries") or []
    entries.sort(key=lambda e: float(e.get("created_at") or 0), reverse=True)
    total = len(entries)
    return {
        "total":   total,
        "limit":   limit,
        "offset":  offset,
        "entries": entries[offset: offset + limit],
    }


def get_entry(entry_id: str) -> Optional[dict]:
    for e in load_state().get("entries") or []:
        if e.get("id") == entry_id:
            return e
    return None


def append_entry(*, image_bytes: bytes, question: str,
                   response_text: str, provider: str, model: str,
                   source: str, camera: Optional[str],
                   latency_ms: int, error: Optional[str],
                   loop: bool, loop_interval_s: Optional[int]) -> dict:
    """Persist a new entry + write the thumbnail JPEG. Enforces FIFO cap."""
    eid = uuid.uuid4().hex[:16]
    image_path = _IMAGES_DIR / f"{eid}.jpg"
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        image_path.write_bytes(image_bytes or b"")
    except Exception:
        logger.exception("ai_center: write thumbnail failed for %s", eid)

    row = {
        "id":              eid,
        "created_at":      time.time(),
        "question":        question or "",
        "response":        response_text or "",
        "provider":        provider or "",
        "model":           model or "",
        "source":          source or "",     # "camera" | "upload"
        "camera":          camera or None,
        "latency_ms":      int(latency_ms or 0),
        "error":           error or None,
        "loop":            bool(loop),
        "loop_interval_s": int(loop_interval_s or 0) if loop_interval_s else None,
        "image":           f"{eid}.jpg",
    }
    with _LOCK:
        state = _load_locked()
        state["entries"].append(row)
        # FIFO eviction
        if len(state["entries"]) > ENTRY_CAP:
            evict = state["entries"][: len(state["entries"]) - ENTRY_CAP]
            state["entries"] = state["entries"][-ENTRY_CAP:]
            for e in evict:
                _delete_image_for(e.get("id") or "")
        _save_locked(state)
    return row


def delete_entry(entry_id: str) -> bool:
    with _LOCK:
        state = _load_locked()
        before = len(state["entries"])
        state["entries"] = [e for e in state["entries"]
                              if e.get("id") != entry_id]
        removed = len(state["entries"]) < before
        if removed:
            _save_locked(state)
            _delete_image_for(entry_id)
        return removed


def clear_all() -> None:
    with _LOCK:
        state = _load_locked()
        for e in state.get("entries") or []:
            _delete_image_for(e.get("id") or "")
        state["entries"] = []
        _save_locked(state)


# ----------------------------------------------------------------------
# Image paths
# ----------------------------------------------------------------------

def _safe_id(eid: str) -> str:
    return "".join(c for c in (eid or "") if c.isalnum() or c in "-_")


def image_path(entry_id: str) -> Path:
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return _IMAGES_DIR / f"{_safe_id(entry_id)}.jpg"


def _delete_image_for(entry_id: str) -> None:
    if not entry_id: return
    p = image_path(entry_id)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass
