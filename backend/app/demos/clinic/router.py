"""Clinic demo backend — login, current-user, logout, and the Live Agent
control plane (config + persona/KB publish + status). The AudioSocket
service itself lives in demos.clinic.live_agent."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio

from ...core.state import state
from . import config
from .live_agent import (
    clinic_live_agent_service,
    load_kb, load_persona, save_kb, save_persona,
    load_escalation_config, save_escalation_config,
    list_saved_calls, load_saved_call, call_audio_path, delete_saved_call,
)
from .ami import AMIService, AMICredentials
from .wasender import build_client as build_wasender_client
from . import whatsapp_inbox
from . import whatsapp_templates
from . import whatsapp_contacts
from . import whatsapp_bot
from . import main_settings as main_settings_mod
from .agent_tools import load_snapshot, save_snapshot
from ..auth import (
    clear_session,
    issue_session,
    require_session,
)

logger = logging.getLogger("demo_clinic")
router = APIRouter()

_SLUG = config.SLUG


# ============================================================================
# Login / session
# ============================================================================

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn, response: Response) -> dict:
    if body.username == config.DEMO_USERNAME and body.password == config.DEMO_PASSWORD:
        issue_session(response, _SLUG, body.username)
        return {"ok": True, "user": {"username": body.username,
                                     "display_name": config.DISPLAY_NAME}}
    raise HTTPException(401, "Invalid credentials")


@router.get("/me")
async def me(request: Request) -> dict:
    sess = require_session(request, _SLUG)
    return {"username": sess["username"], "display_name": config.DISPLAY_NAME}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    clear_session(request, response, _SLUG)
    return {"ok": True}


# ============================================================================
# Live Agent — config + prompt publish + status + activity WS
# ============================================================================

class AgentConfigIn(BaseModel):
    enabled:              Optional[bool] = None
    bind_host:            Optional[str] = None
    bind_port:            Optional[int] = None
    voice:                Optional[str] = None
    greeting:             Optional[str] = None
    max_call_s:           Optional[int] = None
    interruption_enabled: Optional[bool] = None


class AgentConfigOut(BaseModel):
    enabled:              bool
    bind_host:            str
    bind_port:            int
    voice:                str
    greeting:             str
    max_call_s:           int
    interruption_enabled: bool


def _current_agent_config() -> AgentConfigOut:
    return AgentConfigOut(
        enabled=bool(state.cda_enabled),
        bind_host=state.cda_bind_host or "0.0.0.0",
        bind_port=int(state.cda_bind_port or 8092),
        voice=state.cda_voice or "Aoede",
        greeting=state.cda_greeting or "",
        max_call_s=int(state.cda_max_call_s or 0),
        interruption_enabled=bool(state.cda_interruption_enabled),
    )


@router.get("/agent/config", response_model=AgentConfigOut)
async def agent_get_config() -> AgentConfigOut:
    return _current_agent_config()


@router.post("/agent/config", response_model=AgentConfigOut)
async def agent_set_config(payload: AgentConfigIn) -> AgentConfigOut:
    if payload.enabled              is not None: state.cda_enabled = bool(payload.enabled)
    if payload.bind_host            is not None: state.cda_bind_host = (payload.bind_host or "0.0.0.0").strip() or "0.0.0.0"
    if payload.bind_port            is not None: state.cda_bind_port = max(1, min(65535, int(payload.bind_port)))
    if payload.voice                is not None: state.cda_voice = (payload.voice or "Aoede").strip() or "Aoede"
    if payload.greeting             is not None: state.cda_greeting = payload.greeting or ""
    if payload.max_call_s           is not None: state.cda_max_call_s = max(0, int(payload.max_call_s))
    if payload.interruption_enabled is not None: state.cda_interruption_enabled = bool(payload.interruption_enabled)
    state.save()
    clinic_live_agent_service.apply_config()
    return _current_agent_config()


@router.get("/agent/status")
async def agent_status() -> dict:
    s = clinic_live_agent_service.status()
    return {
        **s,
        "calls":          clinic_live_agent_service.active_calls(),
        "persona_chars":  len(load_persona()),
        "kb_chars":       len(load_kb()),
        "api_key_set":    bool(state.gemini_api_key),
    }


class PromptIn(BaseModel):
    persona: Optional[str] = None
    kb:      Optional[str] = None


@router.get("/agent/prompt")
async def agent_get_prompt() -> dict:
    return {"persona": load_persona(), "kb": load_kb()}


@router.post("/agent/prompt")
async def agent_set_prompt(payload: PromptIn) -> dict:
    """Persist persona / KB to data/demos/clinic/{persona,kb}.txt. Service
    re-reads on every call so the next inbound dial picks up the change
    with no restart needed."""
    if payload.persona is not None: save_persona(payload.persona)
    if payload.kb      is not None: save_kb(payload.kb)
    return {"ok": True, "persona": load_persona(), "kb": load_kb()}


# ----- Supervisor escalation (red-flag) ------------------------------------

@router.get("/agent/escalation")
async def agent_get_escalation() -> dict:
    """Return the operator-editable escalation triggers: keywords (EN/AR),
    free-text scenarios, and auto-detect tunables. Read by the
    Configuration page; also injected into the live persona on every
    new inbound call."""
    return load_escalation_config()


@router.post("/agent/escalation")
async def agent_set_escalation(payload: dict) -> dict:
    """Partial update — merges `payload` into the saved config and
    returns the resulting full config. Unknown keys are ignored; the
    service re-reads from disk on every inbound call so the next dial
    picks up the change with no restart."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "escalation config must be a JSON object")
    return save_escalation_config(payload)


@router.post("/agent/calls/{call_id}/acknowledge_flag")
async def agent_ack_flag(call_id: str) -> dict:
    """Operator clicks the Acknowledge button on a flagged row →
    clears the flag and broadcasts a supervisor_flag_ack event so
    every other dashboard instance drops the red tint in sync."""
    call = clinic_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    if not call.ack_flag():
        return {"ok": True, "noop": True}
    return {"ok": True}


# ----- AMI-backed supervisor dial-in ----------------------------------------
# Operator clicks the "Ext. 1003" button on a live-call row. We issue an
# AMI Originate so Asterisk rings the supervisor's extension — no softphone
# `tel:` handler, no copy/paste. AMI credentials come from escalation.json
# (editable from Call Center → Configuration → PBX integration).

def _load_ami_credentials() -> AMICredentials:
    cfg = load_escalation_config()
    return AMICredentials(
        host=str(cfg.get("ami_host") or "").strip(),
        port=int(cfg.get("ami_port") or 5038),
        username=str(cfg.get("ami_username") or "").strip(),
        secret=str(cfg.get("ami_secret") or "").strip(),
    )


_ami_service = AMIService(_load_ami_credentials)


@router.post("/agent/calls/{call_id}/dial_supervisor")
async def agent_dial_supervisor(call_id: str, mode: str = "barge") -> dict:
    """Trigger an AMI Originate to ring the configured supervisor
    extension and drop them into the live call via ChanSpy.

    `mode` query param picks the audio policy:
      - listen  : silent monitor
      - whisper : talk to the caller only (AI doesn't hear)
      - barge   : 3-way (default — supervisor talks to caller + AI)

    Reads AMI host / username / secret / port + supervisor_extension
    from escalation.json on every call so edits from the Configuration
    page take effect with no restart."""
    call = clinic_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    cfg = load_escalation_config()
    extension = str(cfg.get("supervisor_extension") or "").strip()
    result = await _ami_service.dial_supervisor(call_id, extension, spy_mode=mode)
    # 200 with ok=False on AMI failure so the UI can show the message
    # without spawning a network-error toast — body carries the detail.
    return result


