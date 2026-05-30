"""
Persistent inbox for WhatsApp messages we receive via webhook.

WasenderApi's `/whatsapp-sessions/{id}/message-logs` only returns
OUTBOUND messages — anything the caller sends to us has to be
collected via webhook. This module is the tiny on-disk ring buffer
the webhook handler writes into, and that the WhatsApp UI reads
back through the existing /chats and /messages endpoints.

Storage layout:
    data/demos/restaurant/whatsapp_inbox.json
        {"messages": [ {jid, ts, from_me, text, raw}, ...]}

The file is gitignored (it sits under data/demos/) because every
message contains caller PII. We cap the buffer at INBOX_LIMIT
entries so a long-running demo doesn't grow the file unboundedly.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.whatsapp_inbox")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_INBOX_PATH = _DATA_DIR / "whatsapp_inbox.json"

INBOX_LIMIT = 2000   # Keep the last N rows. Plenty for a demo.

_DIGITS = re.compile(r"\D+")
_LOCK = threading.Lock()    # File access from the webhook + UI requests


def _now_ts() -> int:
    return int(time.time())


def _load() -> list[dict]:
    if not _INBOX_PATH.exists():
        return []
    try:
        data = json.loads(_INBOX_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("whatsapp_inbox.json corrupt — starting fresh")
        return []
    if isinstance(data, dict):
        items = data.get("messages")
        return list(items) if isinstance(items, list) else []
    if isinstance(data, list):
        return data
    return []


def _save(rows: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"messages": rows[-INBOX_LIMIT:]}
    tmp = _INBOX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_INBOX_PATH)


def list_inbox() -> list[dict]:
    """Return the full stored inbox (oldest→newest)."""
    with _LOCK:
        return list(_load())


def _digits(s: str) -> str:
    return _DIGITS.sub("", s or "")


# ---- Wasender webhook payload extraction ---------------------------------
# WasenderApi has used a few different envelope shapes across versions:
#
#   {"event": "messages.upsert",
#    "data": {"messages": [{...one or more...}]}}
#
#   {"event_type": "message.received",
#    "session_id": "...",
#    "message": {...one row...}}
#
#   {"messages.received": {...row...}}
#
# We accept any of these and pull (jid, ts, text) out the same way the
# inbox endpoints do, so storage is identical regardless of envelope.

def _candidate_rows(payload: dict) -> list[dict]:
    """Walk the webhook payload and return any dict that looks like a
    message row (has at least a text-ish field or a from/to)."""
    out: list[dict] = []

    def is_message(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        for k in ("from", "to", "sender", "recipient",
                  "remoteJid", "chatJid", "jid"):
            if isinstance(d.get(k), str) and d[k].strip():
                return True
        # Some payloads only carry the body + a type label.
        if isinstance(d.get("messageType") or d.get("type"), str) and (
            d.get("text") or d.get("body") or d.get("message")
            or d.get("messageContent") or d.get("messageText")
        ):
            return True
        return False

    def walk(node, depth: int = 0):
        if depth > 6: return
        if isinstance(node, dict):
            if is_message(node):
                out.append(node)
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload)
    return out


def store_webhook(payload: dict) -> int:
    """Persist any message rows the webhook payload carries. Returns
    the number of rows actually appended (after deduplication on `id`)."""
    if not isinstance(payload, dict):
        return 0
    rows = _candidate_rows(payload)
    if not rows:
        return 0

    with _LOCK:
        existing = _load()
        existing_ids = {
            r.get("id") for r in existing
            if isinstance(r, dict) and r.get("id")
        }
        added = 0
        for row in rows:
            row_id = row.get("id") or (row.get("key") or {}).get("id")
            if row_id and row_id in existing_ids:
                continue
            # Stamp arrival time so the inbox can sort even if the
            # source doesn't provide a usable timestamp.
            wrapped = {
                "received_at": _now_ts(),
                "source":      "webhook",
                **row,
            }
            existing.append(wrapped)
            added += 1
            if row_id:
                existing_ids.add(row_id)
        if added:
            _save(existing)
        return added


def clear_inbox() -> int:
    """Wipe the stored inbox. Returns the number of rows removed."""
    with _LOCK:
        rows = _load()
        if not rows:
            return 0
        _save([])
        return len(rows)


def inbox_stats() -> dict:
    """Lightweight info for the SPA / status page."""
    rows = list_inbox()
    return {
        "path":       str(_INBOX_PATH),
        "count":      len(rows),
        "exists":     _INBOX_PATH.exists(),
        "last_ts":    max((int(r.get("received_at") or 0) for r in rows), default=0),
    }
