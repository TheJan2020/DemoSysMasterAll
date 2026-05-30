"""
WhatsApp Bot — text + voice-note Live Agent over WhatsApp.

Same persona + tools as the voice call agent (`live_agent.CallSession`),
but driven by Gemini's text-mode `generate_content` over a per-phone
conversation history instead of the streaming Live API. The webhook
in `router.py` calls `process_message(msg, broadcast=...)` for every
new inbound row; we resolve the sender to a patient phone, build a
context dict that mirrors the voice agent's, run the model + tool
loop until it returns a final text response, send that reply back via
Wasender, and persist the turn to disk so the next message keeps
context.

Privacy / safety
- We refuse to reply to LID senders (opaque WhatsApp privacy IDs).
  Without a real phone number we can't identify the patient or send
  a reply at all; replying-by-name would risk leaking patient data.
- We pass `skip_auto_whatsapp=True` in the tool context so the
  auto-template fires baked into `_t_create_patient`,
  `_t_create_appointment`, `_t_cancel_appointment`, and
  `_t_reschedule_appointment` are suppressed. Otherwise the caller
  would receive TWO messages per mutation: the bot's own reply, plus
  the template. The agent can still call `send_whatsapp_template`
  explicitly if it wants the templated copy.
- History is per-phone (digits-only key), stored at
  `data/demos/restaurant/whatsapp_conversations.json` (gitignored, PII).
  Capped at HISTORY_MAX_TURNS per conversation.

Voice notes
- Wasender forwards voice notes as audio webhook payloads — exact shape
  varies by plan, but typically an `audioMessage` block with a URL or
  base64 body. We make a best-effort attempt to extract the audio bytes
  and pass them to Gemini as an audio Part (same as the offline
  transcript enhancer). If we can't recognise the shape, we fall back to
  a graceful "voice notes coming soon" reply rather than silently
  dropping the message.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from google import genai
from google.genai import types

from ...core.state import state
from . import whatsapp_contacts
from .agent_tools import build_tools, execute_tool, load_snapshot
from .live_agent import (
    _GUARDRAILS,
    _build_roster_block,
    build_current_time_block,
    load_escalation_config,
    load_kb,
    load_persona,
)
from .wasender import WASENDER_BASE_URL

logger = logging.getLogger("demo_restaurant.whatsapp_bot")

# ----------------------------------------------------------------------
# History on disk — per-phone, capped, gitignored PII.
# ----------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_HISTORY_PATH = _DATA_DIR / "whatsapp_conversations.json"

# Persistent LID → phone JID cache.
# The snapshot-derived name index can lose entries when a patient is
# renamed, edited, or deleted, which silently breaks LID resolution for
# senders we'd already identified. Once we've successfully resolved a
# LID via push-name match, we persist the mapping here so it survives
# snapshot edits. Gitignored — caller PII.
_LID_CACHE_PATH = _DATA_DIR / "lid_phone_cache.json"

# Persistent set of Wasender msgIds that the bot (not a human operator
# at the WhatsApp page) sent. The chat list endpoint reads this so we
# can render an "AI" badge on bot-sent outbound messages, distinguishing
# them from messages the operator typed manually. Gitignored —
# semi-PII (correlates patient phones with bot-handled conversations).
_AI_SENT_PATH = _DATA_DIR / "whatsapp_bot_sent.json"
_AI_SENT_CAP  = 10_000

HISTORY_MAX_TURNS = 30           # Per-phone cap to keep prompts compact.
MAX_TOOL_ITERATIONS = 8          # Defense against runaway tool loops.
DEFAULT_TEXT_MODEL = "gemini-2.5-flash"
DEDUP_TTL_SECONDS = 300          # Drop repeat-fires of the same message id
                                 # within this window. Wasender (and our
                                 # own retries) can post the same webhook
                                 # row twice within milliseconds; without
                                 # dedup the bot replies twice and hits
                                 # the 1-msg-per-5-sec rate limit.
SEND_MIN_INTERVAL_S = 5.5        # Minimum seconds between sends to the
                                 # SAME phone. Wasender account-protection
                                 # caps at "1 message every 5 seconds";
                                 # 5.5s gives us comfortable headroom so
                                 # 200-OK responses actually deliver
                                 # instead of getting silently throttled.

_LOCK = threading.Lock()

# Per-phone last-send timestamp for throttling.
_LAST_SEND_TS: dict[str, float] = {}

# In-memory recent-message-id cache for dedup. Tuple of (id, expires_at).
# Bounded — we trim when it gets above a hard ceiling so memory stays flat.
_RECENT_IDS: dict[str, float] = {}
_RECENT_IDS_MAX = 2000


def _is_duplicate(msg_id: str) -> bool:
    """True if we've seen this Wasender message id within the TTL window.
    Marks it as seen as a side-effect when it's new. Bare `id` falls back
    to silently allow (no id to dedup against)."""
    if not msg_id:
        return False
    now = time.time()
    # Drop expired entries lazily.
    if len(_RECENT_IDS) > _RECENT_IDS_MAX:
        cutoff = now - DEDUP_TTL_SECONDS
        for k in [k for k, exp in _RECENT_IDS.items() if exp < cutoff]:
            _RECENT_IDS.pop(k, None)
    existing = _RECENT_IDS.get(msg_id)
    if existing and existing > now:
        return True
    _RECENT_IDS[msg_id] = now + DEDUP_TTL_SECONDS
    return False


def _load_lid_cache() -> dict[str, str]:
    if not _LID_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_LID_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()
                    if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        logger.exception("lid_phone_cache.json corrupt — starting fresh")
    return {}


def _save_lid_cache(cache: dict[str, str]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _LID_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_LID_CACHE_PATH)


def _remember_lid(lid_jid: str, phone_jid: str) -> None:
    """Persist a confirmed LID → phone mapping so future arrivals from
    the same LID resolve even if the snapshot patient is renamed or
    deleted."""
    if not (lid_jid and phone_jid):
        return
    with _LOCK:
        cache = _load_lid_cache()
        if cache.get(lid_jid) == phone_jid:
            return
        cache[lid_jid] = phone_jid
        _save_lid_cache(cache)
        logger.info("lid cache: stored %s → %s (%d entries)",
                    lid_jid, phone_jid, len(cache))


def _recall_lid(lid_jid: str) -> Optional[str]:
    with _LOCK:
        return _load_lid_cache().get(lid_jid)


def lid_cache_dump() -> dict[str, str]:
    """For the SPA — read-only view of the persistent LID cache."""
    with _LOCK:
        return _load_lid_cache()


def lid_cache_set(lid_jid: str, phone_jid: str) -> dict[str, str]:
    """For the SPA — let the operator manually add or correct an
    entry (e.g. when a user is brand-new and the auto-resolution path
    can't bootstrap them)."""
    if not (lid_jid and phone_jid):
        return lid_cache_dump()
    _remember_lid(lid_jid, phone_jid)
    return lid_cache_dump()


def lid_cache_delete(lid_jid: str) -> bool:
    with _LOCK:
        cache = _load_lid_cache()
        if lid_jid not in cache:
            return False
        del cache[lid_jid]
        _save_lid_cache(cache)
    return True


def _load_ai_sent() -> set[str]:
    if not _AI_SENT_PATH.exists():
        return set()
    try:
        data = json.loads(_AI_SENT_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data if x is not None}
    except Exception:
        logger.exception("whatsapp_bot_sent.json corrupt — starting fresh")
    return set()


def _save_ai_sent(ids: set[str]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _AI_SENT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_AI_SENT_PATH)


def record_ai_sent(msg_id: str) -> None:
    """Mark this Wasender msgId as having been sent by the bot. The
    chat-list endpoint reads the set to flag outbound rows with an "AI"
    badge so the operator can tell at a glance which messages they
    typed manually vs which the bot produced."""
    if not msg_id:
        return
    with _LOCK:
        ids = _load_ai_sent()
        if str(msg_id) in ids:
            return
        ids.add(str(msg_id))
        # Cap to keep the file bounded — drops the oldest ids when over.
        if len(ids) > _AI_SENT_CAP:
            ids = set(sorted(ids)[-(_AI_SENT_CAP // 2):])
        _save_ai_sent(ids)


def ai_sent_ids() -> set[str]:
    """Read-only view, used by the messages endpoint to tag rows."""
    with _LOCK:
        return _load_ai_sent()


def _msg_id(m: dict) -> str:
    """Pull whatever stable identifier this row carries. Wasender uses
    several shapes — Baileys puts it under `key.id`, flat shapes use
    top-level `id` or `messageId`."""
    for k in ("id", "messageId", "message_id"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    key = m.get("key")
    if isinstance(key, dict):
        for k in ("id", "messageId"):
            v = key.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _load_history() -> dict[str, list[dict]]:
    if not _HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                k: list(v) for k, v in data.items()
                if isinstance(k, str) and isinstance(v, list)
            }
    except Exception:
        logger.exception("whatsapp_conversations.json corrupt — starting fresh")
    return {}


def _save_history(hist: dict[str, list[dict]]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_HISTORY_PATH)


def _read_turns(phone_digits: str) -> list[dict]:
    with _LOCK:
        return list(_load_history().get(phone_digits, []))


def _append_turns(phone_digits: str, new_turns: list[dict]) -> None:
    if not new_turns:
        return
    with _LOCK:
        hist = _load_history()
        cur = list(hist.get(phone_digits, []))
        cur.extend(new_turns)
        if len(cur) > HISTORY_MAX_TURNS:
            cur = cur[-HISTORY_MAX_TURNS:]
        hist[phone_digits] = cur
        _save_history(hist)


def get_conversation(phone_digits: str) -> list[dict]:
    """For the SPA — returns the on-disk transcript for one phone."""
    return _read_turns(phone_digits)


def list_conversations() -> list[dict]:
    """For the SPA — summary of every phone we have a history for."""
    with _LOCK:
        hist = _load_history()
    out: list[dict] = []
    for phone, turns in hist.items():
        last_ts = max((int(t.get("ts") or 0) for t in turns), default=0)
        last_text = ""
        for t in reversed(turns):
            txt = str(t.get("text") or "").strip()
            if txt:
                last_text = txt
                break
        out.append({
            "phone":      phone,
            "turns":      len(turns),
            "last_ts":    last_ts,
            "last_text":  last_text[:120],
        })
    out.sort(key=lambda r: r["last_ts"], reverse=True)
    return out


def clear_conversation(phone_digits: str) -> bool:
    with _LOCK:
        hist = _load_history()
        if phone_digits not in hist:
            return False
        del hist[phone_digits]
        _save_history(hist)
    return True


def clear_all_conversations() -> int:
    with _LOCK:
        hist = _load_history()
        n = len(hist)
        _save_history({})
    return n


# ----------------------------------------------------------------------
# Inbound payload extraction.
# ----------------------------------------------------------------------

_DIGITS_RE = re.compile(r"\D+")


def _digits(s: Any) -> str:
    return _DIGITS_RE.sub("", str(s or ""))


def _msg_jid(m: dict) -> str:
    """Pick the chat-key out of a webhook row. Mirrors router._msg_jid
    but kept here to avoid an import cycle."""
    for k in ("remoteJid", "chatJid", "jid", "chat_id", "chatId",
              "from", "sender"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            if "@" not in v and v.strip().isdigit():
                return f"{v.strip()}@s.whatsapp.net"
            return v.strip()
    key = m.get("key")
    if isinstance(key, dict):
        v = key.get("remoteJid") or key.get("jid")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


_NON_TEXT_KEYS = {
    "id", "messageId", "message_id", "key", "remoteJid", "chatJid",
    "jid", "from", "to", "sender", "recipient", "phone", "number",
    "messageTimestamp", "timestamp", "ts", "createdAt", "created_at",
    "updatedAt", "updated_at", "sessionId", "session_id", "session",
    "status", "ack", "direction", "fromMe", "from_me",
    "messageType", "type", "kind", "mediaType",
    "remoteJidServer", "participant", "mimetype", "url",
    "mediaUrl", "media_url", "downloadUrl", "download_url",
    "fileUrl", "file_url",
}


def _deep_find_text(node: Any, depth: int = 0) -> str:
    """Recursively dig for the longest free-text string that isn't an
    id / jid / timestamp. Same heuristic as router._deep_find_text — kept
    here so the bot can recover from Wasender shape variations we
    haven't enumerated explicitly."""
    if depth > 6:
        return ""
    best = ""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _NON_TEXT_KEYS:
                continue
            if isinstance(v, str):
                s = v.strip()
                if not s: continue
                if "@s.whatsapp.net" in v or "@g.us" in v or "@lid" in v: continue
                if len(s) > len(best):
                    best = s
            elif isinstance(v, (dict, list)):
                deeper = _deep_find_text(v, depth + 1)
                if len(deeper) > len(best):
                    best = deeper
    elif isinstance(node, list):
        for item in node:
            deeper = _deep_find_text(item, depth + 1)
            if len(deeper) > len(best):
                best = deeper
    return best


def _msg_text(m: dict) -> str:
    """Pull a text body, if any. Tries common explicit paths first then
    falls back to the longest plausible string in the payload — same
    pattern the router uses, so we cope with whatever Wasender shape
    arrives."""
    for k in ("text", "body", "message", "content",
              "messageText", "messageContent", "message_body",
              "text_body", "msg", "msg_text", "caption"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for inner in ("text", "body", "content", "caption", "conversation"):
                vv = v.get(inner)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    msg = m.get("message")
    if isinstance(msg, dict):
        for k in ("conversation", "text"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        ext = msg.get("extendedTextMessage")
        if isinstance(ext, dict):
            v = ext.get("text") or ext.get("caption")
            if isinstance(v, str) and v.strip():
                return v.strip()
    # Last resort — longest free-text string in the payload.
    deep = _deep_find_text(m)
    return deep


def _find_audio_block(m: dict) -> Optional[dict]:
    """Back-compat shim — voice-only callers used this before the
    generic _find_media_block existed. Returns the audio block only,
    None for anything else."""
    block, kind, _ = _find_media_block(m)
    return block if kind == "audio" else None


# Maps Baileys / Wasender message-block keys to the WhatsApp media kind
# they represent. Iterated in priority order: voice notes first (so they
# don't fall through to "document"), then images / videos / documents.
_MEDIA_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("audioMessage",    "audio"),
    ("voiceMessage",    "audio"),
    ("voiceNote",       "audio"),
    ("imageMessage",    "image"),
    ("videoMessage",    "video"),
    ("documentMessage", "document"),
    ("stickerMessage",  "image"),
    # Wasender flat shapes (no 'Message' suffix)
    ("audio",           "audio"),
    ("voice",           "audio"),
    ("voice_note",      "audio"),
    ("image",           "image"),
    ("video",           "video"),
    ("document",        "document"),
    ("sticker",         "image"),
)

# messageType / type label → kind, for flat rows where the media block
# isn't separately nested.
_MEDIA_TYPE_MAP = {
    "audio":      "audio",
    "voice":      "audio",
    "ptt":        "audio",
    "voice_note": "audio",
    "voicenote":  "audio",
    "image":      "image",
    "photo":      "image",
    "video":      "video",
    "document":   "document",
    "file":       "document",
    "sticker":    "image",
}


def _find_media_block(m: dict) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Locate ANY media block (audio / image / video / document) inside
    a webhook row. Returns `(block, kind, filename)`:
      - block:    the dict carrying the URL / mediaKey / mimetype, or None
      - kind:     "audio" | "image" | "video" | "document", or None
      - filename: best-effort human filename (for documents), or None
    """
    msg = m.get("message")
    # Baileys-shaped: m.message.{xxxMessage}
    if isinstance(msg, dict):
        for key, kind in _MEDIA_KEY_MAP:
            block = msg.get(key)
            if isinstance(block, dict):
                filename = block.get("fileName") or block.get("filename")
                return block, kind, (filename if isinstance(filename, str) else None)
    # Wasender flat / alternative: top-level xxxMessage or xxx
    for key, kind in _MEDIA_KEY_MAP:
        block = m.get(key)
        if isinstance(block, dict):
            filename = block.get("fileName") or block.get("filename")
            return block, kind, (filename if isinstance(filename, str) else None)
    # messageType label + nested media
    mtype = str(m.get("messageType") or m.get("type") or "").lower()
    kind = _MEDIA_TYPE_MAP.get(mtype)
    if kind:
        media = m.get("media") or m.get("attachment")
        if isinstance(media, dict):
            filename = media.get("fileName") or media.get("filename")
            return media, kind, (filename if isinstance(filename, str) else None)
        # Flat row with the url at top level — treat the row itself
        # as the block.
        filename = m.get("fileName") or m.get("filename")
        return m, kind, (filename if isinstance(filename, str) else None)
    return None, None, None


def _msg_from_me(m: dict, our_digits: str) -> bool:
    """Webhook rows: explicit fromMe or direction wins, otherwise check
    if `from` matches our paired number."""
    for k in ("fromMe", "from_me", "is_outgoing", "isOutgoing",
              "outgoing", "is_sent_by_me", "sentByMe"):
        v = m.get(k)
        if isinstance(v, bool):
            return v
    key = m.get("key")
    if isinstance(key, dict):
        for k in ("fromMe", "from_me"):
            if isinstance(key.get(k), bool):
                return key[k]
    direction = str(m.get("direction") or m.get("dir") or "").lower()
    if direction in ("out", "outgoing", "outbound", "sent"):
        return True
    if direction in ("in", "incoming", "inbound", "received"):
        return False
    if our_digits:
        for k in ("from", "sender", "from_number", "from_jid"):
            v = m.get(k)
            if isinstance(v, str):
                d = _digits(v)
                if d:
                    return d == our_digits
    return False


# ----------------------------------------------------------------------
# Voice-note transcription (best-effort).
# ----------------------------------------------------------------------

def _decode_b64(s: str) -> Optional[bytes]:
    """Tolerant base64 decode — handles standard, URL-safe, and missing
    padding. Returns None on failure."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    # Strip data: prefix if present.
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    # Pad to a multiple of 4 chars.
    padded = s + "=" * (-len(s) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(padded)
        except Exception:
            continue
    return None


def _hkdf_expand(key: bytes, length: int, info: bytes) -> bytes:
    """HKDF-SHA256 extract+expand. WhatsApp uses 32 zero bytes as salt."""
    import hmac as _hmac
    import hashlib as _hashlib
    salt = b"\x00" * 32
    prk = _hmac.new(salt, key, _hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = _hmac.new(prk, block + info + bytes([counter]),
                          _hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _looks_like_media(pt: bytes, media_type: str) -> bool:
    """Per-kind plausibility check on decrypted plaintext. We use this
    to pick the right ciphertext layout when both `[ct | 10-byte MAC]`
    and bare `[ct]` are possible. For documents we accept anything that
    passed PKCS7 because a document can be literally any byte pattern."""
    if not pt:
        return False
    if media_type == "audio":
        return (pt[:4] == b"OggS" or pt[:4] == b"RIFF"
                or pt[:3] == b"ID3" or pt[4:8] == b"ftyp"
                or pt[:4] == b"fLaC" or pt[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
    if media_type == "image":
        return (pt[:3] == b"\xff\xd8\xff"           # JPEG
                or pt[:8] == b"\x89PNG\r\n\x1a\n"   # PNG
                or pt[:4] == b"GIF8"                # GIF
                or pt[:4] == b"RIFF"                # WebP (RIFF+WEBP, good enough)
                or pt[:2] == b"BM")                 # BMP
    if media_type == "video":
        return (pt[4:8] == b"ftyp"                  # MP4 / MOV
                or pt[:4] == b"RIFF"                # AVI
                or pt[:4] == b"\x1aE\xdf\xa3")      # MKV / WebM
    # documents — anything goes; very common formats include PDF, ZIP-
    # based (DOCX/XLSX/PPTX), plaintext, CSV, …
    return True


def _decrypt_whatsapp_media(encrypted: bytes, media_key: bytes,
                             media_type: str = "audio") -> Optional[bytes]:
    """Decrypt a WhatsApp E2E-encrypted media blob using its mediaKey.
    Returns the plaintext bytes (e.g. an OGG/Opus file for audio, a PDF
    for documents), or None on any failure. Pure-Python implementation
    of the well-known WhatsApp media key derivation:
        HKDF-SHA256(salt=32×0, info='WhatsApp {Kind} Keys', length=112)
        → iv(16) | cipherKey(32) | macKey(32) | refKey(32)
        plaintext = AES-256-CBC(cipherKey, iv).decrypt(blob[:-10])
        mac = HMAC-SHA256(macKey, iv + blob[:-10])[:10] == blob[-10:]
    The info string changes per media kind, so passing the wrong kind
    here silently yields garbage that fails the magic-bytes check
    afterwards. Sticker uses Image keys (per WhatsApp protocol).
    """
    info_map = {
        "image":    b"WhatsApp Image Keys",
        "video":    b"WhatsApp Video Keys",
        "audio":    b"WhatsApp Audio Keys",
        "document": b"WhatsApp Document Keys",
        "sticker":  b"WhatsApp Image Keys",
    }
    info = info_map.get(media_type, b"WhatsApp Document Keys")
    try:
        expanded = _hkdf_expand(media_key, 112, info)
        iv          = expanded[0:16]
        cipher_key  = expanded[16:48]
        if len(encrypted) < 11:
            return None
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )
        from cryptography.hazmat.backends import default_backend
        # Some gateways serve [ciphertext | 10-byte MAC]; others serve
        # just [ciphertext]. Try the standard layout first.
        for ct in (encrypted[:-10], encrypted):
            try:
                cipher = Cipher(algorithms.AES(cipher_key), modes.CBC(iv),
                                backend=default_backend())
                dec = cipher.decryptor()
                pt = dec.update(ct) + dec.finalize()
                # Strip PKCS7 padding.
                if pt:
                    pad = pt[-1]
                    if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
                        pt = pt[:-pad]
                if _looks_like_media(pt, media_type):
                    return pt
            except Exception:
                continue
        return None
    except Exception:
        logger.exception("media decrypt failed")
        return None


def _media_key_from_block(block: dict) -> Optional[bytes]:
    """Pull the mediaKey out of the audio block. WhatsApp/Baileys
    represents it as base64; some Wasender variants might also use
    hex or raw bytes. Returns 32-byte key or None."""
    for k in ("mediaKey", "media_key", "mediakey"):
        v = block.get(k)
        if v is None:
            continue
        if isinstance(v, (bytes, bytearray)) and len(v) == 32:
            return bytes(v)
        if isinstance(v, str):
            b = _decode_b64(v)
            if b and len(b) == 32:
                return b
            # Hex fallback
            try:
                hx = bytes.fromhex(v)
                if len(hx) == 32:
                    return hx
            except Exception:
                pass
    return None


_DEFAULT_MIME_FOR_KIND = {
    "audio":    "audio/ogg",
    "image":    "image/jpeg",
    "video":    "video/mp4",
    "document": "application/octet-stream",
    "sticker":  "image/webp",
}


def _mime_from_block(block: dict, kind: str = "audio") -> str:
    """Trust the media block's stated mimetype over the HTTP
    Content-Type — Wasender often serves the media URL with
    `application/octet-stream`, which downstream consumers reject. Falls
    back to a sensible default per media kind when the block doesn't
    specify one."""
    default = _DEFAULT_MIME_FOR_KIND.get(kind, "application/octet-stream")
    raw = (block.get("mimetype") or block.get("mime")
           or block.get("mime_type") or default)
    if not isinstance(raw, str):
        raw = default
    return raw.split(";")[0].strip() or default


async def _fetch_audio_bytes(block: dict) -> tuple[Optional[bytes], Optional[str]]:
    """Back-compat shim — voice-only call paths use this. Delegates to
    the generic _fetch_media_bytes with kind='audio'."""
    return await _fetch_media_bytes(block, "audio")


async def _fetch_media_bytes(block: dict, kind: str = "audio") -> tuple[Optional[bytes], Optional[str]]:
    """Resolve a media block of any kind (audio / image / video /
    document) to (bytes, mime_type). Steps:
      1. Try inline base64 in the block.
      2. Else download from any URL field.
      3. If the bytes don't look like a real container AND the block
         carries a `mediaKey`, attempt WhatsApp E2E decryption.
         Wasender's media URL serves the still-encrypted blob on the
         per-session API-key tier — without this step the receiver gets
         random ciphertext.
    """
    logger.info("media[%s]: block keys=%s",
                kind, list(block.keys())[:15])

    inline: Optional[bytes] = None
    for k in ("base64", "b64", "data_base64", "payload", "data"):
        v = block.get(k)
        if isinstance(v, str) and len(v) > 100:
            inline = _decode_b64(v)
            if inline:
                break
    raw_bytes: Optional[bytes] = inline

    if not raw_bytes:
        url = None
        for k in ("url", "mediaUrl", "media_url", "downloadUrl",
                  "download_url", "fileUrl", "file_url"):
            v = block.get(k)
            if isinstance(v, str) and v.startswith("http"):
                url = v
                break
        if not url:
            logger.warning("media[%s]: no inline data and no url found", kind)
            return None, None
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                r = await client.get(url)
        except httpx.RequestError as e:
            logger.warning("media[%s] fetch failed: %s", kind, e)
            return None, None
        if not (200 <= r.status_code < 300):
            logger.warning("media[%s] fetch HTTP %s", kind, r.status_code)
            return None, None
        raw_bytes = r.content

    if not raw_bytes:
        return None, None

    # Always prefer the block's stated mimetype. Wasender frequently
    # serves `application/octet-stream`, which downstream consumers
    # (Gemini for audio, <img>/<video> tags for visuals, browser file
    # dialogs for documents) all reject or mishandle.
    mime = _mime_from_block(block, kind)

    # If the bytes already look like the expected kind of payload,
    # ship as-is. For documents we don't have a single magic to check
    # (anything goes) so we trust the URL response.
    if _looks_like_media(raw_bytes, kind):
        return raw_bytes, mime

    # Encrypted? Try to decrypt with the mediaKey.
    media_key = _media_key_from_block(block)
    if not media_key:
        logger.warning(
            "media[%s]: bytes don't match expected container (head=%s) and "
            "no mediaKey field is present. Returning as-is — may not be "
            "viewable.", kind, raw_bytes[:16].hex(),
        )
        return raw_bytes, mime
    plaintext = _decrypt_whatsapp_media(raw_bytes, media_key, kind)
    if plaintext:
        logger.info(
            "media[%s]: decrypted %d → %d bytes",
            kind, len(raw_bytes), len(plaintext),
        )
        return plaintext, mime
    logger.warning(
        "media[%s]: mediaKey present but decryption produced no valid "
        "container. The mediaKey may be wrong or the gateway transformed "
        "the blob differently than expected.", kind,
    )
    return raw_bytes, mime


_AUDIO_MAGIC = {
    b"OggS":            "ogg",
    b"RIFF":            "wav (RIFF)",
    b"ID3":             "mp3 (ID3)",
    b"\xff\xfb":        "mp3 (frame)",
    b"\xff\xf3":        "mp3 (frame)",
    b"\xff\xf2":        "mp3 (frame)",
    b"fLaC":            "flac",
    b"\x1aE\xdf\xa3":   "matroska/webm",
}


def _identify_audio(b: bytes) -> str:
    """Look at the first few bytes and report the container we see. Tells
    us instantly whether Wasender is serving real OGG/Opus or an
    encrypted blob (which doesn't match any known audio magic and is
    what causes Gemini to silently return empty)."""
    if not b:
        return "empty"
    head = b[:16]
    for magic, label in _AUDIO_MAGIC.items():
        if head.startswith(magic):
            return label
    # ID3 may be prefixed; check at offset too
    if b[4:8] == b"ftyp":
        return "mp4/m4a"
    return f"unknown (head={head.hex()})"


async def _transcribe_voice_note(audio_bytes: bytes, mime: str) -> Optional[str]:
    """Send the audio to Gemini for transcription. Returns the plain
    transcript or None on failure."""
    if not state.gemini_api_key:
        logger.warning("voice-note transcription SKIPPED: gemini_api_key not set")
        return None
    # Diagnostic: tell us up-front whether what we fetched is even a
    # decoded audio container. End-to-end-encrypted WhatsApp media that
    # hasn't been decrypted will land here as random bytes with no magic
    # header, and Gemini will silently return an empty string. Seeing
    # "unknown (head=…)" in the log is the smoking gun.
    container = _identify_audio(audio_bytes)
    logger.info("voice-note: %d bytes mime=%s container=%s",
                len(audio_bytes), mime, container)
    if container.startswith("unknown") or container == "empty":
        logger.warning(
            "voice-note: bytes don't look like a real audio container "
            "(%s). Wasender may be serving the encrypted media blob; "
            "decryption with the mediaKey would be required.", container,
        )
    try:
        client = genai.Client(api_key=state.gemini_api_key)
        part = types.Part.from_bytes(data=audio_bytes, mime_type=mime)
        prompt = (
            "Transcribe this WhatsApp voice note from a clinic patient. "
            "The caller likely speaks Arabic (Saudi dialect) or English. "
            "Return ONLY the transcript text, with no commentary, no "
            "language label, and no quotation marks. Preserve the original "
            "language — do NOT translate. If unintelligible, return an "
            "empty string."
        )
        resp = await client.aio.models.generate_content(
            model=DEFAULT_TEXT_MODEL,
            contents=[part, prompt],
        )
        text = (getattr(resp, "text", None) or "").strip()
        if text:
            logger.info("voice-note transcribed: %d chars", len(text))
            return text
        logger.warning(
            "voice-note: Gemini returned empty transcript "
            "(container=%s, mime=%s, %d bytes). Most likely cause: the "
            "fetched bytes aren't a valid playable audio stream — either "
            "encrypted (Wasender served the raw blob without decrypting "
            "with mediaKey) or in a codec Gemini can't decode.",
            container, mime, len(audio_bytes),
        )
        return None
    except Exception:
        logger.exception("voice-note transcription failed")
        return None


# ----------------------------------------------------------------------
# Reply send-back via Wasender.
# ----------------------------------------------------------------------

async def _send_reply(phone_digits: str, text: str) -> Optional[str]:
    """Send `text` back to the user via WasenderApi. Returns the
    message_id on success, None on failure (logged)."""
    if not text.strip():
        logger.warning("bot reply skipped: empty text")
        return None
    cfg = load_escalation_config()
    api_key = str(cfg.get("wasender_api_key") or "").strip()
    if not api_key:
        logger.warning("bot reply SKIPPED: wasender_api_key not configured "
                       "(set it on the Configuration page)")
        return None
    # Per-phone throttle. Wasender's account-protection caps sends at
    # 1 per 5 seconds; firing faster than that returns 200 but silently
    # drops the message OR returns HTTP 429. Sleep until we're safely
    # past the limit for this phone.
    last = _LAST_SEND_TS.get(phone_digits, 0.0)
    delta = time.time() - last
    if delta < SEND_MIN_INTERVAL_S:
        wait = SEND_MIN_INTERVAL_S - delta
        logger.info("bot reply: throttling %.1fs to stay under Wasender's "
                    "account-protection rate limit (last send %.1fs ago)",
                    wait, delta)
        await asyncio.sleep(wait)
    _LAST_SEND_TS[phone_digits] = time.time()
    body = {"to": phone_digits, "messageType": "text", "text": text}
    logger.info("bot reply: POST send-message to=%s text_len=%d",
                phone_digits, len(text))
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{WASENDER_BASE_URL}/send-message",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
                json=body,
            )
    except httpx.RequestError as e:
        logger.warning("bot reply network error: %s", e)
        return None
    if not (200 <= r.status_code < 300):
        logger.warning("bot reply FAILED HTTP %s: %s",
                       r.status_code, (r.text or "")[:300])
        return None
    try:
        payload = r.json()
    except Exception:
        payload = {}
    # Wasender's actual response shape (observed in this demo's plan tier):
    #   {"success": true,
    #    "data": {"msgId": 12345, "jid": "...", "status": "in_progress"}}
    # Old docs and other tiers use "message_id" — accept either spelling.
    data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    mid = (str(data.get("message_id") or data.get("msgId") or "")) or None
    # Treat success=false as failure even on 2xx — Wasender sometimes
    # 200s a rejection (rate limit, blocked recipient, etc.).
    if isinstance(payload, dict) and payload.get("success") is False:
        err = str(payload.get("message") or payload.get("error")
                  or "Wasender returned success=false")
        logger.warning("bot reply REJECTED by Wasender: %s (raw=%s)",
                       err, (r.text or "")[:300])
        return None
    if mid:
        # Surface the status field so the operator can tell "queued but
        # not yet delivered" (in_progress) from "actually sent". A
        # message stuck at in_progress for >30 sec usually means the
        # paired Wasender WhatsApp session is disconnected — check the
        # Wasender dashboard's session status.
        status = str(data.get("status") or "") or "?"
        logger.info("bot reply OK: msgId=%s status=%s", mid, status)
        if status == "in_progress":
            logger.info(
                "  ↳ Wasender queued the message. If it doesn't arrive "
                "within a minute, verify the paired session is "
                "'Connected' on the Wasender dashboard.",
            )
        # Persist so the WhatsApp page can render an "AI" badge on the
        # outbound row when Wasender's /message-logs feeds it back.
        record_ai_sent(mid)
        return mid
    # 2xx, success!=false, no id we recognise — log the raw body so we
    # can adapt to whatever shape Wasender is sending today.
    logger.warning(
        "bot reply HTTP 200 but no message id field found — Wasender "
        "response shape may have changed. Full response: %s",
        (r.text or "")[:500],
    )
    return None


# ----------------------------------------------------------------------
# Caller identification + ctx priming.
# ----------------------------------------------------------------------

def _identify_caller(phone_digits: str) -> tuple[set[str], Optional[dict]]:
    """If the sender's phone matches a patient on file, return
    ({patient_id}, patient_record). Otherwise ({}, None)."""
    if not phone_digits:
        return set(), None
    target = phone_digits
    snap = load_snapshot()
    for p in snap.get("patients", []):
        if _digits(p.get("phone")) == target:
            pid = p.get("id")
            if pid:
                return {pid}, p
    return set(), None


# ----------------------------------------------------------------------
# Text-mode system instruction.
# ----------------------------------------------------------------------

_TEXT_MODE_PREAMBLE = """# WhatsApp Bot mode — text channel, not voice

You are the same Layla persona as the voice agent, but you're
operating over WhatsApp chat with this user instead of a phone call.
The persona and clinic facts below apply unchanged — but a few
behaviours change because the channel is text, not audio:

- Keep replies CONCISE. WhatsApp messages should be 1–4 sentences
  normally; longer only when listing options the user explicitly
  asked for (e.g. free slots).
- You CAN use Arabic script — the user reads it directly. Do NOT
  spell out digits one-by-one in WhatsApp; just write the digits.
  The "speak digit-by-digit" guardrail applies to PHONE CALLS ONLY.
- When you call create_patient / create_appointment etc., the system
  automatically suppresses the auto-WhatsApp template that would
  normally fire on a phone call. So if you want the user to receive
  a templated copy of (e.g.) their appointment confirmation, call
  `send_whatsapp_template` yourself. Otherwise just summarise the
  result in your own reply.
- You do NOT need to call `end_call`. The conversation stays open;
  the user may continue at any time. Reply naturally and stop.
- Never speak in third person about yourself ("Layla is checking
  …"). Speak as Layla, in first person.
- Treat each user message as a new turn in an ongoing conversation.
  Earlier turns in this WhatsApp thread are included below.
"""


def _identified_block(patient: Optional[dict], phone_digits: str) -> str:
    """Per-message context block telling the agent who's writing."""
    if patient:
        return (
            "\n\n## WHO IS WRITING (already identified on file)\n"
            f"- patient_id:  {patient.get('id')}\n"
            f"- file_number: {patient.get('file_number')}\n"
            f"- name (en):   {patient.get('name')}\n"
            f"- name (ar):   {patient.get('name_ar')}\n"
            f"- phone:       {patient.get('phone')}\n"
            "You may use this patient_id directly with scheduling tools — "
            "no separate lookup_patient_* call is needed. Address them "
            "by name on the first reply of a new thread."
        )
    return (
        "\n\n## WHO IS WRITING (unknown — no patient on file with this phone)\n"
        f"- WhatsApp phone: +{phone_digits}\n"
        "Greet them, ask their name (Arabic if their message was in "
        "Arabic), and offer to create a file with create_patient. The "
        "patient name is the only required field — phone is already "
        "known."
    )


def _build_system_instruction(patient: Optional[dict], phone_digits: str) -> str:
    return (
        _TEXT_MODE_PREAMBLE
        + "\n\n"
        + load_persona().strip()
        + _GUARDRAILS
        + "\n\n"
        + load_kb().strip()
        + _build_roster_block()
        # Authoritative date + time — same block the voice agent uses. Without
        # this the model defaults to its training-cutoff year (we observed
        # `list_free_slots(date='2024-06-13')` when today is in 2026).
        + build_current_time_block()
        + _identified_block(patient, phone_digits)
    )


# ----------------------------------------------------------------------
# History ↔ Gemini Content conversion.
# ----------------------------------------------------------------------

def _history_to_contents(turns: list[dict]) -> list[types.Content]:
    out: list[types.Content] = []
    for t in turns:
        role = "user" if t.get("role") == "user" else "model"
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        out.append(types.Content(
            role=role,
            parts=[types.Part.from_text(text=text)],
        ))
    return out


# ----------------------------------------------------------------------
# Main entry point — called from the webhook for each new inbound row.
# ----------------------------------------------------------------------

async def process_message(msg: dict, *, broadcast: Optional[Callable[[dict], None]] = None,
                          our_digits: str = "") -> None:
    """Outer wrapper around `_process_message` that GUARANTEES nothing
    raises out of an asyncio.create_task — a silent task-death is the
    worst failure mode for a webhook bot because the operator gets no
    signal. Anything that escapes is logged + broadcast as a bot_error
    event so the SPA's Activity feed shows it."""
    try:
        await _process_message(msg, broadcast=broadcast, our_digits=our_digits)
    except Exception as e:
        logger.exception("bot process_message crashed")
        _emit(broadcast, {
            "type":  "whatsapp_bot_error",
            "where": "process_message",
            "error": f"{type(e).__name__}: {e}",
        })


async def _process_message(msg: dict, *, broadcast: Optional[Callable[[dict], None]] = None,
                            our_digits: str = "") -> None:
    """Run one inbound WhatsApp message through the agent loop and send
    a reply. Best-effort — exceptions bubble to process_message for
    logging.

    `broadcast` is the live-agent service's _broadcast — when wired we
    emit `whatsapp_bot_*` events for the SPA's bot page activity feed.
    """
    if not isinstance(msg, dict):
        return

    jid = _msg_jid(msg)
    if not jid:
        logger.info("bot: skipping row without a jid")
        return

    # Dedup: Wasender (and our local webhook handler under retries) can
    # deliver the same message id twice in quick succession. Without
    # this guard the bot replies twice, and the second reply trips
    # Wasender's "1 message every 5 seconds" account-protection
    # rate-limit (HTTP 429).
    mid = _msg_id(msg)
    if mid and _is_duplicate(mid):
        logger.info("bot: skipping duplicate message id=%s jid=%s", mid, jid)
        return

    # Outbound rows (echoes of OUR sends) → never reply to ourselves.
    if _msg_from_me(msg, our_digits):
        logger.info("bot: skipping outbound echo for jid=%s", jid)
        return

    if whatsapp_contacts.is_group_jid(jid):
        logger.info("bot: skipping group jid=%s", jid)
        return

    # LIDs (WhatsApp privacy IDs) carry no phone, and the gateway won't
    # accept a LID as a /send-message destination. To reply, we need a
    # phone JID for this sender. Resolution order:
    #   1. Persistent LID cache (lid_phone_cache.json) — survives
    #      snapshot patient edits / deletes that would otherwise lose
    #      the mapping mid-session.
    #   2. Wasender push-name → patient-name match via
    #      `canonical_jid_for`. If it works, write to the cache so
    #      future arrivals are instant + survive registry changes.
    #   3. Give up + log + skip. The patient must share their phone
    #      in-band (operator can also manually pin a LID→phone via
    #      the bot's REST endpoints) before the bot can reply.
    if whatsapp_contacts.is_lid_jid(jid):
        cached = _recall_lid(jid)
        if cached:
            logger.info("bot: LID %s → %s (from persistent cache)",
                        jid, cached)
            jid = cached
        else:
            snap = load_snapshot()
            name_index = whatsapp_contacts.build_patient_name_index(
                snap.get("patients", []),
            )
            canonical = await whatsapp_contacts.canonical_jid_for(jid, name_index)
            if canonical == jid:
                push_name = await whatsapp_contacts.display_name_for(jid)
                logger.warning(
                    "bot: LID %s could not be resolved to a patient phone "
                    "(push name=%r). Reply skipped — add a patient with "
                    "name=%r and the correct phone, or POST "
                    "/api/demo/clinic/whatsapp/bot/lid-cache to pin a "
                    "mapping manually.",
                    jid, push_name, push_name,
                )
                return
            logger.info("bot: LID %s resolved → %s via name index, "
                        "caching for future arrivals", jid, canonical)
            _remember_lid(jid, canonical)
            jid = canonical
    logger.info("bot: dispatching inbound jid=%s", jid)

    phone_digits = _digits(jid.split("@", 1)[0])
    if not phone_digits:
        logger.info("bot: could not parse phone from jid=%s", jid)
        return

    # Voice FIRST, then text. Order matters: voice-note rows carry an
    # `audioMessage` block whose internal fields (mediaKey, fileEncSha256,
    # …) are long base64 strings that our `_deep_find_text` fallback in
    # `_msg_text` will happily mistake for a message body. If we let text
    # detection win on a voice row, the model gets handed gibberish and
    # responds with a generic greeting. So: if the row has an audio
    # block, treat it as a voice note unconditionally — only fall back
    # to text when there is no audio.
    text = ""
    audio_meta: Optional[dict] = None
    block = _find_audio_block(msg)
    if block:
        logger.info("bot: voice note detected, attempting transcription")
        audio_bytes, mime = await _fetch_audio_bytes(block)
        if audio_bytes:
            tx = await _transcribe_voice_note(audio_bytes, mime or "audio/ogg")
            if tx:
                text = tx
                audio_meta = {"transcribed": True, "mime": mime or "audio/ogg",
                              "bytes": len(audio_bytes)}
                logger.info("bot: voice note transcribed (%d bytes %s → %d chars)",
                            len(audio_bytes), mime, len(tx))
            else:
                audio_meta = {"transcribed": False,
                              "reason": "Gemini transcription returned empty"}
                logger.warning("bot: voice note Gemini transcription returned empty")
        else:
            audio_meta = {"transcribed": False,
                          "reason": "Could not fetch audio bytes from webhook payload"}
            logger.warning("bot: voice note audio fetch failed — "
                           "block keys=%s", list(block.keys())[:10])
    else:
        text = _msg_text(msg)
    if not text:
        # We had something media-shaped we couldn't handle. Send a
        # graceful nudge so the user isn't ignored.
        if audio_meta:
            fallback = (
                "آسف، ما قدرت أفهم الرسالة الصوتية. تقدر تكتبلي اللي تبيه نصاً؟ "
                "/ Sorry, I couldn't process that voice note. Could you type "
                "your request instead?"
            )
            _emit(broadcast, {
                "type":  "whatsapp_bot_received",
                "phone": phone_digits, "jid": jid,
                "kind":  "voice_unhandled",
                "audio": audio_meta,
            })
            await _send_reply(phone_digits, fallback)
            _record_turns(phone_digits, [
                {"role": "user",  "ts": int(time.time()),
                 "text": "[voice note — could not transcribe]",
                 "audio": audio_meta},
                {"role": "model", "ts": int(time.time()), "text": fallback},
            ])
            _emit(broadcast, {
                "type":  "whatsapp_bot_sent",
                "phone": phone_digits, "text": fallback,
                "kind":  "voice_fallback",
            })
            return
        logger.info("bot: row had neither text nor audio, skipping")
        return

    # Identify caller — set tool-context owner set so privacy checks in
    # cancel/reschedule/list_patient_appointments work as on the voice
    # path.
    identified_ids, patient = _identify_caller(phone_digits)
    caller_phone = patient.get("phone") if patient else f"+{phone_digits}"

    cfg = load_escalation_config()
    model_name = str(cfg.get("whatsapp_bot_text_model") or DEFAULT_TEXT_MODEL).strip() or DEFAULT_TEXT_MODEL

    _emit(broadcast, {
        "type":     "whatsapp_bot_received",
        "phone":    phone_digits,
        "jid":      jid,
        "text":     text,
        "audio":    audio_meta,
        "patient":  ({"id": patient.get("id"), "name": patient.get("name")}
                     if patient else None),
        "model":    model_name,
    })

    # Build conversation: persisted history + this new user message.
    prior = _read_turns(phone_digits)
    user_turn = {"role": "user", "ts": int(time.time()), "text": text}
    if audio_meta:
        user_turn["audio"] = audio_meta

    contents = _history_to_contents(prior)
    contents.append(types.Content(
        role="user",
        parts=[types.Part.from_text(text=text)],
    ))

    # Tool context — mirrors what live_agent.CallSession assembles.
    new_turns_to_persist: list[dict] = [user_turn]
    ctx: dict = {
        "call_id":                f"wabot-{phone_digits}-{int(time.time())}",
        "identified_patient_ids": set(identified_ids),
        "caller_phone":           caller_phone,
        "call_language":          _guess_lang(text),
        "broadcast":              broadcast,
        "set_flag":               lambda _flag: None,  # bot can flag but no live call to bridge
        "skip_auto_whatsapp":     True,
    }

    final_text = await _run_tool_loop(model_name, contents, ctx, broadcast,
                                       phone_digits)

    if not final_text:
        final_text = (
            "آسف، ما قدرت أرد على هذي الرسالة. حاول مرة ثانية بعد قليل. "
            "/ Sorry, I couldn't process that. Please try again shortly."
        )

    msg_id = await _send_reply(phone_digits, final_text)
    new_turns_to_persist.append({
        "role": "model", "ts": int(time.time()), "text": final_text,
        "message_id": msg_id,
    })
    _record_turns(phone_digits, new_turns_to_persist)

    _emit(broadcast, {
        "type":       "whatsapp_bot_sent",
        "phone":      phone_digits,
        "text":       final_text,
        "message_id": msg_id,
    })


def _record_turns(phone_digits: str, turns: list[dict]) -> None:
    _append_turns(phone_digits, turns)


def _emit(broadcast: Optional[Callable[[dict], None]], payload: dict) -> None:
    if not broadcast:
        return
    try:
        broadcast(payload)
    except Exception:
        logger.exception("bot: broadcast failed")


_ARABIC_RE = re.compile(r"[؀-ۿ]")


def _guess_lang(text: str) -> str:
    return "ar" if _ARABIC_RE.search(text or "") else "en"


# ----------------------------------------------------------------------
# Gemini tool-call loop.
# ----------------------------------------------------------------------

async def _run_tool_loop(model_name: str, contents: list[types.Content],
                          ctx: dict,
                          broadcast: Optional[Callable[[dict], None]],
                          phone_digits: str) -> str:
    """Run generate_content; if the response includes function calls,
    execute them, append their FunctionResponse parts, and call again.
    Returns the final plain text once the model stops requesting tools."""
    if not state.gemini_api_key:
        return ("WhatsApp bot is online but the Gemini API key isn't "
                "configured on the server — please tell the operator.")

    client = genai.Client(api_key=state.gemini_api_key)
    sys_inst = _build_system_instruction(
        patient=_lookup_patient(ctx),
        phone_digits=phone_digits,
    )
    tools = build_tools()
    gen_cfg = types.GenerateContentConfig(
        system_instruction=sys_inst,
        tools=tools,
        # Slight conservatism vs voice — text is read carefully so
        # there's less tolerance for fluff.
        temperature=0.4,
    )

    convo = list(contents)
    last_text = ""

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            resp = await client.aio.models.generate_content(
                model=model_name,
                contents=convo,
                config=gen_cfg,
            )
        except Exception as e:
            logger.exception("bot generate_content failed (iter=%d)", iteration)
            return f"Sorry, I hit an error talking to the model: {type(e).__name__}."

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            break
        cand = candidates[0]
        content = getattr(cand, "content", None)
        if content is None:
            break

        parts = list(getattr(content, "parts", None) or [])
        function_calls: list = []
        text_pieces: list[str] = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc and getattr(fc, "name", None):
                function_calls.append(fc)
                continue
            txt = getattr(p, "text", None)
            if isinstance(txt, str) and txt.strip():
                text_pieces.append(txt)

        if text_pieces:
            last_text = "\n".join(s.strip() for s in text_pieces).strip()

        if not function_calls:
            # Model gave us text-only — we're done.
            return last_text

        # Append the model turn (with its function calls) so the next
        # generate_content call sees them, then execute each call and
        # append its FunctionResponse.
        convo.append(types.Content(role="model", parts=parts))

        response_parts: list[types.Part] = []
        for fc in function_calls:
            name = fc.name
            args = dict(getattr(fc, "args", None) or {})
            try:
                result = execute_tool(name, args, ctx)
            except Exception as e:
                logger.exception("bot: tool %s raised", name)
                result = {"error": f"{type(e).__name__}: {e}"}
            if not isinstance(result, dict):
                result = {"value": result}
            _emit(broadcast, {
                "type":     "whatsapp_bot_tool",
                "phone":    phone_digits,
                "tool":     name,
                "args":     _summarise(args),
                "result":   _summarise(result),
                "iter":     iteration,
            })
            response_parts.append(types.Part.from_function_response(
                name=name, response=result,
            ))
        convo.append(types.Content(role="tool", parts=response_parts))

    # We exhausted iterations. Return whatever text we last gathered, or
    # an honest fallback.
    return last_text or (
        "I ran out of internal steps trying to handle that. "
        "Please try rephrasing or contact reception directly."
    )


def _lookup_patient(ctx: dict) -> Optional[dict]:
    ids = ctx.get("identified_patient_ids") or set()
    if not ids:
        return None
    snap = load_snapshot()
    for p in snap.get("patients", []):
        if p.get("id") in ids:
            return p
    return None


def _summarise(d: dict) -> dict:
    """Trim a dict for the activity feed — long values get shortened so
    the WS event stays compact."""
    out: dict = {}
    for k, v in (d or {}).items():
        if isinstance(v, str) and len(v) > 120:
            out[k] = v[:117] + "…"
        elif isinstance(v, list):
            out[k] = f"[{len(v)} item(s)]"
        elif isinstance(v, dict):
            out[k] = f"{{...{len(v)} keys...}}"
        else:
            out[k] = v
    return out


# ----------------------------------------------------------------------
# Webhook envelope extraction — find the OUTER message rows in a
# Wasender / Baileys-style payload (not the inner `key` block).
# ----------------------------------------------------------------------

def _extract_envelopes(payload: Any, depth: int = 0,
                        out: Optional[list[dict]] = None) -> list[dict]:
    """Walk the payload and return any dict that looks like a full
    message ENVELOPE (carries a body or media block AND a sender/jid).
    The inbox's _candidate_rows captures the inner `key` block, which
    works for storage but loses access to message.conversation. For
    the bot we need both the sender id AND the body, so we identify
    them here."""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(payload, dict):
        if _looks_like_envelope(payload):
            out.append(payload)
        else:
            for v in payload.values():
                _extract_envelopes(v, depth + 1, out)
    elif isinstance(payload, list):
        for v in payload:
            _extract_envelopes(v, depth + 1, out)
    return out


def _looks_like_envelope(d: dict) -> bool:
    """True when d has both (a) a sender/jid we can resolve and (b) a
    body or media block we can act on."""
    # (a) sender / jid present, directly or via key.remoteJid
    has_jid = False
    for k in ("remoteJid", "chatJid", "jid", "from", "sender",
              "chat_id", "chatId", "from_number", "from_jid"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            has_jid = True
            break
    if not has_jid:
        key = d.get("key")
        if isinstance(key, dict):
            for k in ("remoteJid", "jid"):
                if isinstance(key.get(k), str) and key[k].strip():
                    has_jid = True
                    break
    if not has_jid:
        return False
    # (b) body or media block present
    for k in ("text", "body", "content", "messageText", "messageContent",
              "caption", "msg_text"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, dict):
            for inner in ("text", "body", "content", "caption", "conversation"):
                vv = v.get(inner)
                if isinstance(vv, str) and vv.strip():
                    return True
    msg = d.get("message")
    if isinstance(msg, dict):
        for k in ("conversation", "text"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return True
        for k in ("extendedTextMessage", "audioMessage", "imageMessage",
                  "videoMessage", "documentMessage"):
            if isinstance(msg.get(k), dict):
                return True
    if _find_audio_block(d):
        return True
    mtype = str(d.get("messageType") or d.get("type") or "").lower()
    if mtype in ("text", "audio", "voice", "ptt", "voice_note",
                 "voicenote", "image", "video", "document"):
        return True
    return False


# ----------------------------------------------------------------------
# Spawn-from-webhook helper. Called from router.py's webhook handler.
# ----------------------------------------------------------------------

def maybe_dispatch_webhook(payload: dict, *,
                            broadcast: Optional[Callable[[dict], None]] = None,
                            our_digits: str = "") -> int:
    """Find every inbound envelope in `payload` and schedule a background
    bot task for it. Returns the count dispatched (0 when the bot is
    disabled or no envelopes were found). The dispatch decision is
    logged loudly so the operator can see in the Debug page whether the
    webhook actually triggered the bot."""
    cfg = load_escalation_config()
    if not cfg.get("whatsapp_bot_enabled"):
        logger.info("bot dispatch SKIPPED: whatsapp_bot_enabled=False")
        return 0
    envelopes = _extract_envelopes(payload)
    if not envelopes:
        # Help the operator debug Wasender-shape mismatches by showing
        # WHICH top-level keys we did see.
        keys = (list(payload.keys())[:10] if isinstance(payload, dict) else [])
        logger.warning("bot dispatch: found 0 envelopes in webhook payload "
                       "(top-level keys=%s) — Wasender payload shape may have changed, "
                       "check /whatsapp/raw for sample shapes", keys)
        _emit(broadcast, {
            "type":  "whatsapp_bot_error",
            "where": "extract_envelopes",
            "error": f"no envelopes; top-level keys={keys}",
        })
        return 0
    n = 0
    for env in envelopes:
        try:
            asyncio.create_task(process_message(env, broadcast=broadcast,
                                                our_digits=our_digits))
            n += 1
        except RuntimeError:
            threading.Thread(
                target=lambda e=env: asyncio.run(process_message(
                    e, broadcast=broadcast, our_digits=our_digits)),
                daemon=True,
            ).start()
            n += 1
    logger.info("bot dispatch: scheduled %d task(s) from %d envelope(s)",
                n, len(envelopes))
    return n