# ----- WhatsApp via WasenderApi --------------------------------------------
# Per-machine API key lives in escalation.json (gitignored). Reads on every
# request so editing in Call Center → Configuration takes effect with no
# restart. See backend/app/demos/clinic/wasender.py for the wrapper.

class WhatsAppSendIn(BaseModel):
    to:   str    # any reasonable phone-number shape — the wrapper normalises
    text: str


@router.get("/whatsapp/status")
async def whatsapp_status() -> dict:
    """Lightweight key-validity ping. The SPA pings this on page load
    so the operator sees 'connected' / 'not configured' / 'error: …'
    without sending a real message. Returns:
      {configured: bool, ok: bool, status: int, error?: str, contact_count?: int}
    """
    cfg = load_escalation_config()
    api_key  = str(cfg.get("wasender_api_key") or "").strip()
    personal = str(cfg.get("wasender_personal_token") or "").strip()
    if not api_key:
        return {"configured": False, "ok": False, "status": 0,
                "error": "WhatsApp API key not configured"}
    client = build_wasender_client(api_key, personal)
    result = await client.ping()
    # Expose the PAT-availability flag so the SPA can grey out the
    # inbox tab with a useful hint instead of just showing the raw
    # Wasender error.
    inbox = whatsapp_inbox.inbox_stats()
    return {
        "configured": True,
        "personal_token_configured": bool(personal),
        "inbox_stored":   inbox["count"],
        "inbox_last_ts":  inbox["last_ts"],
        **result,
    }


# --- Incoming-message webhook ---------------------------------------------
# WasenderApi's /whatsapp-sessions/{id}/message-logs only returns OUTBOUND
# messages — incoming arrives via webhooks the operator wires up on the
# WasenderApi dashboard (Webhooks → Add webhook → URL: this endpoint,
# events: messages.upsert / Message Received). We accept whatever Wasender
# posts, deep-search for message rows, and persist them locally so the
# inbox UI can merge them with the outbound logs.

@router.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        # Some Wasender variants post form-encoded; fall back to raw.
        try:
            body = (await request.body()).decode("utf-8", "ignore")
            payload = json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body}
        except Exception:
            payload = {}
    safe_payload = payload if isinstance(payload, dict) else {"payload": payload}
    added = whatsapp_inbox.store_webhook(safe_payload)
    if added:
        logger.info("WhatsApp webhook stored %d new message(s)", added)
    # Fan the newly-arrived inbound envelopes into the bot — best effort,
    # gated on the operator's toggle. The bot walks the FULL payload itself
    # so it sees the outer message envelope (with text + audio blocks),
    # not the inner `key` row that the inbox's _candidate_rows captures.
    dispatched = 0
    try:
        dispatched = whatsapp_bot.maybe_dispatch_webhook(
            safe_payload,
            broadcast=clinic_live_agent_service._broadcast,
        )
    except Exception:
        logger.exception("WhatsApp webhook → bot dispatch failed (non-fatal)")
    return {"ok": True, "added": added, "bot_dispatched": dispatched}


@router.get("/whatsapp/inbox")
async def whatsapp_inbox_dump(limit: int = 50) -> dict:
    """Read-back of the locally-stored incoming messages — useful for
    confirming the Wasender webhook is actually hitting us."""
    rows = whatsapp_inbox.list_inbox()
    return {
        "ok":    True,
        "count": len(rows),
        "rows":  rows[-max(1, min(500, int(limit))):],
    }


@router.delete("/whatsapp/inbox")
async def whatsapp_inbox_clear() -> dict:
    removed = whatsapp_inbox.clear_inbox()
    return {"ok": True, "removed": removed}


# --- WhatsApp templates ----------------------------------------------------
# Five pre-canned message templates the Live Agent fires during a call
# (e.g. confirm a booking, share the clinic's location). Stored merged
# with the defaults on every GET; only operator-edited bodies persist to
# disk so the JSON file stays small + diffable.

class WhatsAppTemplatesIn(BaseModel):
    # {template_id: {en?: str, ar?: str}} — partial updates allowed.
    templates: dict


@router.get("/whatsapp/templates")
async def whatsapp_templates_get() -> dict:
    """Returns the live (defaults + operator overrides) view of every
    template plus the metadata the SPA renders (name, description,
    variables)."""
    resolved = whatsapp_templates.resolved_templates()
    return {
        "ok":        True,
        "order":     list(whatsapp_templates.TEMPLATE_ORDER),
        "templates": resolved,
    }


@router.post("/whatsapp/templates")
async def whatsapp_templates_set(payload: WhatsAppTemplatesIn) -> dict:
    """Persist operator edits. Body shape:
        { templates: { "<template_id>": { en?: str, ar?: str } } }
    Returns the merged live view so the SPA can refresh from the
    response without a follow-up GET."""
    if not isinstance(payload.templates, dict):
        raise HTTPException(400, "templates must be an object")
    whatsapp_templates.save_overrides(payload.templates)
    return {
        "ok":        True,
        "templates": whatsapp_templates.resolved_templates(),
    }


# Public per-machine directory where operator-uploaded media is stored.
# Files here are served by the /whatsapp/outgoing/{name} endpoint and
# Wasender fetches them by URL when we call send-message with imageUrl /
# videoUrl / documentUrl / audioUrl. Lives under data/ so it's gitignored
# automatically (PII: caller phones + the media they receive).
_OUTGOING_DIR = (Path(__file__).resolve().parents[4]
                 / "data" / "demos" / "clinic" / "whatsapp_outgoing")


def _detect_media_kind(content_type: str, filename: str) -> str:
    """Map an upload's content_type / extension onto Wasender's
    messageType enum. Returns 'image' | 'video' | 'audio' | 'document'.
    Documents is the safe catch-all."""
    ct = (content_type or "").lower().split(";")[0].strip()
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ct.startswith("image/") or ext in {"jpg", "jpeg", "png", "gif", "webp"}:
        return "image"
    if ct.startswith("video/") or ext in {"mp4", "mov", "webm", "mkv", "m4v"}:
        return "video"
    if ct.startswith("audio/") or ext in {"mp3", "ogg", "m4a", "aac", "opus", "wav"}:
        return "audio"
    return "document"


