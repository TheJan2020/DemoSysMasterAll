"""
Persistent WhatsApp inbox (Primewave demo).

WasenderApi's `/whatsapp-sessions/{id}/message-logs` returns OUTBOUND
messages only — INCOMING messages have to be captured via webhook. This
module is the tiny on-disk ring buffer the webhook handler writes into.

Storage layout:
    data/demos/primewave/whatsapp_inbox.json
        {"messages": [ {jid, ts, from_me, text, raw, received_at}, ...]}

Mutagen-ignored + gitignored (every message contains caller PII).
Buffer capped at INBOX_LIMIT entries.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

logger = logging.getLogger("demo_primewave.whatsapp_inbox")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "primewave"
_INBOX_PATH = _DATA_DIR / "whatsapp_inbox.json"

INBOX_LIMIT = 2000

_DIGITS = re.compile(r"\D+")
_LOCK = threading.Lock()


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
    with _LOCK:
        return list(_load())


# Wasender webhook envelope shapes we accept — same as restaurant.

def _candidate_rows(payload: dict) -> list[dict]:
    out: list[dict] = []

    def is_message(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        for k in ("from", "to", "sender", "recipient",
                  "remoteJid", "chatJid", "jid"):
            if isinstance(d.get(k), str) and d[k].strip():
                return True
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


def append_outbound(*, to: str, text: str, message_id: str = "") -> dict:
    """Record a message we just SENT successfully. Mirrors the shape of
    webhook rows so the SPA can render outgoing + incoming uniformly."""
    row = {
        "received_at": _now_ts(),
        "source":      "outbound",
        "from_me":     True,
        "to":          to,
        "jid":         (to + "@s.whatsapp.net") if to else "",
        "text":        text,
        "id":          message_id or f"local-{_now_ts()}",
    }
    with _LOCK:
        existing = _load()
        existing.append(row)
        _save(existing)
    return row


def clear_inbox() -> int:
    with _LOCK:
        rows = _load()
        if not rows:
            return 0
        _save([])
        return len(rows)


def inbox_stats() -> dict:
    rows = list_inbox()
    return {
        "path":    str(_INBOX_PATH),
        "count":   len(rows),
        "exists":  _INBOX_PATH.exists(),
        "last_ts": max((int(r.get("received_at") or 0) for r in rows), default=0),
    }