@router.post("/whatsapp/send-media")
async def whatsapp_send_media(
    request: Request,
    to:      str = Form(...),
    caption: str = Form(""),
    file:    UploadFile = File(...),
) -> dict:
    """Send an image / video / audio / document via WasenderApi.

    Wasender's send-message endpoint takes a public URL for each media
    type (imageUrl / videoUrl / audioUrl / documentUrl). We save the
    upload under data/demos/clinic/whatsapp_outgoing/, then construct
    the public URL using the request's own host so the file is
    reachable through the Cloudflare tunnel (clinicmac.primewave2.tech).
    If you're testing on bare localhost the URL will be unreachable by
    Wasender — that's flagged in the response.
    """
    if not (to or "").strip():
        raise HTTPException(400, "to is required")
    if whatsapp_contacts.is_lid_jid(to):
        return {
            "ok":    False,
            "status": 0,
            "error": "Cannot send to a WhatsApp LID (privacy ID). Need a phone.",
        }
    if not file or not file.filename:
        raise HTTPException(400, "file is required")
    cfg = load_escalation_config()
    api_key = str(cfg.get("wasender_api_key") or "").strip()
    if not api_key:
        return {"ok": False, "status": 0,
                "error": "WhatsApp API key not configured."}

    # Save under a randomised filename to prevent collisions + simple
    # path-traversal proof. Keep the original extension so MIME sniffing
    # on Wasender's / WhatsApp's side works.
    import uuid as _uuid
    ext = Path(file.filename).suffix.lower()[:10] or ".bin"
    safe_name = f"{_uuid.uuid4().hex}{ext}"
    _OUTGOING_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUTGOING_DIR / safe_name
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    out_path.write_bytes(data)

    # Build the public URL Wasender will fetch from. Prefer the operator's
    # configured public base URL (set on Call Center → Configuration); the
    # request's own host only works when the operator is currently
    # browsing via the public tunnel hostname. Falling back to the
    # request URL means "send-media works from the same hostname that
    # serves the SPA" but breaks for local-browser access.
    rel_path = f"/api/demo/clinic/whatsapp/outgoing/{safe_name}"
    public_base = str(cfg.get("public_base_url") or "").rstrip("/").strip()
    if public_base and public_base.startswith(("http://", "https://")):
        public_url = f"{public_base}{rel_path}"
    else:
        public_url = str(request.url_for("whatsapp_serve_outgoing",
                                          filename=safe_name))
    private_host = any(
        s in public_url for s in (
            "localhost", "127.0.0.1", "://10.", "://192.168.",
            "://172.16.", "://172.17.", "://172.18.", "://172.19.",
            "://172.2", "://172.30.", "://172.31.",
        )
    )
    if private_host:
        return {
            "ok":    False,
            "status": 0,
            "url":    public_url,
            "error": (
                "The media URL the backend would hand to Wasender "
                f"({public_url}) points at a private/loopback address that "
                "Wasender's servers cannot reach from the public internet. "
                "Open Call Center → Configuration and set "
                "'Public base URL' to your Cloudflare tunnel "
                "(e.g. https://clinicmac.primewave2.tech), then retry."
            ),
        }

    # Pick the messageType + URL field WasenderApi expects.
    kind = _detect_media_kind(file.content_type or "", file.filename)
    body: dict = {"to": to, "messageType": kind}
    if kind == "image":
        body["imageUrl"] = public_url
        if caption.strip(): body["text"] = caption.strip()
    elif kind == "video":
        body["videoUrl"] = public_url
        if caption.strip(): body["text"] = caption.strip()
    elif kind == "audio":
        body["audioUrl"] = public_url
    else:
        body["documentUrl"] = public_url
        body["fileName"]    = file.filename
        if caption.strip(): body["text"] = caption.strip()

    logger.info("send-media: kind=%s to=%s file=%s (%d bytes) url=%s",
                kind, to, file.filename, len(data), public_url)
    import httpx as _hx
    try:
        async with _hx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://www.wasenderapi.com/api/send-message",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type":  "application/json",
                         "Accept":        "application/json"},
                json=body,
            )
    except _hx.RequestError as e:
        return {"ok": False, "status": 0, "error": f"network: {e}"}
    try:
        payload = r.json()
    except Exception:
        payload = {"raw": (r.text or "")[:500]}
    ok = 200 <= r.status_code < 300 and not (
        isinstance(payload, dict) and payload.get("success") is False
    )
    data_field = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    mid = data_field.get("msgId") or data_field.get("message_id")
    return {
        "ok":          ok,
        "status":      r.status_code,
        "message_id":  mid,
        "kind":        kind,
        "url":         public_url,
        "error":       (payload.get("message") or payload.get("error")
                        if not ok and isinstance(payload, dict) else None),
        "raw":         payload,
    }


@router.get("/whatsapp/outgoing/{filename}", name="whatsapp_serve_outgoing")
async def whatsapp_serve_outgoing(filename: str):
    """Public-by-design endpoint that hands Wasender (and recipients
    fetching previews) the operator-uploaded media. The path is
    deliberately not session-gated — if it were, Wasender's servers
    couldn't fetch the file at all."""
    if "/" in filename or ".." in filename or len(filename) > 200:
        raise HTTPException(400, "invalid filename")
    p = _OUTGOING_DIR / filename
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p))


@router.post("/whatsapp/send")
async def whatsapp_send(payload: WhatsAppSendIn) -> dict:
    """Send a WhatsApp text via WasenderApi. The API key comes from
    escalation.json (set on Call Center → Configuration). Returns
    {ok, message_id?, status, error?, raw}. 200 with ok=False on any
    upstream failure so the UI can display the message in-band."""
    if not (payload.text or "").strip():
        raise HTTPException(400, "text is required")
    if not (payload.to or "").strip():
        raise HTTPException(400, "to is required")
    # Refuse LID targets up front — Wasender's send-message endpoint
    # expects a phone number, and the LID doesn't resolve to one in
    # this account's contact data. Better to surface the impossibility
    # than send to "233908264300569" thinking it's a Ghana mobile.
    if whatsapp_contacts.is_lid_jid(payload.to):
        return {
            "ok":    False,
            "status": 0,
            "error": (
                "Cannot send to a WhatsApp LID (privacy ID). The sender "
                "hasn't exposed their phone number — replies via the "
                "send-message API need a real phone. Wait for them to "
                "message you with their phone in-band, or ask them to "
                "disable WhatsApp Business / privacy ID mode."
            ),
        }
    cfg = load_escalation_config()
    api_key  = str(cfg.get("wasender_api_key") or "").strip()
    personal = str(cfg.get("wasender_personal_token") or "").strip()
    client = build_wasender_client(api_key, personal)
    return await client.send_text(payload.to, payload.text)


# Inbox endpoints — both go through WasenderApi message-logs and group
# the response client-side. /chats lists conversations (one row per
# JID); /messages?jid=… returns the timeline for a single conversation.

def _msg_jid(m: dict) -> str:
    """Pick the chat-key (remoteJid) from a message-log row. WasenderApi
    has used several different field names across plans + versions —
    accept all the candidates we've seen, and as a last resort scan
    string values for an `@s.whatsapp.net` / `@g.us` suffix and take
    the first one that isn't our own paired number."""
    # Direct keys.
    for k in ("remoteJid", "chatJid", "jid", "chat_id", "chatId",
              "from", "to", "sender", "recipient", "phone", "number"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            # Normalise bare-digits to a JID so grouping is consistent.
            if "@" not in v and v.strip().isdigit():
                return f"{v.strip()}@s.whatsapp.net"
            return v.strip()
    # Nested key.{remoteJid}
    key = m.get("key")
    if isinstance(key, dict):
        v = key.get("remoteJid") or key.get("jid")
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Deep fallback — any string that looks like a JID.
    def walk(node, depth: int = 0) -> str:
        if depth > 5: return ""
        if isinstance(node, str) and ("@s.whatsapp.net" in node or "@g.us" in node):
            return node
        if isinstance(node, dict):
            for v in node.values():
                r = walk(v, depth + 1)
                if r: return r
        elif isinstance(node, list):
            for v in node:
                r = walk(v, depth + 1)
                if r: return r
        return ""
    return walk(m)


def _msg_ts(m: dict) -> int:
    """Best-effort epoch-seconds timestamp. Falls back to 0 so unparseable
    rows don't crash the sort."""
    for k in ("messageTimestamp", "timestamp", "ts",
              "createdAt", "created_at", "received_at"):
        v = m.get(k)
        if v is None:
            continue
        try:
            n = int(v)
            # Some APIs use milliseconds — heuristic: if it's > year 3000 in
            # seconds, treat as ms.
            if n > 32_000_000_000:
                n //= 1000
            return n
        except Exception:
            # ISO string? Cheap parse without dateutil.
            try:
                from datetime import datetime
                return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
            except Exception:
                continue
    return 0


# Keys we'll never accept as the message body even if they happen to
# contain a string. Anything else string-shaped is a candidate.
_NON_TEXT_KEYS = {
    "id", "messageId", "message_id", "key", "remoteJid", "chatJid",
    "jid", "from", "to", "sender", "recipient", "phone", "number",
    "messageTimestamp", "timestamp", "ts", "createdAt", "created_at",
    "updatedAt", "updated_at", "sessionId", "session_id", "session",
    "status", "ack", "direction", "fromMe", "from_me",
    "messageType", "type", "kind", "mediaType",
    "remoteJidServer", "participant",
}


def _deep_find_text(node, depth: int = 0) -> str:
    """Recursively dig for a plausible message body inside an arbitrary
    JSON shape. Returns the LONGEST non-whitelisted string found —
    real message bodies are almost always the largest free-text value
    in the row."""
    if depth > 6:
        return ""
    best = ""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _NON_TEXT_KEYS:
                continue
            if isinstance(v, str):
                # Skip ISO timestamps + JIDs + WhatsApp IDs (heuristic
                # — they look stringy but aren't message bodies).
                stripped = v.strip()
                if not stripped: continue
                if "@s.whatsapp.net" in v or "@g.us" in v: continue
                if len(stripped) > len(best):
                    best = stripped
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
    """Pull the displayable text out of a message-log row. Tries the
    common explicit paths first, then falls back to a recursive deep
    search for the longest plausible string. Returns the text, or a
    `[type]` tag for media-only messages, or empty string."""
    # Direct text field on common shapes (Wasender flat + a few common
    # third-party shapes).
    for k in ("text", "body", "message", "content",
              "messageText", "messageContent", "message_body",
              "text_body", "msg", "msg_text", "caption"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v
        # Some shapes nest the actual body one level deeper:
        if isinstance(v, dict):
            for inner in ("text", "body", "content", "caption", "conversation"):
                vv = v.get(inner)
                if isinstance(vv, str) and vv.strip():
                    return vv
    # Baileys-ish: message.{conversation,extendedTextMessage.text,…}
    msg = m.get("message")
    if isinstance(msg, dict):
        for k in ("conversation", "extendedTextMessage", "text"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                txt = v.get("text") or v.get("caption")
                if isinstance(txt, str) and txt.strip():
                    return txt
        # Image / video / document → show a placeholder
        for kind in ("imageMessage", "videoMessage", "audioMessage",
                     "documentMessage", "stickerMessage"):
            if kind in msg:
                caption = (msg[kind] or {}).get("caption") if isinstance(msg[kind], dict) else None
                label = kind.replace("Message", "")
                return f"[{label}]" + (f" {caption}" if caption else "")

    # Recursive fallback — covers Wasender variants we haven't enumerated
    # explicitly. Cheap because rows are small.
    deep = _deep_find_text(m)
    if deep:
        return deep

    mtype = m.get("messageType") or m.get("type")
    if mtype:
        return f"[{mtype}]"
    return ""


_OUTBOUND_STATUSES = {"sent", "delivered", "read", "playing", "played", "pending"}


def _msg_from_me(m: dict, our_digits: str = "") -> bool:
    """Heuristics for which side sent the message — tried in order of
    increasing fragility.

    `our_digits` is the digits-only form of our paired WhatsApp number,
    auto-detected from the batch (see _detect_our_phone). When supplied
    it lets us settle the direction by comparing m.from to our number,
    which is much more reliable than the other signals."""
    # 1. Explicit boolean fields.
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
    # 2. Direction-marker string fields.
    direction = str(m.get("direction") or m.get("dir")
                    or m.get("messageDirection") or m.get("flow") or "").lower()
    if direction in ("out", "outgoing", "outbound", "sent", "send"):
        return True
    if direction in ("in", "incoming", "inbound", "received", "receive"):
        return False
    # 3. Status-based — only outbound rows ever reach delivered/read.
    status = str(m.get("status") or m.get("messageStatus") or "").lower()
    if status in _OUTBOUND_STATUSES:
        return True
    # 4. Compare m.from digits to our paired number's digits.
    if our_digits:
        import re as _re_inner
        for k in ("from", "sender", "from_number", "fromPhone", "from_jid"):
            v = m.get(k)
            if not isinstance(v, str): continue
            d = _re_inner.sub(r"\D", "", v)
            if d:
                return d == our_digits
    return False


def _detect_our_phone(items: list) -> str:
    """Across all message rows, the phone number that appears in the
    most rows is almost certainly our paired WhatsApp number — it's in
    every conversation, the partner numbers each appear in only one.
    Returns digits-only, or "" if we can't tell."""
    from collections import Counter
    import re as _re_inner
    counts: Counter = Counter()
    for m in items or []:
        if not isinstance(m, dict): continue
        for key in ("from", "to", "sender", "recipient",
                    "from_number", "to_number", "from_jid", "to_jid"):
            v = m.get(key)
            if isinstance(v, str):
                d = _re_inner.sub(r"\D", "", v)
                if len(d) >= 8:    # ignore obvious non-phone numerics
                    counts[d] += 1
        # Also check key.remoteJid + key.participant if present.
        key_obj = m.get("key")
        if isinstance(key_obj, dict):
            for kk in ("remoteJid", "participant"):
                v = key_obj.get(kk)
                if isinstance(v, str):
                    d = _re_inner.sub(r"\D", "", v)
                    if len(d) >= 8:
                        counts[d] += 1
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _jid_display_name(jid: str) -> str:
    """Strip WhatsApp's @s.whatsapp.net / @g.us suffix for display."""
    if not jid:
        return "(unknown)"
    if "@" in jid:
        return jid.split("@", 1)[0]
    return jid


@router.get("/whatsapp/chats")
async def whatsapp_chats(limit: int = 300) -> dict:
    """Group the latest `limit` message-logs by remoteJid, return one
    row per conversation sorted by most-recent activity. Used by the
    inbox left pane.

    Returns {ok, chats: [{jid, name, last_text, last_ts, last_from_me,
    msg_count}, ...], error?}.
    """
    cfg = load_escalation_config()
    api_key    = str(cfg.get("wasender_api_key") or "").strip()
    personal   = str(cfg.get("wasender_personal_token") or "").strip()
    session_id = str(cfg.get("wasender_session_id") or "").strip()
    client = build_wasender_client(api_key, personal)
    result = await client.list_messages(session_id, limit=limit)
    if not result.get("ok"):
        return {"ok": False, "chats": [], "error": result.get("error")}

    # Outbound (from Wasender's message-logs) + inbound (webhook-fed,
    # stored locally). Wasender's logs are sent-only — without the
    # local inbox the operator would never see incoming messages.
    outbound = result.get("items") or []
    inbound  = whatsapp_inbox.list_inbox()
    items    = list(outbound) + list(inbound)
    our_digits = _detect_our_phone(items)

    # Patient-name index — used to fold a LID chat into the patient's
    # phone chat when the LID's push name matches a patient on record.
    # Same WhatsApp user often appears under two JIDs (phone for our
    # outbound, LID for their inbound when privacy mode is on); this
    # stitches them back together.
    from .agent_tools import load_snapshot as _load_snap
    patients_for_merge = (_load_snap() or {}).get("patients") or []
    name_index = whatsapp_contacts.build_patient_name_index(patients_for_merge)

    grouped: dict[str, dict] = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        jid = _msg_jid(m)
        if not jid:
            continue
        # Fold this row into its canonical JID if we can prove it's
        # the same person — see whatsapp_contacts.canonical_jid_for.
        canon = await whatsapp_contacts.canonical_jid_for(jid, name_index)
        if canon != jid:
            jid = canon
        ts   = _msg_ts(m)
        text = _msg_text(m)
        rec = grouped.setdefault(jid, {
            "jid":           jid,
            "name":          _jid_display_name(jid),
            "display_name":  None,   # push-name from contacts cache (filled below)
            "is_lid":        whatsapp_contacts.is_lid_jid(jid),
            "is_group":      whatsapp_contacts.is_group_jid(jid),
            "last_text":     "",
            "last_ts":       0,
            "last_from_me":  False,
            "msg_count":     0,
        })
        rec["msg_count"] += 1
        if ts >= rec["last_ts"]:
            rec["last_ts"]      = ts
            rec["last_text"]    = text
            rec["last_from_me"] = _msg_from_me(m, our_digits)

    # Fill in display_name from Wasender's contacts cache so a LID
    # chat renders as the contact's push name ("Ahmed") instead of
    # the raw 15-digit LID. Non-LID phone JIDs also benefit when the
    # contact has a saved name.
    for rec in grouped.values():
        pretty = await whatsapp_contacts.display_name_for(rec["jid"])
        if pretty:
            rec["display_name"] = pretty

    chats = sorted(grouped.values(), key=lambda x: x["last_ts"], reverse=True)
    return {"ok": True, "chats": chats, "our_digits": our_digits,
            "total_messages": len(items)}


@router.get("/whatsapp/messages")
async def whatsapp_messages(jid: str, limit: int = 300) -> dict:
    """Return all message-log rows for one conversation, normalised and
    sorted oldest→newest so the SPA can render them as a chat thread."""
    if not jid:
        raise HTTPException(400, "jid is required")
    cfg = load_escalation_config()
    api_key    = str(cfg.get("wasender_api_key") or "").strip()
    personal   = str(cfg.get("wasender_personal_token") or "").strip()
    session_id = str(cfg.get("wasender_session_id") or "").strip()
    client = build_wasender_client(api_key, personal)
    result = await client.list_messages(session_id, limit=limit)
    if not result.get("ok"):
        return {"ok": False, "messages": [], "error": result.get("error")}

    # Compare on digits-only — a message row might store the JID as
    # "9665…@s.whatsapp.net", "9665…", or "+9665 …" depending on the
    # endpoint. Matching on digits sidesteps all of that.
    import re as _re
    def _digits(s: str) -> str:
        return _re.sub(r"\D", "", s or "")
    target_digits = _digits(jid)
    # Same merge as /whatsapp/chats — Wasender's logs are outbound-only
    # so we splice the locally-stored inbound rows in here.
    outbound = result.get("items") or []
    inbound  = whatsapp_inbox.list_inbox()
    items    = list(outbound) + list(inbound)
    # Auto-detect our paired number across the WHOLE batch (not just
    # rows that match this chat) — gives the from/to comparison its
    # best chance of being right.
    our_digits = _detect_our_phone(items)

    # Same LID→phone canonicalisation as /chats — so opening the
    # merged phone-JID chat pulls in messages that arrived under the
    # LID. Otherwise the operator clicks "Zeyad Ibrahim" (now the
    # merged chat under 966591697226@s.whatsapp.net) and only sees
    # the outbound half because the LID rows still carry the LID jid.
    from .agent_tools import load_snapshot as _load_snap
    patients_for_merge = (_load_snap() or {}).get("patients") or []
    name_index = whatsapp_contacts.build_patient_name_index(patients_for_merge)

    # Read the bot's outbound msgId ledger once per request so each
    # outbound row can be flagged sent_by_ai without doing per-row file I/O.
    ai_ids = whatsapp_bot.ai_sent_ids()

    rows = []
    for m in items:
        if not isinstance(m, dict):
            continue
        row_jid = _msg_jid(m)
        # Canonicalise THIS row's JID. If it's a LID that maps to a
        # phone, treat it as if it had arrived under that phone.
        canon_row = await whatsapp_contacts.canonical_jid_for(row_jid, name_index)
        if _digits(canon_row) != target_digits and canon_row != jid and \
           _digits(row_jid) != target_digits and row_jid != jid:
            continue
        msg_id = m.get("id") or (m.get("key") or {}).get("id")
        # Surface any inbound media (voice note, image, video, document)
        # as a `media_url` so the SPA can render the appropriate player /
        # preview / download link. The endpoint behind that URL fetches
        # the file from Wasender, decrypts it with the mediaKey if
        # needed, and caches the plaintext on disk.
        media_url = None
        media_type = None
        media_filename = None
        if msg_id:
            _block, _kind, _fname = whatsapp_bot._find_media_block(m)
            if _block and _kind:
                media_url = f"/api/demo/clinic/whatsapp/media/{msg_id}"
                media_type = _kind
                media_filename = _fname
        from_me = _msg_from_me(m, our_digits)
        rows.append({
            "id":            msg_id,
            "ts":            _msg_ts(m),
            "from_me":       from_me,
            "text":          _msg_text(m),
            # Raw type tag for icons / styling
            "type":          m.get("messageType") or m.get("type") or "text",
            "media_type":    media_type,
            "media_url":     media_url,
            "media_filename": media_filename,
            # True when the bot (not the operator at the WhatsApp page)
            # sent this outbound row. Frontend renders an "AI" badge so
            # the operator can distinguish hand-typed replies from
            # bot-handled ones at a glance.
            "sent_by_ai":    bool(from_me and msg_id and str(msg_id) in ai_ids),
        })
    rows.sort(key=lambda r: r["ts"])
    return {"ok": True, "messages": rows, "count": len(rows),
            "our_digits": our_digits}


# Map media kind → a sensible extension for the disk cache filename. We
# don't trust the mimetype's extension because Wasender often gives
# `application/octet-stream` for audio and "bin" for everything else.
_EXT_FOR_KIND = {
    "audio":    "ogg",
    "image":    "jpg",
    "video":    "mp4",
    "document": "bin",   # overridden by the block's fileName when present
    "sticker":  "webp",
}


@router.get("/whatsapp/media/{message_id}")
async def whatsapp_media(message_id: str):
    """Return the decrypted media bytes for an inbound WhatsApp row.
    Handles every kind of media — voice notes, images, videos,
    documents — by routing to `whatsapp_bot._fetch_media_bytes` with
    the right kind. WhatsApp media is E2E-encrypted with the `mediaKey`
    carried in the message block, so the raw URL Wasender gives us is
    a ciphertext blob that has to be HKDF+AES decrypted first.
    Decrypted bytes get cached on disk because Wasender's presigned
    URLs expire quickly and re-fetching wouldn't even work after a few
    minutes."""
    if not message_id or len(message_id) > 128 or "/" in message_id \
            or ".." in message_id:
        raise HTTPException(400, "invalid message_id")
    cache_dir = Path(__file__).resolve().parents[4] / "data" / "demos" / "clinic" / "whatsapp_media"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Look up the inbound row so we know what kind of media this is
    # AND what filename to advertise to the browser (documents come
    # with a human-readable `fileName` we want to preserve).
    target = None
    for row in whatsapp_inbox.list_inbox():
        rid = row.get("id") or (row.get("key") or {}).get("id")
        if rid == message_id:
            target = row
            break
    if not target:
        raise HTTPException(404, "message not found in inbox")
    block, kind, fname = whatsapp_bot._find_media_block(target)
    if not block or not kind:
        raise HTTPException(404, "message has no media block")

    # Cache key per (id, kind, ext). Reuse the document's original
    # filename for the on-disk extension when we have one — picture-
    # perfect downloads for PDFs / DOCX / etc.
    ext = _EXT_FOR_KIND.get(kind, "bin")
    if kind == "document" and fname and "." in fname:
        ext = fname.rsplit(".", 1)[-1].lower()[:8] or "bin"
    cache_path = cache_dir / f"{message_id}.{ext}"
    download_name = fname if (kind == "document" and fname) else f"{message_id}.{ext}"

    mime_from_block = whatsapp_bot._mime_from_block(block, kind)
    if cache_path.exists():
        return FileResponse(
            str(cache_path), media_type=mime_from_block,
            filename=download_name,
        )

    media_bytes, mime = await whatsapp_bot._fetch_media_bytes(block, kind)
    if not media_bytes:
        raise HTTPException(502, "media fetch or decrypt failed — "
                                 "check the bot debug log")
    if whatsapp_bot._looks_like_media(media_bytes, kind):
        cache_path.write_bytes(media_bytes)
    else:
        logger.warning("whatsapp_media: bytes don't match expected %s "
                        "container for id=%s — serving uncached",
                        kind, message_id)
    return Response(
        content=media_bytes,
        media_type=(mime or mime_from_block),
        headers={"Content-Disposition":
                 f'inline; filename="{download_name}"'},
    )


@router.get("/whatsapp/raw_contacts")
async def whatsapp_raw_contacts(limit: int = 50) -> dict:
    """Dump Wasender's raw /contacts response. Diagnostic only — used
    to figure out which fields are in a contact (jid? phone? lid?
    push_name?) so we can resolve incoming LIDs to real phone numbers."""
    import httpx as _hx
    cfg = load_escalation_config()
    api_key  = str(cfg.get("wasender_api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "wasender_api_key not configured"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
    }
    try:
        async with _hx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                "https://www.wasenderapi.com/api/contacts",
                headers=headers,
            )
    except _hx.RequestError as e:
        return {"ok": False, "error": f"network: {e}"}
    try:
        payload = r.json()
    except Exception:
        return {"ok": False, "status": r.status_code,
                "raw_text": r.text[:500]}
    # Try to slice the actual contact array out for a compact response,
    # but also echo the wrapper so we can see pagination / count fields.
    contacts = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(contacts, list):
        contacts = payload if isinstance(payload, list) else []
    return {
        "ok":           200 <= r.status_code < 300,
        "status":       r.status_code,
        "wrapper_keys": list(payload.keys()) if isinstance(payload, dict) else None,
        "count":        len(contacts),
        "items":        contacts[:max(1, min(50, int(limit)))],
    }


@router.get("/whatsapp/raw")
async def whatsapp_raw(limit: int = 5) -> dict:
    """Return up to `limit` message-log rows VERBATIM from WasenderApi —
    bypasses our normalisation. Used to diagnose "messages show as
    [text]", "sender shown as LID", "reply doesn't appear" issues —
    Wasender's payload shape varies by plan tier and the only way to
    know what your account returns is to look at the raw row.

    Honours the upstream Wasender pagination — pass `limit=500` and
    we'll bump our internal request to match. Wasender itself may
    still page-cap (default ~10/page on Laravel paginators); the
    `raw` property of the inner result echoes whatever wrapper Wasender
    sent so you can see pagination metadata."""
    cfg = load_escalation_config()
    api_key    = str(cfg.get("wasender_api_key") or "").strip()
    personal   = str(cfg.get("wasender_personal_token") or "").strip()
    session_id = str(cfg.get("wasender_session_id") or "").strip()
    client = build_wasender_client(api_key, personal)
    # Bumped from min(20) → min(500) so the operator can see enough
    # rows to spot the inbound LID row (Wasender's first page is
    # usually 10 outbound rows for a fresh setup).
    n = max(1, min(500, int(limit)))
    result = await client.list_messages(session_id, limit=n)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    items = (result.get("items") or [])[:n]
    return {
        "ok":          True,
        "count":       len(items),
        "items":       items,
        # Pagination metadata: lets the operator see if there are
        # more pages (last_page, total, next_page_url, etc.) when
        # Wasender wraps results.
        "raw_wrapper": result.get("raw"),
    }


# ----- WhatsApp Bot --------------------------------------------------------
# Read-only views over data/demos/clinic/whatsapp_conversations.json so the
# SPA's Bot page can show per-phone histories and a status pill, plus a
# toggle endpoint mirroring the escalation config field.

@router.get("/whatsapp/bot/status")
async def whatsapp_bot_status() -> dict:
    """Whether the bot is on, what model it'd use, and how many phones
    we have history for. Mirrors keys from the escalation config."""
    cfg = load_escalation_config()
    enabled = bool(cfg.get("whatsapp_bot_enabled"))
    model = (str(cfg.get("whatsapp_bot_text_model") or "").strip()
             or whatsapp_bot.DEFAULT_TEXT_MODEL)
    convs = whatsapp_bot.list_conversations()
    return {
        "ok":             True,
        "enabled":        enabled,
        "model":          model,
        "has_api_key":    bool(str(cfg.get("wasender_api_key") or "").strip()),
        "has_gemini_key": bool(state.gemini_api_key),
        "conversations":  len(convs),
    }


class BotToggleIn(BaseModel):
    enabled: bool
    text_model: Optional[str] = None


@router.post("/whatsapp/bot/toggle")
async def whatsapp_bot_toggle(payload: BotToggleIn) -> dict:
    """One-knob enable/disable. Persists to the escalation.json file."""
    patch: dict = {"whatsapp_bot_enabled": bool(payload.enabled)}
    if payload.text_model is not None:
        patch["whatsapp_bot_text_model"] = str(payload.text_model or "").strip()
    cfg = save_escalation_config(patch)
    return {
        "ok":         True,
        "enabled":    bool(cfg.get("whatsapp_bot_enabled")),
        "text_model": str(cfg.get("whatsapp_bot_text_model") or "").strip(),
    }


@router.get("/whatsapp/bot/conversations")
async def whatsapp_bot_conversations() -> dict:
    """List every phone we have a bot conversation for, newest-first."""
    return {"ok": True, "items": whatsapp_bot.list_conversations()}


@router.get("/whatsapp/bot/conversations/{phone}")
async def whatsapp_bot_conversation(phone: str) -> dict:
    """Full turn-by-turn transcript for one phone (digits-only key)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        raise HTTPException(400, "phone must be digits")
    return {
        "ok":    True,
        "phone": digits,
        "turns": whatsapp_bot.get_conversation(digits),
    }


@router.delete("/whatsapp/bot/conversations/{phone}")
async def whatsapp_bot_conversation_clear(phone: str) -> dict:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        raise HTTPException(400, "phone must be digits")
    removed = whatsapp_bot.clear_conversation(digits)
    return {"ok": True, "removed": removed}


@router.delete("/whatsapp/bot/conversations")
async def whatsapp_bot_conversation_clear_all() -> dict:
    n = whatsapp_bot.clear_all_conversations()
    return {"ok": True, "removed": n}


@router.get("/whatsapp/bot/lid-cache")
async def whatsapp_bot_lid_cache_get() -> dict:
    """Persistent LID → phone JID map the bot uses to route replies for
    privacy-mode senders. Survives snapshot patient edits/deletes."""
    return {"ok": True, "items": whatsapp_bot.lid_cache_dump()}


class LidCacheIn(BaseModel):
    lid_jid:    str   # e.g. "233908264300569@lid"
    phone_jid:  str   # e.g. "966591697226@s.whatsapp.net" or just "966591697226"


@router.post("/whatsapp/bot/lid-cache")
async def whatsapp_bot_lid_cache_set(payload: LidCacheIn) -> dict:
    """Manually pin a LID → phone mapping. Use when auto-resolution can't
    bootstrap a brand-new sender (no patient on file yet, or the push
    name doesn't match)."""
    lid = (payload.lid_jid or "").strip()
    phone = (payload.phone_jid or "").strip()
    if not lid.endswith("@lid"):
        raise HTTPException(400, "lid_jid must end in @lid")
    if "@" not in phone:
        digits = "".join(c for c in phone if c.isdigit())
        if not digits:
            raise HTTPException(400, "phone_jid must be digits or a full JID")
        phone = f"{digits}@s.whatsapp.net"
    items = whatsapp_bot.lid_cache_set(lid, phone)
    return {"ok": True, "items": items}


@router.delete("/whatsapp/bot/lid-cache/{lid_jid}")
async def whatsapp_bot_lid_cache_del(lid_jid: str) -> dict:
    removed = whatsapp_bot.lid_cache_delete(lid_jid)
    return {"ok": True, "removed": removed}


class BotTestIn(BaseModel):
    phone: str
    text:  str


@router.post("/whatsapp/bot/test")
async def whatsapp_bot_test(payload: BotTestIn) -> dict:
    """Run the bot synchronously on a synthetic inbound message, then
    return whatever the bot replied. Useful for end-to-end testing
    without depending on the Wasender webhook actually firing — if THIS
    works but real WhatsApp messages don't get a reply, the issue is
    upstream (webhook URL, Wasender event subscription, Cloudflare
    tunnel)."""
    digits = "".join(ch for ch in (payload.phone or "") if ch.isdigit())
    if not digits:
        raise HTTPException(400, "phone must be digits")
    if not (payload.text or "").strip():
        raise HTTPException(400, "text required")
    # Build a synthetic envelope in the Baileys shape; reuse the live
    # broadcast bus so the Activity feed shows the run.
    fake = {
        "key":     {"remoteJid": f"{digits}@s.whatsapp.net",
                    "fromMe":    False,
                    "id":        f"test-{int(asyncio.get_event_loop().time()*1000)}"},
        "message": {"conversation": payload.text},
    }
    try:
        await whatsapp_bot.process_message(
            fake, broadcast=clinic_live_agent_service._broadcast,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    turns = whatsapp_bot.get_conversation(digits)
    # Find the last model turn — that's the reply we just produced.
    last_reply = ""
    for t in reversed(turns):
        if t.get("role") == "model":
            last_reply = str(t.get("text") or "")
            break
    return {"ok": True, "phone": digits, "reply": last_reply,
            "turns": len(turns)}


# ----- Main settings -------------------------------------------------------
# The "about us" clinic-group info (name, location, contact details) that
# the Settings page edits. Stored in data/demos/clinic/main_settings.json
# and git-tracked so both machines stay in sync.

@router.get("/main-settings")
async def main_settings_get() -> dict:
    return {"ok": True, "settings": main_settings_mod.load_main_settings()}


class MainSettingsIn(BaseModel):
    settings: dict


@router.post("/main-settings")
async def main_settings_set(payload: MainSettingsIn) -> dict:
    if not isinstance(payload.settings, dict):
        raise HTTPException(400, "settings must be an object")
    out = main_settings_mod.save_main_settings(payload.settings)
    return {"ok": True, "settings": out}


# ----- Unified Search ------------------------------------------------------
# Search across patients + calls + WhatsApp by phone / name / file number.
# Used by Call Center → Search to surface every interaction we have with a
# given person without the operator having to bounce between three pages.

@router.get("/search")
async def search(q: str = "") -> dict:
    """Find every interaction (patient record, call, WhatsApp thread)
    matching the query. The query is interpreted broadly:
      - digits → match phone / file_number / id_number
      - text   → match name / name_ar (case + whitespace insensitive)
    Calls are matched by `caller_phone`; WhatsApp threads by the
    digits-only suffix of the JID."""
    import re as _re
    query = (q or "").strip()
    if len(query) < 2:
        return {"ok": True, "query": query,
                "patients": [], "calls": [], "whatsapp": []}

    def _norm(s: Optional[str]) -> str:
        return _re.sub(r"\s+", " ", (s or "").strip()).lower()

    def _digits(s: Optional[str]) -> str:
        return _re.sub(r"\D+", "", s or "")

    q_norm   = _norm(query)
    q_digits = _digits(query)

    # ---- Patients --------------------------------------------------------
    snap = load_snapshot()
    matched_patients: list[dict] = []
    phone_set: set[str] = set()
    for p in snap.get("patients", []):
        hits: list[str] = []
        for field in ("name", "name_ar"):
            v = _norm(p.get(field))
            if v and q_norm in v:
                hits.append(field)
        if q_digits:
            for field in ("phone", "id_number"):
                v = _digits(p.get(field))
                if v and q_digits in v:
                    hits.append(field)
            file_num = (p.get("file_number") or "").upper()
            if query.upper() in file_num:
                hits.append("file_number")
        if hits:
            matched_patients.append({**p, "_matched_on": hits})
            d = _digits(p.get("phone"))
            if d:
                phone_set.add(d)
                # Last-9 form so 9665XXXXXXXX matches 5XXXXXXXX matches 05XXXXXXXX
                if len(d) >= 9:
                    phone_set.add(d[-9:])

    # If the query itself is digits, treat them as a phone too — covers the
    # case where the operator searches a phone for someone NOT in the
    # patient registry (e.g. a brand-new caller we have a call recording
    # for but never created a file for).
    if q_digits and len(q_digits) >= 7:
        phone_set.add(q_digits)
        if len(q_digits) >= 9:
            phone_set.add(q_digits[-9:])

    # ---- Calls -----------------------------------------------------------
    calls_matched: list[dict] = []
    if phone_set:
        for c in list_saved_calls(limit=500):
            cp = _digits(c.get("caller_phone"))
            if not cp:
                continue
            if cp in phone_set or (len(cp) >= 9 and cp[-9:] in phone_set):
                calls_matched.append(c)
    # Newest first (list_saved_calls already sorts that way).

    # ---- WhatsApp --------------------------------------------------------
    # Reuse the same merge the /messages endpoint does so we cope with
    # Wasender's outbound-only logs + our local inbound inbox + LID
    # canonicalisation.
    whatsapp_threads: list[dict] = []
    if phone_set:
        cfg = load_escalation_config()
        api_key    = str(cfg.get("wasender_api_key") or "").strip()
        personal   = str(cfg.get("wasender_personal_token") or "").strip()
        session_id = str(cfg.get("wasender_session_id") or "").strip()
        try:
            wa_client = build_wasender_client(api_key, personal)
            wa_result = await wa_client.list_messages(session_id, limit=500)
        except Exception:
            wa_result = {"items": []}
        outbound = wa_result.get("items") or []
        inbound  = whatsapp_inbox.list_inbox()
        items    = list(outbound) + list(inbound)
        our_digits = _detect_our_phone(items)
        name_index = whatsapp_contacts.build_patient_name_index(
            (snap.get("patients") or []),
        )
        ai_ids = whatsapp_bot.ai_sent_ids()

        # Bucket messages per phone — collapse LIDs to their canonical phone.
        by_phone: dict[str, list[dict]] = {}
        for m in items:
            if not isinstance(m, dict):
                continue
            row_jid = _msg_jid(m)
            canon = await whatsapp_contacts.canonical_jid_for(row_jid, name_index)
            d = _digits(canon.split("@", 1)[0]) if "@" in canon else _digits(canon)
            if not d:
                continue
            if not (d in phone_set
                    or (len(d) >= 9 and d[-9:] in phone_set)):
                continue
            msg_id = m.get("id") or (m.get("key") or {}).get("id")
            media_url = None
            if msg_id and whatsapp_bot._find_audio_block(m):
                media_url = f"/api/demo/clinic/whatsapp/media/{msg_id}"
            from_me = _msg_from_me(m, our_digits)
            by_phone.setdefault(d, []).append({
                "id":         msg_id,
                "ts":         _msg_ts(m),
                "from_me":    from_me,
                "text":       _msg_text(m),
                "type":       m.get("messageType") or m.get("type") or "text",
                "media_type": "audio" if media_url else None,
                "media_url":  media_url,
                "sent_by_ai": bool(from_me and msg_id and str(msg_id) in ai_ids),
            })
        for phone, msgs in by_phone.items():
            msgs.sort(key=lambda r: r.get("ts") or 0)
            whatsapp_threads.append({
                "phone":    phone,
                "messages": msgs,
                "count":    len(msgs),
            })
        whatsapp_threads.sort(
            key=lambda t: (t["messages"][-1]["ts"] if t["messages"] else 0),
            reverse=True,
        )

    return {
        "ok":       True,
        "query":    query,
        "patients": matched_patients,
        "calls":    calls_matched,
        "whatsapp": whatsapp_threads,
    }


# ----- Saved-call History --------------------------------------------------

@router.get("/agent/calls")
async def agent_list_calls(limit: int = 100) -> dict:
    """List recorded calls newest-first. Cheap — just stats every meta.json
    in data/demos/clinic/calls."""
    return {"items": list_saved_calls(max(1, min(500, limit)))}


@router.get("/agent/calls/{call_id}")
async def agent_get_call(call_id: str) -> dict:
    meta = load_saved_call(call_id)
    if meta is None:
        raise HTTPException(404, "call not found")
    return meta


@router.get("/agent/calls/{call_id}/audio/{side}")
async def agent_get_call_audio(call_id: str, side: str):
    """Stream the caller-side or agent-side WAV. `side` ∈ {caller, agent}."""
    p = call_audio_path(call_id, side)
    if p is None:
        raise HTTPException(404, "audio not found")
    return FileResponse(str(p), media_type="audio/wav",
                        filename=f"{call_id}-{side}.wav")


@router.delete("/agent/calls/{call_id}")
async def agent_delete_call(call_id: str) -> dict:
    if not delete_saved_call(call_id):
        raise HTTPException(404, "call not found")
    return {"ok": True}


# ----- Data snapshot (clinic SPA → backend) ------------------------------
# The agent's function tools read from data/demos/clinic/snapshot.json. The
# Clinic SPA's Dashboard pushes the current localStorage state here on
# mount (and after any SPA-side mutation) so the agent always has the live
# patient + appointment data.

@router.get("/data/snapshot")
async def agent_get_snapshot() -> dict:
    return load_snapshot()


@router.post("/data/snapshot")
async def agent_set_snapshot(payload: dict) -> dict:
    """Accepts a full snapshot from the SPA — patients, appointments,
    clinics, providers, slot_overrides. Stored as-is."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "snapshot must be a JSON object")
    # Only persist the keys we expect — keeps the file tidy and avoids
    # accidentally storing UI-only state.
    clean = {
        "patients":       list(payload.get("patients") or []),
        "appointments":   list(payload.get("appointments") or []),
        "clinics":        list(payload.get("clinics") or []),
        "providers":      list(payload.get("providers") or []),
        "slot_overrides": list(payload.get("slot_overrides") or []),
    }
    save_snapshot(clean)
    return {
        "ok":              True,
        "patient_count":   len(clean["patients"]),
        "appt_count":      len(clean["appointments"]),
        "clinic_count":    len(clean["clinics"]),
        "override_count":  len(clean["slot_overrides"]),
    }


@router.websocket("/agent/ws")
async def agent_ws(websocket: WebSocket) -> None:
    """Push transcript / call lifecycle events to the Dashboard."""
    await websocket.accept()
    q = clinic_live_agent_service.subscribe()
    try:
        # Send the current status so the UI hydrates immediately.
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "status": clinic_live_agent_service.status(),
            "calls":  clinic_live_agent_service.active_calls(),
        }))

        async def _drain_incoming() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                raise

        drain = asyncio.create_task(_drain_incoming())
        try:
            while True:
                pop = asyncio.create_task(q.get())
                done, _pending = await asyncio.wait(
                    {pop, drain},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if drain in done:
                    pop.cancel()
                    return
                event = pop.result()
                await websocket.send_text(json.dumps(event))
        finally:
            drain.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("agent_ws fatal")
    finally:
        clinic_live_agent_service.unsubscribe(q)
        try: await websocket.close()
        except Exception: pass
