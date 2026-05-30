"""Clinic demo backend — login, current-user, logout, and the Live Agent
control plane (config + persona/KB publish + status). The AudioSocket
service itself lives in demos.clinic.live_agent."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel
import asyncio

from ...core.state import state
from . import config
from .live_agent import (
    restaurant_live_agent_service,
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
from . import menu as menu_mod
from . import layout as layout_mod
from . import invoices as invoices_mod
from . import reservations as reservations_mod
from . import clients as clients_mod
from . import meals_catalog as meals_cat_mod
from . import subscriptions as cm_subs_mod
from . import meal_schedules as schedules_mod
from . import catering_orders as catering_mod
from . import catering_menu as catering_menu_mod
from . import catering_packages as catering_pkg_mod
from . import catering_delivery as catering_dlv_mod
from . import cm_delivery as cm_dlv_mod
from . import kitchen_layout as kzones_mod
from . import kitchen_staff   as kstaff_mod
from . import kitchen_appliances as kappl_mod
from . import kitchen_camera_config as kcam_mod
from . import kitchen_ai_history as khist_mod
from . import kitchen_ai_history_task as khist_task
from . import kitchen_rules as krules_mod
from . import kitchen_rules_engine as krules_engine
from . import kitchen_vision as kvision
from . import ai_center as ai_center_mod
from . import cai_elevenlabs as cai_mod
from .agent_tools import load_snapshot, save_snapshot
from ..auth import (
    clear_session,
    issue_session,
    require_session,
)

logger = logging.getLogger("demo_restaurant")
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
    # ALSO mirror the bind toggle into the restaurant's escalation.json
    # so `_resolved_bind()` sees the operator's intent. Without this the
    # restaurant Live Agent never actually starts because the default in
    # DEFAULT_ESCALATION (live_agent_enabled = False) wins over the
    # shared state.cda_enabled flag, AND the restaurant ends up sharing
    # cda_bind_port with the clinic — which causes whichever demo binds
    # second to fail with "address in use".
    bind_patch: dict = {}
    if payload.enabled   is not None: bind_patch["live_agent_enabled"]   = bool(payload.enabled)
    if payload.bind_host is not None: bind_patch["live_agent_bind_host"] = state.cda_bind_host
    if payload.bind_port is not None: bind_patch["live_agent_bind_port"] = state.cda_bind_port
    if bind_patch:
        save_escalation_config(bind_patch)
    restaurant_live_agent_service.apply_config()
    return _current_agent_config()


@router.get("/agent/status")
async def agent_status() -> dict:
    s = restaurant_live_agent_service.status()
    return {
        **s,
        "calls":          restaurant_live_agent_service.active_calls(),
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
    """Persist persona / KB to data/demos/restaurant/{persona,kb}.txt. Service
    re-reads on every call so the next inbound dial picks up the change
    with no restart needed. If the operator has the ElevenLabs CAI voice
    provider selected, we also push the new persona / KB to the auto-managed
    agent on their account in the same request (fire-and-forget — failure
    just means the next call falls back to the previous agent body)."""
    if payload.persona is not None: save_persona(payload.persona)
    if payload.kb      is not None: save_kb(payload.kb)
    persona, kb = load_persona(), load_kb()
    if cai_mod.is_active():
        try:
            await cai_mod.ensure_agent(
                persona=persona, kb=kb,
                voice_id=state.elevenlabs_voice_id or "EXAVITQu4vr4xnSDxMaL",
                model_id=state.elevenlabs_model_id or "eleven_multilingual_v2",
                first_message=state.cda_greeting or "",
            )
        except Exception as e:
            logger.warning("CAI ensure_agent after prompt save failed: %s", e)
    return {"ok": True, "persona": persona, "kb": kb,
            "agent_id": state.elevenlabs_agent_id or ""}


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
    call = restaurant_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    if not call.ack_flag():
        return {"ok": True, "noop": True}
    return {"ok": True}


class MuteIn(BaseModel):
    muted: bool


@router.post("/agent/calls/{call_id}/mute")
async def agent_mute(call_id: str, payload: MuteIn) -> dict:
    """Toggle the supervisor mute on a live call. When `muted=true` the
    Live Agent's voice is silenced (audio_out drained, no AudioSocket
    writes) so a supervisor who barged in via ChanSpy can speak to the
    caller without the AI talking over them. The session itself stays
    alive — Gemini keeps transcribing the caller for the dashboard so
    the supervisor sees what's being said. Broadcasts a `muted` event
    over the live-agent WS so every dashboard instance updates the
    button state in sync."""
    call = restaurant_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    result = call.set_muted(bool(payload.muted))
    return {"ok": True, **result}


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
    call = restaurant_live_agent_service.get_call(call_id)
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
            broadcast=restaurant_live_agent_service._broadcast,
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
                 / "data" / "demos" / "restaurant" / "whatsapp_outgoing")


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
    upload under data/demos/restaurant/whatsapp_outgoing/, then construct
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
    cache_dir = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant" / "whatsapp_media"
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
# Read-only views over data/demos/restaurant/whatsapp_conversations.json so the
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
            fake, broadcast=restaurant_live_agent_service._broadcast,
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
# the Settings page edits. Stored in data/demos/restaurant/main_settings.json
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


# ----- Integrations (Frigate IP + Gemini API key) --------------------------
# These are CREDENTIALS and live in data/state.json (gitignored), NOT in
# the git-tracked main_settings.json. We just provide a Restaurant-scoped
# GET/POST so the Settings page can edit them without leaving the demo SPA.
# Edits here propagate to every other consumer (the global AI-Camera
# Playground, SIP Live Agent, etc.) since they all read the same state.

@router.get("/integrations")
async def integrations_get() -> dict:
    return {
        "ok": True,
        "frigate_url":               state.frigate_url or "",
        # Never echo raw secrets — just whether they're set + a masked preview.
        "gemini_api_key_set":        bool(state.gemini_api_key),
        "gemini_api_key_masked":     _mask_secret(state.gemini_api_key or ""),
        "gemini_model":              state.gemini_model or "",
        "deepseek_api_key_set":      bool(state.deepseek_api_key),
        "deepseek_api_key_masked":   _mask_secret(state.deepseek_api_key or ""),
        "deepseek_base_url":         state.deepseek_base_url or "",
        "openrouter_api_key_set":    bool(state.openrouter_api_key),
        "openrouter_api_key_masked": _mask_secret(state.openrouter_api_key or ""),
        "openrouter_base_url":       state.openrouter_base_url or "",
        "elevenlabs_api_key_set":    bool(state.elevenlabs_api_key),
        "elevenlabs_api_key_masked": _mask_secret(state.elevenlabs_api_key or ""),
        "elevenlabs_voice_id":       state.elevenlabs_voice_id or "",
        "elevenlabs_model_id":       state.elevenlabs_model_id or "",
        "voice_provider":            state.voice_provider or "gemini",
        "elevenlabs_agent_id":       state.elevenlabs_agent_id or "",
        "homeassistant_url":         state.homeassistant_url or "",
        "homeassistant_token_set":   bool(state.homeassistant_token),
        "homeassistant_token_masked": _mask_secret(state.homeassistant_token or ""),
    }


class IntegrationsIn(BaseModel):
    frigate_url:         Optional[str] = None
    gemini_api_key:      Optional[str] = None   # send "" to clear
    gemini_model:        Optional[str] = None
    deepseek_api_key:    Optional[str] = None   # send "" to clear
    deepseek_base_url:   Optional[str] = None
    openrouter_api_key:  Optional[str] = None   # send "" to clear
    openrouter_base_url: Optional[str] = None
    elevenlabs_api_key:  Optional[str] = None   # send "" to clear
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model_id: Optional[str] = None
    voice_provider:      Optional[str] = None   # "gemini" | "elevenlabs_cai"
    elevenlabs_agent_id: Optional[str] = None
    homeassistant_url:   Optional[str] = None
    homeassistant_token: Optional[str] = None   # send "" to clear


@router.post("/integrations")
async def integrations_set(payload: IntegrationsIn) -> dict:
    if payload.frigate_url is not None:
        v = (payload.frigate_url or "").strip()
        # Strip trailing slash so the Frigate router's f"{state.frigate_url}/api/…"
        # concatenations don't end up with a double slash.
        state.frigate_url = (v.rstrip("/") or None)
    if payload.gemini_api_key is not None:
        v = (payload.gemini_api_key or "").strip()
        state.gemini_api_key = (v or None)
    if payload.gemini_model is not None:
        v = (payload.gemini_model or "").strip()
        if v:
            state.gemini_model = v
    if payload.deepseek_api_key is not None:
        v = (payload.deepseek_api_key or "").strip()
        state.deepseek_api_key = (v or None)
    if payload.deepseek_base_url is not None:
        v = (payload.deepseek_base_url or "").strip().rstrip("/")
        # Keep a sane default so the operator can clear the field without
        # nuking the whole DeepSeek integration.
        state.deepseek_base_url = (v or "https://api.deepseek.com")
    if payload.openrouter_api_key is not None:
        v = (payload.openrouter_api_key or "").strip()
        state.openrouter_api_key = (v or None)
    if payload.openrouter_base_url is not None:
        v = (payload.openrouter_base_url or "").strip().rstrip("/")
        state.openrouter_base_url = (v or "https://openrouter.ai/api/v1")
    if payload.elevenlabs_api_key is not None:
        v = (payload.elevenlabs_api_key or "").strip()
        state.elevenlabs_api_key = (v or None)
    if payload.elevenlabs_voice_id is not None:
        v = (payload.elevenlabs_voice_id or "").strip()
        if v: state.elevenlabs_voice_id = v
    if payload.elevenlabs_model_id is not None:
        v = (payload.elevenlabs_model_id or "").strip()
        if v: state.elevenlabs_model_id = v
    if payload.voice_provider is not None:
        v = (payload.voice_provider or "").strip().lower()
        if v in ("gemini", "gemini_with_elevenlabs_tts", "elevenlabs_cai"):
            state.voice_provider = v
    if payload.elevenlabs_agent_id is not None:
        v = (payload.elevenlabs_agent_id or "").strip()
        state.elevenlabs_agent_id = (v or None)
    # Trigger ensure_agent when any field that affects the auto-managed
    # agent changes — including an explicit clear of `elevenlabs_agent_id`
    # (operator's way of saying "recreate fresh"). When agent_id is
    # empty, ensure_agent CREATEs a new agent; otherwise it PATCHes the
    # existing one only if the config hash drifted.
    if cai_mod.is_active() and (
        payload.voice_provider is not None
        or payload.elevenlabs_voice_id is not None
        or payload.elevenlabs_model_id is not None
        or payload.elevenlabs_api_key  is not None
        or payload.elevenlabs_agent_id is not None
    ):
        try:
            await cai_mod.ensure_agent(
                persona=load_persona(), kb=load_kb(),
                voice_id=state.elevenlabs_voice_id or "EXAVITQu4vr4xnSDxMaL",
                model_id=state.elevenlabs_model_id or "eleven_multilingual_v2",
                first_message=state.cda_greeting or "",
            )
        except Exception as e:
            logger.warning("CAI ensure_agent after settings save failed: %s", e)
    if payload.homeassistant_url is not None:
        v = (payload.homeassistant_url or "").strip().rstrip("/")
        state.homeassistant_url = (v or None)
    if payload.homeassistant_token is not None:
        v = (payload.homeassistant_token or "").strip()
        state.homeassistant_token = (v or None)
    state.save()
    return await integrations_get()


def _mask_secret(s: str) -> str:
    if not s: return ""
    if len(s) <= 8: return "•" * len(s)
    return s[:4] + "…" + s[-4:]


# ----- ElevenLabs Conversational AI: recreate auto-managed agent ----------
# A dedicated endpoint that wipes the persisted agent_id and immediately
# runs ensure_agent() so a fresh agent is created from the CURRENT
# persona / KB / voice / model on disk. We need this because PATCH on
# ElevenLabs CAI does NOT propagate changes to fundamental fields like
# voice_id or agent_output_audio_format on an already-created agent —
# they're locked at create-time. Without this, edits to voice_id in
# Settings silently fail to take effect.

@router.post("/agent/cai/recreate")
async def agent_cai_recreate() -> dict:
    if not cai_mod.is_active():
        raise HTTPException(400,
            "Voice provider is not set to elevenlabs_cai. Switch to it in "
            "Settings → ElevenLabs first.")
    old_id = state.elevenlabs_agent_id
    state.elevenlabs_agent_id = None
    state.save()
    try:
        new_id = await cai_mod.ensure_agent(
            persona=load_persona(), kb=load_kb(),
            voice_id=state.elevenlabs_voice_id or "EXAVITQu4vr4xnSDxMaL",
            model_id=state.elevenlabs_model_id or "eleven_multilingual_v2",
            first_message=state.cda_greeting or "",
        )
    except RuntimeError as e:
        # ensure_agent now raises with the actual ElevenLabs response body
        # so the operator sees WHICH constraint failed (quota, validation,
        # CAI not enabled on the account, etc.) — not a generic message.
        raise HTTPException(502, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"CAI ensure_agent crashed: {e}") from e
    if not new_id:
        # Defensive — ensure_agent's RuntimeError path covers the real
        # error cases; this only fires if it returned None silently.
        raise HTTPException(502,
            "CAI create returned no agent_id. Check the ./run.sh terminal "
            "for the underlying error — typically API key missing, CAI not "
            "enabled, or an agent-quota cap on your ElevenLabs plan.")
    logger.info("CAI agent recreated old=%s new=%s", old_id, new_id)
    return {"ok": True, "old_agent_id": old_id, "new_agent_id": new_id}


# ----- Reset ALL dummy data ------------------------------------------------
# Powers the "Reset all dummy data" button on the Overview page. Runs every
# per-module reset_* in one shot so demo dates / sample rows are freshened
# across every page. Each step is wrapped so one failure doesn't abort the
# rest — the response lists per-module ok/error so the UI can show what
# survived. Order doesn't matter (no cross-module foreign keys).

@router.post("/dummy-data/reset-all")
async def dummy_data_reset_all() -> dict:
    steps: list[tuple[str, callable]] = [
        ("menu",                 menu_mod.reset_menu),
        ("layout",               layout_mod.reset_layout),
        ("invoices",             invoices_mod.reset_invoices),
        ("reservations",         reservations_mod.reset_reservations),
        ("clients",              clients_mod.reset_clients),
        ("meals_catalog",        meals_cat_mod.reset_catalog),
        ("cm_subscriptions",     cm_subs_mod.reset_subscriptions),
        ("cm_schedules",         schedules_mod.reset_schedules),
        ("cm_delivery",          cm_dlv_mod.reset_delivery),
        ("catering_orders",      catering_mod.reset_orders),
        ("catering_menu",        catering_menu_mod.reset_items),
        ("catering_packages",    catering_pkg_mod.reset_packages),
        ("catering_delivery",    catering_dlv_mod.reset_delivery),
        ("kitchen_layout",       kzones_mod.reset_layout),
        ("kitchen_staff",        kstaff_mod.reset_staff),
        ("kitchen_appliances",   kappl_mod.reset_catalog),
        ("kitchen_camera_cfg",   kcam_mod.reset_config),
    ]
    results: list[dict] = []
    ok_count = 0
    for name, fn in steps:
        try:
            fn()
            results.append({"module": name, "ok": True})
            ok_count += 1
        except Exception as exc:
            logger.exception("dummy_data reset failed: %s", name)
            results.append({"module": name, "ok": False, "error": str(exc)})
    return {
        "ok": ok_count == len(steps),
        "reset": ok_count,
        "total": len(steps),
        "results": results,
    }


# ----- F&B Menu ------------------------------------------------------------
# Operator-editable Lebanese-restaurant menu (categories + items).
# Reset Dummy Data restores the seed in `menu.py`.

@router.get("/menu")
async def menu_get() -> dict:
    return {"ok": True, **menu_mod.load_menu()}


@router.post("/menu/reset")
async def menu_reset() -> dict:
    return {"ok": True, **menu_mod.reset_menu()}


class MenuCategoryIn(BaseModel):
    id:         Optional[str] = None
    name_en:    Optional[str] = None
    name_ar:    Optional[str] = None
    sort_order: Optional[int] = None
    active:     Optional[bool] = None


@router.post("/menu/categories")
async def menu_category_add(payload: MenuCategoryIn) -> dict:
    try:
        cat = menu_mod.add_category(payload.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "category": cat}


@router.patch("/menu/categories/{cat_id}")
async def menu_category_update(cat_id: str, payload: MenuCategoryIn) -> dict:
    out = menu_mod.update_category(cat_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "category not found")
    return {"ok": True, "category": out}


@router.delete("/menu/categories/{cat_id}")
async def menu_category_delete(cat_id: str) -> dict:
    return {"ok": True, **menu_mod.delete_category(cat_id)}


class MenuItemIn(BaseModel):
    id:             Optional[str] = None
    category_id:    Optional[str] = None
    name_en:        Optional[str] = None
    name_ar:        Optional[str] = None
    description_en: Optional[str] = None
    description_ar: Optional[str] = None
    price:          Optional[float] = None
    currency:       Optional[str] = None
    available:      Optional[bool] = None


@router.post("/menu/items")
async def menu_item_add(payload: MenuItemIn) -> dict:
    try:
        item = menu_mod.add_item(payload.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": item}


@router.patch("/menu/items/{item_id}")
async def menu_item_update(item_id: str, payload: MenuItemIn) -> dict:
    out = menu_mod.update_item(item_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "item not found")
    return {"ok": True, "item": out}


@router.delete("/menu/items/{item_id}")
async def menu_item_delete(item_id: str) -> dict:
    removed = menu_mod.delete_item(item_id)
    if not removed:
        raise HTTPException(404, "item not found")
    return {"ok": True, "removed": True}


# ----- Restaurant Layout ---------------------------------------------------
# Operator-editable floor plan (indoor + outdoor) with per-table state:
# seats, status (free/occupied/reserved), guests, ordered_items, server.
# Reset Dummy Data restores the seed in `layout.py`.

@router.get("/layout")
async def layout_get() -> dict:
    return {"ok": True, **layout_mod.load_layout()}


@router.post("/layout/reset")
async def layout_reset() -> dict:
    return {"ok": True, **layout_mod.reset_layout()}


class TableItemIn(BaseModel):
    name_en:  Optional[str] = None
    name_ar:  Optional[str] = None
    qty:      Optional[int] = None
    price:    Optional[float] = None
    currency: Optional[str] = None


class TableIn(BaseModel):
    id:             Optional[str]  = None
    floor_id:       Optional[str]  = None
    number:         Optional[str]  = None
    x:              Optional[int]  = None
    y:              Optional[int]  = None
    width:          Optional[int]  = None
    height:         Optional[int]  = None
    shape:          Optional[str]  = None
    seats:          Optional[int]  = None
    status:         Optional[str]  = None
    guests:         Optional[int]  = None
    ordered_items:  Optional[list[TableItemIn]] = None
    opened_at:      Optional[int]  = None
    server_name:    Optional[str]  = None


def _table_in_to_dict(payload: TableIn) -> dict:
    d = payload.model_dump(exclude_none=True)
    if "ordered_items" in d:
        # Each item carries optional fields; turn the Pydantic objects
        # back into plain dicts the `layout` module's coercer can chew on.
        d["ordered_items"] = [
            (i if isinstance(i, dict) else dict(i))
            for i in d["ordered_items"]
        ]
    return d


@router.post("/layout/tables")
async def layout_table_add(payload: TableIn) -> dict:
    table = layout_mod.add_table(_table_in_to_dict(payload))
    return {"ok": True, "table": table}


@router.patch("/layout/tables/{table_id}")
async def layout_table_update(table_id: str, payload: TableIn) -> dict:
    out = layout_mod.update_table(table_id, _table_in_to_dict(payload))
    if out is None:
        raise HTTPException(404, "table not found")
    return {"ok": True, "table": out}


@router.delete("/layout/tables/{table_id}")
async def layout_table_delete(table_id: str) -> dict:
    removed = layout_mod.delete_table(table_id)
    if not removed:
        raise HTTPException(404, "table not found")
    return {"ok": True, "removed": True}


# ----- Invoices + Overview -------------------------------------------------
# Invoices: today's closed checks. Overview: an aggregate used by the
# Restaurant → Overview page that combines current floor occupancy (from
# layout.py) with today's invoice metrics so the operator sees the whole
# service state at a glance. The Overview reflects layout edits
# automatically — it re-reads `layout.load_layout()` on every request.

@router.get("/invoices")
async def invoices_get() -> dict:
    return {"ok": True, "invoices": invoices_mod.load_invoices()}


@router.post("/invoices/reset")
async def invoices_reset() -> dict:
    return {"ok": True, "invoices": invoices_mod.reset_invoices()}


# ============================================================================
# Orders / POS — the missing link between Layout (open tabs) and Invoices
# (closed checks). The page presents every occupied table as an editable
# tab: add menu items, remove items, change guests/server, then "close" to
# stamp an invoice and flip the table back to free.
# ============================================================================

_VAT_RATE = 0.15


def _tab_totals(items: list[dict], tip_pct: float = 0.0) -> dict:
    sub = round(sum((it.get("qty") or 0) * (it.get("price") or 0) for it in items), 2)
    tax = round(sub * _VAT_RATE, 2)
    tip = round(sub * (tip_pct / 100.0), 2)
    return {"subtotal": sub, "tax": tax, "tip": tip, "total": round(sub + tax + tip, 2)}


@router.get("/orders/open")
async def orders_open_get() -> dict:
    """List every table currently flagged as occupied + its running tab
    totals. Free + reserved tables are returned separately so the Orders
    page can show capacity headroom + reservations waiting to be seated."""
    layout = layout_mod.load_layout()
    tables = layout.get("tables", [])
    open_tabs: list[dict] = []
    reserved:  list[dict] = []
    free_count = 0
    occ_seats  = 0
    free_seats = 0
    for t in tables:
        st = (t.get("status") or "").lower()
        if st == "occupied":
            items = t.get("ordered_items") or []
            tot = _tab_totals(items, tip_pct=5.0)
            open_tabs.append({
                **t,
                "running": tot,
            })
            occ_seats += int(t.get("seats") or 0)
        elif st == "reserved":
            reserved.append(t)
        else:
            free_count += 1
            free_seats += int(t.get("seats") or 0)

    # Sort newest-first by opened_at so most-recently-seated is on top.
    open_tabs.sort(key=lambda r: r.get("opened_at") or 0, reverse=True)
    return {
        "ok": True,
        "open_tabs":  open_tabs,
        "reserved":   reserved,
        "floors":     layout.get("floors", []),
        "totals": {
            "open_count":    len(open_tabs),
            "reserved_count": len(reserved),
            "free_count":    free_count,
            "open_seats":    occ_seats,
            "free_seats":    free_seats,
            "service_total": round(sum(t["running"]["total"] for t in open_tabs), 2),
            "currency":      invoices_mod.DEFAULT_CURRENCY,
        },
    }


class OrderItemIn(BaseModel):
    name_en:     Optional[str] = None
    name_ar:     Optional[str] = None
    qty:         Optional[int] = None
    price:       Optional[float] = None
    category_id: Optional[str] = None


@router.post("/orders/{table_id}/items")
async def orders_add_item(table_id: str, payload: OrderItemIn) -> dict:
    """Append (or merge by name) one line item to a table's open tab.
    Flips the table to occupied if it isn't already."""
    layout = layout_mod.load_layout()
    table = next((t for t in layout.get("tables", []) if t.get("id") == table_id), None)
    if not table:
        raise HTTPException(404, "table not found")
    items = list(table.get("ordered_items") or [])
    new_qty = max(1, int(payload.qty or 1))
    # Merge by name_en if the row already exists at the same price.
    merged = False
    for it in items:
        if (it.get("name_en") == (payload.name_en or "")
                and abs((it.get("price") or 0) - float(payload.price or 0)) < 0.005):
            it["qty"] = int(it.get("qty") or 0) + new_qty
            merged = True
            break
    if not merged:
        items.append({
            "name_en":  (payload.name_en or "").strip(),
            "name_ar":  (payload.name_ar or "").strip(),
            "qty":      new_qty,
            "price":    round(max(0.0, float(payload.price or 0)), 2),
            "currency": invoices_mod.DEFAULT_CURRENCY,
        })
    patch: dict = {"ordered_items": items}
    if (table.get("status") or "") != "occupied":
        patch["status"] = "occupied"
        patch["opened_at"] = int(__import__("time").time())
        if not (table.get("guests") or 0):
            patch["guests"] = max(1, int(table.get("seats") or 1) // 2 or 1)
    out = layout_mod.update_table(table_id, patch)
    return {"ok": True, "table": out}


class OrderItemRemoveIn(BaseModel):
    index: int  # 0-based position in ordered_items
    qty:   Optional[int] = None   # if set, decrement; else remove the line


@router.post("/orders/{table_id}/items/remove")
async def orders_remove_item(table_id: str, payload: OrderItemRemoveIn) -> dict:
    layout = layout_mod.load_layout()
    table = next((t for t in layout.get("tables", []) if t.get("id") == table_id), None)
    if not table:
        raise HTTPException(404, "table not found")
    items = list(table.get("ordered_items") or [])
    if payload.index < 0 or payload.index >= len(items):
        raise HTTPException(400, "index out of range")
    if payload.qty is not None and payload.qty > 0:
        items[payload.index]["qty"] = max(0, int(items[payload.index]["qty"]) - int(payload.qty))
        if items[payload.index]["qty"] <= 0:
            items.pop(payload.index)
    else:
        items.pop(payload.index)
    out = layout_mod.update_table(table_id, {"ordered_items": items})
    return {"ok": True, "table": out}


class OrderMetaIn(BaseModel):
    guests:      Optional[int] = None
    server_name: Optional[str] = None


@router.patch("/orders/{table_id}")
async def orders_update_meta(table_id: str, payload: OrderMetaIn) -> dict:
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(400, "nothing to update")
    out = layout_mod.update_table(table_id, patch)
    if out is None:
        raise HTTPException(404, "table not found")
    return {"ok": True, "table": out}


class OrderCloseIn(BaseModel):
    payment_method: Optional[str] = None   # card | cash | mada
    tip_pct:        Optional[float] = None


@router.post("/orders/{table_id}/close")
async def orders_close(table_id: str, payload: OrderCloseIn) -> dict:
    """Close the tab to an invoice: copy items into invoices.json, then
    flip the table back to free (clearing items, guests, opened_at)."""
    layout = layout_mod.load_layout()
    table = next((t for t in layout.get("tables", []) if t.get("id") == table_id), None)
    if not table:
        raise HTTPException(404, "table not found")
    items = table.get("ordered_items") or []
    if not items:
        raise HTTPException(400, "table has no items to close")
    try:
        inv = invoices_mod.add_invoice({
            "table_number":   table.get("number") or "",
            "server_name":    table.get("server_name") or "",
            "guests":         table.get("guests") or 0,
            "items":          items,
            "payment_method": payload.payment_method or "card",
            "tip_pct":        payload.tip_pct or 5.0,
        })
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = layout_mod.update_table(table_id, {
        "status": "free",
        "ordered_items": [],
        "guests": 0,
        "opened_at": None,
        "server_name": "",
    })
    return {"ok": True, "invoice": inv, "table": out}


@router.post("/orders/{table_id}/open")
async def orders_open_seat(table_id: str, payload: OrderMetaIn) -> dict:
    """Seat a free / reserved table — flip status to occupied, set guests +
    server, stamp opened_at. Used when a reservation arrives or a walk-in
    is seated."""
    layout = layout_mod.load_layout()
    table = next((t for t in layout.get("tables", []) if t.get("id") == table_id), None)
    if not table:
        raise HTTPException(404, "table not found")
    patch = {
        "status":      "occupied",
        "guests":      max(1, int(payload.guests or table.get("guests") or 2)),
        "server_name": payload.server_name or table.get("server_name") or "",
        "opened_at":   int(__import__("time").time()),
        "ordered_items": table.get("ordered_items") or [],
    }
    out = layout_mod.update_table(table_id, patch)
    return {"ok": True, "table": out}


# ----- Reservations --------------------------------------------------------
# Strictly today + tomorrow (date allow-list enforced server-side). Each
# add/update/delete syncs the matching table's status in the layout —
# so creating a reservation flips a table to "reserved" on the Layout
# page, cancelling it flips back to "free", and the Overview reflects
# both because it reads layout.

@router.get("/reservations")
async def reservations_get(date: Optional[str] = None) -> dict:
    return {
        "ok":             True,
        "reservations":   reservations_mod.reservations_for(date),
        "allowed_dates":  reservations_mod.allowed_dates(),
    }


@router.post("/reservations/reset")
async def reservations_reset() -> dict:
    return {"ok": True, "reservations": reservations_mod.reset_reservations()}


class ReservationIn(BaseModel):
    id:            Optional[str] = None
    table_number:  Optional[str] = None
    date:          Optional[str] = None
    time:          Optional[str] = None
    duration_min:  Optional[int] = None
    guest_name:    Optional[str] = None
    guest_phone:   Optional[str] = None
    party_size:    Optional[int] = None
    notes:         Optional[str] = None
    status:        Optional[str] = None


@router.post("/reservations")
async def reservations_add(payload: ReservationIn) -> dict:
    try:
        res = reservations_mod.add_reservation(payload.model_dump(exclude_none=True))
    except reservations_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "reservation": res}


@router.patch("/reservations/{res_id}")
async def reservations_update(res_id: str, payload: ReservationIn) -> dict:
    try:
        out = reservations_mod.update_reservation(
            res_id, payload.model_dump(exclude_none=True),
        )
    except reservations_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "reservation not found")
    return {"ok": True, "reservation": out}


@router.delete("/reservations/{res_id}")
async def reservations_delete(res_id: str) -> dict:
    if not reservations_mod.delete_reservation(res_id):
        raise HTTPException(404, "reservation not found")
    return {"ok": True, "removed": True}


@router.get("/overview")
async def overview_get() -> dict:
    """Aggregated dashboard payload for Restaurant → Overview. Pulls
    LIVE from layout (so table-status edits show up immediately) and
    from today's closed invoices."""
    layout = layout_mod.load_layout()
    tables = layout.get("tables") or []

    # ---- Floor / status counters -----------------------------------------
    by_status = {"free": 0, "occupied": 0, "reserved": 0}
    seats_total = 0
    guests_seated = 0
    for t in tables:
        st = (t.get("status") or "free").lower()
        if st in by_status:
            by_status[st] += 1
        seats_total += int(t.get("seats") or 0)
        if st == "occupied":
            guests_seated += int(t.get("guests") or 0)

    # Per-floor breakdown so the UI can show "Indoor 3/6 occupied" etc.
    by_floor: dict[str, dict] = {}
    for f in (layout.get("floors") or []):
        fid = f.get("id")
        if not fid:
            continue
        floor_tables = [t for t in tables if t.get("floor_id") == fid]
        floor_counts = {"free": 0, "occupied": 0, "reserved": 0}
        for t in floor_tables:
            st = (t.get("status") or "free").lower()
            if st in floor_counts:
                floor_counts[st] += 1
        by_floor[fid] = {
            "id":         fid,
            "name_en":    f.get("name_en"),
            "name_ar":    f.get("name_ar"),
            "total":      len(floor_tables),
            "by_status":  floor_counts,
        }

    # ---- Today's invoices ------------------------------------------------
    today = invoices_mod.invoices_for_today()
    revenue_subtotal = round(sum(float(r.get("subtotal") or 0) for r in today), 2)
    revenue_tax      = round(sum(float(r.get("tax")      or 0) for r in today), 2)
    revenue_tip      = round(sum(float(r.get("tip")      or 0) for r in today), 2)
    revenue_total    = round(sum(float(r.get("total")    or 0) for r in today), 2)
    items_sold = sum(
        int(it.get("qty") or 0)
        for r in today for it in (r.get("items") or [])
    )

    # Per-category item count → drives the visual on the Overview page.
    # We label categories by the F&B Menu's category names where
    # possible; unknown ids fall back to "Uncategorised".
    menu = menu_mod.load_menu()
    cat_by_id = {c.get("id"): c for c in (menu.get("categories") or [])}
    per_cat: dict[str, dict] = {}
    for r in today:
        for it in (r.get("items") or []):
            cid = it.get("category_id") or "_uncat"
            qty = int(it.get("qty") or 0)
            revenue = round(qty * float(it.get("price") or 0), 2)
            bucket = per_cat.setdefault(cid, {
                "category_id": cid,
                "name_en":     (cat_by_id.get(cid) or {}).get("name_en") or "Uncategorised",
                "name_ar":     (cat_by_id.get(cid) or {}).get("name_ar") or "غير مصنّف",
                "sort_order":  (cat_by_id.get(cid) or {}).get("sort_order") or 99,
                "qty":         0,
                "revenue":     0.0,
            })
            bucket["qty"]     += qty
            bucket["revenue"] = round(bucket["revenue"] + revenue, 2)
    items_per_category = sorted(
        per_cat.values(),
        key=lambda b: (b["sort_order"], b["name_en"]),
    )

    # Payment-method breakdown for the small pill row on the page.
    by_payment: dict[str, dict] = {}
    for r in today:
        pm = (r.get("payment_method") or "other").lower()
        bucket = by_payment.setdefault(pm, {"method": pm, "count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] = round(bucket["total"] + float(r.get("total") or 0), 2)
    payment_breakdown = sorted(by_payment.values(),
                                key=lambda b: -b["total"])

    return {
        "ok":       True,
        "tables": {
            "total":          len(tables),
            "by_status":      by_status,
            "seats_total":    seats_total,
            "guests_seated":  guests_seated,
            "by_floor":       list(by_floor.values()),
        },
        "today": {
            "invoice_count":      len(today),
            "items_sold":         items_sold,
            "revenue_subtotal":   revenue_subtotal,
            "revenue_tax":        revenue_tax,
            "revenue_tip":        revenue_tip,
            "revenue_total":      revenue_total,
            "items_per_category": items_per_category,
            "payment_breakdown":  payment_breakdown,
            "currency":           invoices_mod.DEFAULT_CURRENCY,
        },
        # Today's closed invoices feed the Revenue table directly.
        "invoices_today":     today,
        # Upcoming reservations across today + tomorrow — Overview shows
        # the count, Reservations page reads them in full.
        "reservations_today": reservations_mod.reservations_for(
            reservations_mod.allowed_dates()[0]
        ),
        "reservations_tomorrow": reservations_mod.reservations_for(
            reservations_mod.allowed_dates()[1]
        ),
    }


# ----- Custom Meals · Clients ---------------------------------------------
# Simple per-restaurant clients DB powering /custom-meals/clients on the
# SPA. Reset Dummy Data restores the 10-row Lebanese-restaurant seed.

@router.get("/clients")
async def clients_get() -> dict:
    return {
        "ok":        True,
        "clients":   clients_mod.load_clients(),
        "diet_tags": clients_mod.DIET_TAGS,
    }


@router.post("/clients/reset")
async def clients_reset() -> dict:
    return {"ok": True, "clients": clients_mod.reset_clients()}


class ClientIn(BaseModel):
    id:          Optional[str] = None
    name:        Optional[str] = None
    name_ar:     Optional[str] = None
    phone:       Optional[str] = None
    email:       Optional[str] = None
    city:        Optional[str] = None
    diet_tags:   Optional[list[str]] = None
    allergies:   Optional[str] = None
    notes:       Optional[str] = None
    created_at:  Optional[int] = None
    active:      Optional[bool] = None


@router.post("/clients")
async def clients_add(payload: ClientIn) -> dict:
    try:
        cli = clients_mod.add_client(payload.model_dump(exclude_none=True))
    except clients_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "client": cli}


@router.patch("/clients/{client_id}")
async def clients_update(client_id: str, payload: ClientIn) -> dict:
    try:
        out = clients_mod.update_client(client_id, payload.model_dump(exclude_none=True))
    except clients_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "client not found")
    return {"ok": True, "client": out}


@router.delete("/clients/{client_id}")
async def clients_delete(client_id: str) -> dict:
    if not clients_mod.delete_client(client_id):
        raise HTTPException(404, "client not found")
    return {"ok": True, "removed": True}


# ----- Custom Meals · Meals catalog ---------------------------------------
# Meals + raw-materials inventory. Reset Dummy Data restores both to
# the Lebanese-restaurant seed in `meals_catalog.py`.

@router.get("/meals-catalog")
async def meals_catalog_get() -> dict:
    data = meals_cat_mod.load_catalog()
    return {
        "ok":              True,
        "meals":           data["meals"],
        "raw_materials":   data["raw_materials"],
        "meal_categories": meals_cat_mod.MEAL_CATEGORIES,
        "diet_tags":       meals_cat_mod.MEAL_DIET_TAGS,
        "allergen_types":  meals_cat_mod.ALLERGEN_TYPES,
        "material_units":  meals_cat_mod.MATERIAL_UNITS,
    }


@router.post("/meals-catalog/reset")
async def meals_catalog_reset() -> dict:
    data = meals_cat_mod.reset_catalog()
    return {"ok": True, "meals": data["meals"], "raw_materials": data["raw_materials"]}


class MealIn(BaseModel):
    id:              Optional[str] = None
    category:        Optional[str] = None
    name_en:         Optional[str] = None
    name_ar:         Optional[str] = None
    description_en:  Optional[str] = None
    description_ar:  Optional[str] = None
    portion_size_g:  Optional[int] = None
    prep_time_min:   Optional[int] = None
    calories:        Optional[int] = None
    protein_g:       Optional[int] = None
    carbs_g:         Optional[int] = None
    fat_g:           Optional[int] = None
    fiber_g:         Optional[int] = None
    diet_tags:       Optional[list[str]] = None
    material_ids:    Optional[list[str]] = None
    price:           Optional[float] = None
    currency:        Optional[str] = None
    active:          Optional[bool] = None


@router.post("/meals-catalog/meals")
async def meals_add(payload: MealIn) -> dict:
    try:
        meal = meals_cat_mod.add_meal(payload.model_dump(exclude_none=True))
    except meals_cat_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "meal": meal}


@router.patch("/meals-catalog/meals/{meal_id}")
async def meals_update(meal_id: str, payload: MealIn) -> dict:
    try:
        out = meals_cat_mod.update_meal(meal_id, payload.model_dump(exclude_none=True))
    except meals_cat_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "meal not found")
    return {"ok": True, "meal": out}


@router.delete("/meals-catalog/meals/{meal_id}")
async def meals_delete(meal_id: str) -> dict:
    if not meals_cat_mod.delete_meal(meal_id):
        raise HTTPException(404, "meal not found")
    return {"ok": True, "removed": True}


class MaterialIn(BaseModel):
    id:             Optional[str] = None
    name_en:        Optional[str] = None
    name_ar:        Optional[str] = None
    unit:           Optional[str] = None
    current_stock:  Optional[float] = None
    min_stock:      Optional[float] = None
    cost_per_unit:  Optional[float] = None
    supplier:       Optional[str] = None
    allergen_type:  Optional[str] = None
    active:         Optional[bool] = None


@router.post("/meals-catalog/materials")
async def materials_add(payload: MaterialIn) -> dict:
    try:
        mat = meals_cat_mod.add_material(payload.model_dump(exclude_none=True))
    except meals_cat_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "material": mat}


@router.patch("/meals-catalog/materials/{mat_id}")
async def materials_update(mat_id: str, payload: MaterialIn) -> dict:
    try:
        out = meals_cat_mod.update_material(mat_id, payload.model_dump(exclude_none=True))
    except meals_cat_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "material not found")
    return {"ok": True, "material": out}


@router.delete("/meals-catalog/materials/{mat_id}")
async def materials_delete(mat_id: str) -> dict:
    res = meals_cat_mod.delete_material(mat_id)
    if not res.get("removed"):
        raise HTTPException(404, "material not found")
    return {"ok": True, **res}


# ----- Custom Meals · Subscriptions + Plans -------------------------------
# 6 standard plans ship with the seed. Subscriptions are per-client
# instances that lock in a plan + price + delivery window.

@router.get("/cm-subscriptions")
async def cm_subscriptions_get() -> dict:
    data = cm_subs_mod.load_data()
    return {
        "ok":             True,
        "plans":          data["plans"],
        "subscriptions":  data["subscriptions"],
        "billing_cycles": cm_subs_mod.BILLING_CYCLES,
        "delivery_windows": cm_subs_mod.DELIVERY_WINDOWS,
        "statuses":       cm_subs_mod.SUBSCRIPTION_STATUSES,
        "payment_statuses": cm_subs_mod.PAYMENT_STATUSES,
    }


@router.post("/cm-subscriptions/reset")
async def cm_subscriptions_reset() -> dict:
    data = cm_subs_mod.reset_subscriptions()
    return {"ok": True, "plans": data["plans"], "subscriptions": data["subscriptions"]}


class PlanIn(BaseModel):
    id:                 Optional[str] = None
    code:               Optional[str] = None
    name_en:            Optional[str] = None
    name_ar:            Optional[str] = None
    description_en:     Optional[str] = None
    description_ar:     Optional[str] = None
    meals_per_week:     Optional[int] = None
    meal_slots:         Optional[list[str]] = None
    required_diet_tags: Optional[list[str]] = None
    excluded_allergens: Optional[list[str]] = None
    calorie_target:     Optional[int] = None
    price_weekly:       Optional[float] = None
    price_monthly:      Optional[float] = None
    price_quarterly:    Optional[float] = None
    currency:           Optional[str] = None
    active:             Optional[bool] = None
    sort_order:         Optional[int] = None


@router.post("/cm-subscriptions/plans")
async def cm_plan_add(payload: PlanIn) -> dict:
    try:
        plan = cm_subs_mod.add_plan(payload.model_dump(exclude_none=True))
    except cm_subs_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "plan": plan}


@router.patch("/cm-subscriptions/plans/{plan_id}")
async def cm_plan_update(plan_id: str, payload: PlanIn) -> dict:
    try:
        out = cm_subs_mod.update_plan(plan_id, payload.model_dump(exclude_none=True))
    except cm_subs_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "plan not found")
    return {"ok": True, "plan": out}


@router.delete("/cm-subscriptions/plans/{plan_id}")
async def cm_plan_delete(plan_id: str) -> dict:
    res = cm_subs_mod.delete_plan(plan_id)
    if not res.get("removed"):
        raise HTTPException(404, "plan not found")
    return {"ok": True, **res}


class SubscriptionIn(BaseModel):
    id:                Optional[str]  = None
    client_id:         Optional[str]  = None
    plan_id:           Optional[str]  = None
    start_date:        Optional[str]  = None
    end_date:          Optional[str]  = None
    billing_cycle:     Optional[str]  = None
    price:             Optional[float] = None
    currency:          Optional[str]  = None
    status:            Optional[str]  = None
    payment_status:    Optional[str]  = None
    delivery_address:  Optional[str]  = None
    delivery_window:   Optional[str]  = None
    meal_overrides:    Optional[list[str]] = None
    extra_allergens:   Optional[list[str]] = None
    notes:             Optional[str]  = None
    created_at:        Optional[int]  = None


@router.post("/cm-subscriptions/subs")
async def cm_subscription_add(payload: SubscriptionIn) -> dict:
    try:
        sub = cm_subs_mod.add_subscription(payload.model_dump(exclude_none=True))
    except cm_subs_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "subscription": sub}


@router.patch("/cm-subscriptions/subs/{sub_id}")
async def cm_subscription_update(sub_id: str, payload: SubscriptionIn) -> dict:
    out = cm_subs_mod.update_subscription(sub_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "subscription not found")
    return {"ok": True, "subscription": out}


@router.delete("/cm-subscriptions/subs/{sub_id}")
async def cm_subscription_delete(sub_id: str) -> dict:
    if not cm_subs_mod.delete_subscription(sub_id):
        raise HTTPException(404, "subscription not found")
    return {"ok": True, "removed": True}


# ----- Custom Meals · Schedules -------------------------------------------
# Derived from active subscriptions but persisted so the operator can
# hand-edit (swap meal, mark delivered, skip). Reset regenerates from
# scratch for the next 7 days.

@router.get("/cm-schedules")
async def cm_schedules_get() -> dict:
    return {"ok": True, "schedules": schedules_mod.load_schedules()}


@router.post("/cm-schedules/reset")
async def cm_schedules_reset() -> dict:
    return {"ok": True, "schedules": schedules_mod.reset_schedules()}


class ScheduleRowIn(BaseModel):
    meal_id: Optional[str] = None
    status:  Optional[str] = None
    notes:   Optional[str] = None
    date:    Optional[str] = None
    meal_slot: Optional[str] = None


@router.patch("/cm-schedules/{row_id}")
async def cm_schedule_update(row_id: str, payload: ScheduleRowIn) -> dict:
    out = schedules_mod.update_row(row_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "schedule row not found")
    return {"ok": True, "row": out}


@router.delete("/cm-schedules/{row_id}")
async def cm_schedule_delete(row_id: str) -> dict:
    if not schedules_mod.delete_row(row_id):
        raise HTTPException(404, "schedule row not found")
    return {"ok": True, "removed": True}


# ============================================================================
# Custom Meals — Weekly Prep aggregator.
# ============================================================================

@router.get("/cm-weekly-prep")
async def cm_weekly_prep_get(date: Optional[str] = None,
                              days: int = 7) -> dict:
    """Per-day prep brief for the kitchen — aggregates every scheduled
    meal in the window (default: today + next 6 days) by meal, lists
    portion totals, allergen + dietary footprint, and a raw-material
    burn forecast based on each meal's `material_ids` × portion size."""
    import time as _t
    if days < 1: days = 1
    if days > 30: days = 30

    if date:
        try:
            base = _t.mktime(_t.strptime(date, "%Y-%m-%d"))
        except Exception:
            raise HTTPException(400, "date must be YYYY-MM-DD")
    else:
        n = _t.localtime()
        base = _t.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, n.tm_isdst))

    dates_window: list[str] = []
    for i in range(days):
        t = _t.localtime(base + i * 86400)
        dates_window.append(f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}")

    schedules = schedules_mod.load_schedules()
    cat = meals_cat_mod.load_catalog()
    meals_by_id    = {m["id"]: m for m in cat.get("meals", [])}
    materials_by_id = {x["id"]: x for x in cat.get("raw_materials", [])}
    clients = {c["id"]: c for c in clients_mod.load_clients()}

    buckets: dict[str, list[dict]] = {d: [] for d in dates_window}
    for r in schedules:
        d = r.get("date") or ""
        if d in buckets and (r.get("status") or "pending") != "skipped":
            buckets[d].append(r)

    days_out: list[dict] = []
    grand_material_kg: dict[str, float] = {}

    for d in dates_window:
        rows = buckets[d]
        # Roll up meals by meal_id.
        meal_roll: dict[str, dict] = {}
        allergens: set[str] = set()
        diets: set[str]     = set()
        client_set: set[str] = set()
        for r in rows:
            mid  = r.get("meal_id") or ""
            meal = meals_by_id.get(mid)
            if not meal: continue
            client_set.add(r.get("client_id") or "")
            for a in (meal.get("allergens") or []):
                if a: allergens.add(str(a).lower())
            for dt in (meal.get("diet_tags") or []):
                if dt: diets.add(str(dt).lower())
            slot = r.get("meal_slot") or "lunch"
            row = meal_roll.setdefault(mid, {
                "meal_id":    mid,
                "name_en":    meal.get("name_en") or "",
                "name_ar":    meal.get("name_ar") or "",
                "category":   meal.get("category") or "",
                "portion_g":  int(meal.get("portion_size_g") or 0),
                "qty":        0,
                "slots":      {},
                "clients":    [],
                "material_ids": list(meal.get("material_ids") or []),
            })
            row["qty"] += 1
            row["slots"][slot] = row["slots"].get(slot, 0) + 1
            cli = clients.get(r.get("client_id") or "")
            if cli:
                cname = cli.get("name") or cli.get("name_ar") or ""
                if cname and cname not in row["clients"]:
                    row["clients"].append(cname)

        # Material forecast — sum portion grams per material that appears.
        # We don't have per-meal material weights so the forecast is
        # "headcount-equivalent kg" = sum(portion_g * qty) / 1000, evenly
        # distributed across the meal's material_ids. It's a directional
        # estimate, not a strict BoM calc — kitchen leads round up.
        materials_today: dict[str, float] = {}
        for row in meal_roll.values():
            if not row["material_ids"]: continue
            kg = row["portion_g"] * row["qty"] / 1000.0
            per_mat = kg / len(row["material_ids"])
            for mid in row["material_ids"]:
                materials_today[mid] = materials_today.get(mid, 0) + per_mat
                grand_material_kg[mid] = grand_material_kg.get(mid, 0) + per_mat

        materials_brief = sorted(
            [
                {
                    "id":         mid,
                    "name_en":    (materials_by_id.get(mid) or {}).get("name_en") or mid,
                    "name_ar":    (materials_by_id.get(mid) or {}).get("name_ar") or "",
                    "unit":       (materials_by_id.get(mid) or {}).get("unit") or "",
                    "kg":         round(qty, 2),
                    "in_stock":   (materials_by_id.get(mid) or {}).get("current_stock") or 0,
                    "min_stock":  (materials_by_id.get(mid) or {}).get("min_stock") or 0,
                    "allergen_type": (materials_by_id.get(mid) or {}).get("allergen_type") or "",
                }
                for mid, qty in materials_today.items()
            ],
            key=lambda x: -x["kg"],
        )

        days_out.append({
            "date":           d,
            "weekday":        _t.strftime("%a", _t.strptime(d, "%Y-%m-%d")),
            "meal_count":     sum(int(x["qty"]) for x in meal_roll.values()),
            "distinct_meals": len(meal_roll),
            "client_count":   len([c for c in client_set if c]),
            "allergens":      sorted(allergens),
            "diet_tags":      sorted(diets),
            "meals":          sorted(meal_roll.values(), key=lambda x: -x["qty"]),
            "materials":      materials_brief,
        })

    # Grand totals across the window — used for the "this week you need"
    # summary at the top of the page.
    grand_materials = sorted(
        [
            {
                "id":         mid,
                "name_en":    (materials_by_id.get(mid) or {}).get("name_en") or mid,
                "unit":       (materials_by_id.get(mid) or {}).get("unit") or "",
                "kg":         round(qty, 2),
                "in_stock":   (materials_by_id.get(mid) or {}).get("current_stock") or 0,
                "min_stock":  (materials_by_id.get(mid) or {}).get("min_stock") or 0,
                "supplier":   (materials_by_id.get(mid) or {}).get("supplier") or "",
            }
            for mid, qty in grand_material_kg.items()
        ],
        key=lambda x: -x["kg"],
    )

    return {
        "ok":             True,
        "start_date":     dates_window[0],
        "days":           days_out,
        "grand_materials": grand_materials,
        "totals": {
            "portions":       sum(d["meal_count"] for d in days_out),
            "distinct_meals": len({m["meal_id"] for d in days_out for m in d["meals"]}),
            "clients_served": len({c for d in days_out for m in d["meals"] for c in m["clients"]}),
        },
    }


# ============================================================================
# Custom Meals — Delivery (drivers + per-subscription assignments + manifest).
# ============================================================================

@router.get("/cm-delivery")
async def cm_delivery_get() -> dict:
    payload = cm_dlv_mod.load_all()
    return {
        "ok":            True,
        "drivers":       sorted(payload.get("drivers") or [],
                                 key=lambda r: (r.get("name") or "").lower()),
        "assignments":   payload.get("assignments") or [],
        "vehicle_types": cm_dlv_mod.VEHICLE_TYPES,
        "statuses":      cm_dlv_mod.DRIVER_STATUSES,
    }


@router.post("/cm-delivery/reset")
async def cm_delivery_reset() -> dict:
    return {"ok": True, **cm_dlv_mod.reset_delivery()}


class CmDriverIn(BaseModel):
    id:              Optional[str]   = None
    name:            Optional[str]   = None
    name_ar:         Optional[str]   = None
    phone:           Optional[str]   = None
    license_no:      Optional[str]   = None
    vehicle_type:    Optional[str]   = None
    vehicle_plate:   Optional[str]   = None
    shift:           Optional[str]   = None
    capacity_orders: Optional[int]   = None
    zones:           Optional[list[str]] = None
    status:          Optional[str]   = None
    rating:          Optional[float] = None
    active:          Optional[bool]  = None
    notes:           Optional[str]   = None


@router.post("/cm-delivery/drivers")
async def cm_driver_add(payload: CmDriverIn) -> dict:
    try:
        d = cm_dlv_mod.add_driver(payload.model_dump(exclude_none=True))
    except cm_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "driver": d}


@router.patch("/cm-delivery/drivers/{driver_id}")
async def cm_driver_update(driver_id: str, payload: CmDriverIn) -> dict:
    try:
        out = cm_dlv_mod.update_driver(driver_id,
                                        payload.model_dump(exclude_none=True))
    except cm_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "driver not found")
    return {"ok": True, "driver": out}


@router.delete("/cm-delivery/drivers/{driver_id}")
async def cm_driver_delete(driver_id: str) -> dict:
    if not cm_dlv_mod.delete_driver(driver_id):
        raise HTTPException(404, "driver not found")
    return {"ok": True, "removed": True}


class CmAssignmentIn(BaseModel):
    subscription_id: str
    date:            str
    driver_id:       Optional[str] = None
    depart_time:     Optional[str] = None
    arrive_time:     Optional[str] = None
    route_note:      Optional[str] = None


@router.post("/cm-delivery/assignments")
async def cm_assignment_upsert(payload: CmAssignmentIn) -> dict:
    try:
        a = cm_dlv_mod.upsert_assignment(payload.model_dump(exclude_none=True))
    except cm_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "assignment": a}


@router.delete("/cm-delivery/assignments")
async def cm_assignment_delete(subscription_id: str, date: str) -> dict:
    if not cm_dlv_mod.delete_assignment(subscription_id, date):
        raise HTTPException(404, "assignment not found")
    return {"ok": True, "removed": True}


@router.get("/cm-delivery/manifest")
async def cm_delivery_manifest(date: Optional[str] = None) -> dict:
    """Build the daily delivery manifest. For the given date (defaults to
    today) we walk every active subscription, join its client + the
    scheduled meals for that day, and either pin the explicit driver
    assignment OR auto-suggest a driver whose preferred zones cover the
    client's district. Auto-suggested rows have `auto_assigned=true` so
    the operator can see what's pinned vs computed."""
    import time as _t
    if date:
        try:
            _t.strptime(date, "%Y-%m-%d")
        except Exception:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        target = date
    else:
        n = _t.localtime()
        target = f"{n.tm_year:04d}-{n.tm_mon:02d}-{n.tm_mday:02d}"

    sub_data = cm_subs_mod.load_data()
    plans_by_id = {p["id"]: p for p in sub_data.get("plans", [])}
    clients = {c["id"]: c for c in clients_mod.load_clients()}
    schedules = schedules_mod.load_schedules()
    schedules_by_sub_day: dict[tuple, list[dict]] = {}
    for r in schedules:
        if (r.get("date") or "") != target:
            continue
        if (r.get("status") or "pending") == "skipped":
            continue
        key = (r.get("subscription_id"), target)
        schedules_by_sub_day.setdefault(key, []).append(r)

    payload = cm_dlv_mod.load_all()
    drivers = {d.get("id"): d for d in (payload.get("drivers") or [])}
    pinned: dict[tuple, dict] = {}
    for a in (payload.get("assignments") or []):
        pinned[(a.get("subscription_id"), a.get("date"))] = a

    routes: list[dict] = []
    unassigned: list[dict] = []
    by_driver: dict[str, list[dict]] = {}

    for sub in sub_data.get("subscriptions", []):
        if (sub.get("status") or "").lower() != "active":
            continue
        rows = schedules_by_sub_day.get((sub["id"], target)) or []
        if not rows:
            continue   # no meals today for this sub — skip
        cli = clients.get(sub.get("client_id")) or {}
        address = sub.get("delivery_address") or cli.get("city") or ""
        # Auto-pick a driver whose zones include the address (substring).
        auto_drv_id = ""
        if address:
            lower_addr = address.lower()
            for d in drivers.values():
                if not d.get("active"): continue
                for z in (d.get("zones") or []):
                    if z and z.lower() in lower_addr:
                        auto_drv_id = d["id"]
                        break
                if auto_drv_id: break

        pin = pinned.get((sub["id"], target))
        drv_id = (pin or {}).get("driver_id") or auto_drv_id
        drv = drivers.get(drv_id) if drv_id else None
        row = {
            "subscription_id": sub["id"],
            "client_id":       sub.get("client_id"),
            "client_name":     cli.get("name") or cli.get("name_ar") or "",
            "client_phone":    cli.get("phone") or "",
            "client_allergies": cli.get("allergies") or "",
            "delivery_address": address,
            "delivery_window":  sub.get("delivery_window") or "anytime",
            "plan":             (plans_by_id.get(sub.get("plan_id")) or {}).get("name_en") or "",
            "meal_count":       len(rows),
            "meals":            [{"slot": r.get("meal_slot"),
                                   "name_en": r.get("meal_name_en"),
                                   "name_ar": r.get("meal_name_ar"),
                                   "status": r.get("status")} for r in rows],
            "driver":           drv,
            "auto_assigned":    bool(auto_drv_id and not pin),
            "depart_time":      (pin or {}).get("depart_time") or "",
            "arrive_time":      (pin or {}).get("arrive_time") or "",
            "route_note":       (pin or {}).get("route_note") or "",
            "pinned":           bool(pin),
        }
        if drv:
            routes.append(row)
            by_driver.setdefault(drv["id"], []).append(row)
        else:
            unassigned.append(row)

    # Sort by delivery window then client name.
    _WINDOW_RANK = {"morning": 0, "afternoon": 1, "evening": 2, "anytime": 3}
    def _route_key(r: dict) -> tuple:
        return (r.get("depart_time") or "99:99",
                 _WINDOW_RANK.get(r.get("delivery_window") or "anytime", 9),
                 (r.get("client_name") or "").lower())
    routes.sort(key=_route_key)
    unassigned.sort(key=_route_key)

    return {
        "ok":         True,
        "date":       target,
        "routes":     routes,
        "unassigned": unassigned,
        "by_driver":  by_driver,
        "totals": {
            "routes":         len(routes),
            "unassigned":     len(unassigned),
            "drivers_in_use": len(by_driver),
            "meals_total":    sum(r["meal_count"] for r in routes + unassigned),
        },
    }


# ----- Catering · Orders + Overview ---------------------------------------
# Catering doesn't share the Custom Meals clients DB — each order carries
# the customer's name/phone/email inline. Repeat customers are detected
# in the Overview/Insights pages by phone-digit match.

@router.get("/catering/orders")
async def catering_orders_get() -> dict:
    return {
        "ok":               True,
        "orders":           catering_mod.load_orders(),
        "event_types":      catering_mod.EVENT_TYPES,
        "package_types":    catering_mod.PACKAGE_TYPES,
        "order_statuses":   catering_mod.ORDER_STATUSES,
        "payment_statuses": catering_mod.PAYMENT_STATUSES,
    }


@router.post("/catering/orders/reset")
async def catering_orders_reset() -> dict:
    return {"ok": True, "orders": catering_mod.reset_orders()}


class CateringItemIn(BaseModel):
    name_en:    Optional[str]  = None
    name_ar:    Optional[str]  = None
    qty:        Optional[int]  = None
    unit_price: Optional[float] = None
    currency:   Optional[str]  = None


class CateringOrderIn(BaseModel):
    id:               Optional[str]  = None
    client_name:      Optional[str]  = None
    client_name_ar:   Optional[str]  = None
    client_phone:     Optional[str]  = None
    client_email:     Optional[str]  = None
    client_company:   Optional[str]  = None
    event_date:       Optional[str]  = None
    event_time:       Optional[str]  = None
    event_type:       Optional[str]  = None
    package:          Optional[str]  = None
    venue:            Optional[str]  = None
    guests:           Optional[int]  = None
    items:            Optional[list[CateringItemIn]] = None
    per_guest_price:  Optional[float] = None
    delivery_fee:     Optional[float] = None
    deposit_paid:     Optional[float] = None
    amount_paid:      Optional[float] = None
    currency:         Optional[str]  = None
    status:           Optional[str]  = None
    payment_status:   Optional[str]  = None
    dietary:          Optional[list[str]] = None
    allergens:        Optional[list[str]] = None
    notes:            Optional[str]  = None
    created_at:       Optional[int]  = None


def _order_in_to_dict(p: CateringOrderIn) -> dict:
    d = p.model_dump(exclude_none=True)
    if "items" in d:
        d["items"] = [(i if isinstance(i, dict) else dict(i)) for i in d["items"]]
    return d


@router.post("/catering/orders")
async def catering_order_add(payload: CateringOrderIn) -> dict:
    try:
        o = catering_mod.add_order(_order_in_to_dict(payload))
    except catering_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "order": o}


@router.patch("/catering/orders/{order_id}")
async def catering_order_update(order_id: str, payload: CateringOrderIn) -> dict:
    try:
        out = catering_mod.update_order(order_id, _order_in_to_dict(payload))
    except catering_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "order not found")
    return {"ok": True, "order": out}


@router.delete("/catering/orders/{order_id}")
async def catering_order_delete(order_id: str) -> dict:
    if not catering_mod.delete_order(order_id):
        raise HTTPException(404, "order not found")
    return {"ok": True, "removed": True}


@router.get("/catering/overview")
async def catering_overview_get() -> dict:
    """Aggregated dashboard payload for Catering → Overview. Everything
    is computed fresh from the orders file so changes to Orders flow
    here on next request."""
    import time as _t
    orders = catering_mod.load_orders()

    by_status: dict[str, int] = {s: 0 for s in catering_mod.ORDER_STATUSES}
    by_type:   dict[str, int] = {}
    by_package: dict[str, int] = {}
    confirmed_pipeline_total = 0.0
    open_pipeline_total      = 0.0   # confirmed + inquiry (forward-looking)
    completed_revenue_30d    = 0.0
    completed_revenue_total  = 0.0
    today_str = _t.strftime("%Y-%m-%d", _t.localtime())
    in_progress = 0
    upcoming_14d: list[dict] = []
    in_progress_rows: list[dict] = []

    cutoff_30d = _t.time() - 30 * 86400

    for o in orders:
        st = (o.get("status") or "").lower()
        if st in by_status:
            by_status[st] += 1
        et = (o.get("event_type") or "other")
        by_type[et] = by_type.get(et, 0) + 1
        pk = (o.get("package") or "custom")
        by_package[pk] = by_package.get(pk, 0) + 1

        total = float(o.get("total") or 0)
        if st == "confirmed":
            confirmed_pipeline_total += total
        if st in ("inquiry", "confirmed"):
            open_pipeline_total += total
        if st in ("in_kitchen", "out_for_delivery"):
            in_progress += 1
            in_progress_rows.append(o)
        if st in ("delivered", "completed"):
            completed_revenue_total += total
            if (o.get("created_at") or 0) >= cutoff_30d:
                completed_revenue_30d += total

        # Upcoming = event_date in [today, today+14)
        ed = o.get("event_date") or ""
        if ed >= today_str:
            try:
                ed_ts = _t.mktime(_t.strptime(ed, "%Y-%m-%d"))
            except Exception:
                ed_ts = 0
            if ed_ts and (ed_ts - _t.time()) <= 14 * 86400 and st not in ("cancelled",):
                upcoming_14d.append(o)

    upcoming_14d.sort(key=lambda r: (r.get("event_date") or "", r.get("event_time") or ""))
    in_progress_rows.sort(key=lambda r: r.get("event_date") or "")

    return {
        "ok":         True,
        "totals": {
            "orders_total":         len(orders),
            "by_status":            by_status,
            "by_type":              by_type,
            "by_package":           by_package,
            "in_progress":          in_progress,
            "confirmed_pipeline":   round(confirmed_pipeline_total, 2),
            "open_pipeline":        round(open_pipeline_total, 2),
            "completed_revenue_30d": round(completed_revenue_30d, 2),
            "completed_revenue_total": round(completed_revenue_total, 2),
            "currency":             catering_mod.DEFAULT_CURRENCY,
        },
        "upcoming_14d":    upcoming_14d,
        "in_progress":     in_progress_rows,
        "recent_orders":   sorted(orders, key=lambda r: r.get("created_at") or 0,
                                   reverse=True)[:6],
    }


# ============================================================================
# Catering — Menu (catering items catalog).
# ============================================================================

@router.get("/catering/menu")
async def catering_menu_get() -> dict:
    return {
        "ok":         True,
        "items":      catering_menu_mod.load_items(),
        "categories": catering_menu_mod.CATEGORIES,
        "unit_types": catering_menu_mod.UNIT_TYPES,
        "diet_tags":  catering_menu_mod.COMMON_DIET,
        "allergens":  catering_menu_mod.COMMON_ALLERGENS,
        "currency":   catering_menu_mod.DEFAULT_CURRENCY,
    }


@router.post("/catering/menu/reset")
async def catering_menu_reset() -> dict:
    return {"ok": True, "items": catering_menu_mod.reset_items()}


class CateringMenuItemIn(BaseModel):
    id:             Optional[str]  = None
    code:           Optional[str]  = None
    name_en:        Optional[str]  = None
    name_ar:        Optional[str]  = None
    description_en: Optional[str]  = None
    description_ar: Optional[str]  = None
    category:       Optional[str]  = None
    unit_type:      Optional[str]  = None
    serves:         Optional[int]  = None
    prep_time_min:  Optional[int]  = None
    price:          Optional[float] = None
    currency:       Optional[str]  = None
    diet_tags:      Optional[list[str]] = None
    allergens:      Optional[list[str]] = None
    image_url:      Optional[str]  = None
    active:         Optional[bool] = None
    sort_order:     Optional[int]  = None


@router.post("/catering/menu")
async def catering_menu_add(payload: CateringMenuItemIn) -> dict:
    try:
        it = catering_menu_mod.add_item(payload.model_dump(exclude_none=True))
    except catering_menu_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "item": it}


@router.patch("/catering/menu/{item_id}")
async def catering_menu_update(item_id: str, payload: CateringMenuItemIn) -> dict:
    try:
        out = catering_menu_mod.update_item(item_id,
                                             payload.model_dump(exclude_none=True))
    except catering_menu_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "item not found")
    return {"ok": True, "item": out}


@router.delete("/catering/menu/{item_id}")
async def catering_menu_delete(item_id: str) -> dict:
    if not catering_menu_mod.delete_item(item_id):
        raise HTTPException(404, "item not found")
    return {"ok": True, "removed": True}


# ============================================================================
# Catering — Packages (templates bundling menu items).
# ============================================================================

@router.get("/catering/packages")
async def catering_packages_get() -> dict:
    return {
        "ok":             True,
        "packages":       catering_pkg_mod.load_packages(),
        "event_types":    catering_pkg_mod.EVENT_TYPES,
        "service_styles": catering_pkg_mod.SERVICE_STYLES,
        "menu_items":     catering_menu_mod.load_items(),  # for code lookup
        "currency":       catering_pkg_mod.DEFAULT_CURRENCY,
    }


@router.post("/catering/packages/reset")
async def catering_packages_reset() -> dict:
    return {"ok": True, "packages": catering_pkg_mod.reset_packages()}


class CateringPackageItemIn(BaseModel):
    code:       Optional[str] = None
    qty_basis:  Optional[str] = None
    qty:        Optional[int] = None
    block_size: Optional[int] = None
    note:       Optional[str] = None


class CateringPackageIn(BaseModel):
    id:                 Optional[str]   = None
    code:               Optional[str]   = None
    name_en:            Optional[str]   = None
    name_ar:            Optional[str]   = None
    description_en:     Optional[str]   = None
    description_ar:     Optional[str]   = None
    event_type:         Optional[str]   = None
    service_style:      Optional[str]   = None
    min_guests:         Optional[int]   = None
    max_guests:         Optional[int]   = None
    per_guest_price:    Optional[float] = None
    currency:           Optional[str]   = None
    prep_lead_days:     Optional[int]   = None
    items:              Optional[list[CateringPackageItemIn]] = None
    included_diet_tags: Optional[list[str]] = None
    tier:               Optional[str]   = None
    popular:            Optional[bool]  = None
    active:             Optional[bool]  = None
    sort_order:         Optional[int]   = None


def _pkg_in_to_dict(p: CateringPackageIn) -> dict:
    d = p.model_dump(exclude_none=True)
    if "items" in d:
        d["items"] = [(i if isinstance(i, dict) else dict(i)) for i in d["items"]]
    return d


@router.post("/catering/packages")
async def catering_package_add(payload: CateringPackageIn) -> dict:
    try:
        pk = catering_pkg_mod.add_package(_pkg_in_to_dict(payload))
    except catering_pkg_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "package": pk}


@router.patch("/catering/packages/{pkg_id}")
async def catering_package_update(pkg_id: str, payload: CateringPackageIn) -> dict:
    try:
        out = catering_pkg_mod.update_package(pkg_id, _pkg_in_to_dict(payload))
    except catering_pkg_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "package not found")
    return {"ok": True, "package": out}


@router.delete("/catering/packages/{pkg_id}")
async def catering_package_delete(pkg_id: str) -> dict:
    if not catering_pkg_mod.delete_package(pkg_id):
        raise HTTPException(404, "package not found")
    return {"ok": True, "removed": True}


# ============================================================================
# Catering — Calendar (aggregator over orders).
# ============================================================================

@router.get("/catering/calendar")
async def catering_calendar_get(year: Optional[int] = None,
                                  month: Optional[int] = None) -> dict:
    """Group catering orders by event_date for a given month (defaults to
    current month). Returns one entry per event with the minimum fields
    the calendar UI needs."""
    import time as _t
    now = _t.localtime()
    yr = int(year) if year else now.tm_year
    mo = int(month) if month else now.tm_mon
    if not (1 <= mo <= 12):
        raise HTTPException(400, "month out of range")

    orders = catering_mod.load_orders()
    month_prefix = f"{yr:04d}-{mo:02d}"

    days: dict[str, list[dict]] = {}
    totals_by_status: dict[str, int] = {s: 0 for s in catering_mod.ORDER_STATUSES}
    revenue_in_month = 0.0
    guests_in_month  = 0
    events_in_month  = 0

    for o in orders:
        ed = o.get("event_date") or ""
        if not ed.startswith(month_prefix):
            continue
        events_in_month += 1
        st = (o.get("status") or "").lower()
        if st in totals_by_status:
            totals_by_status[st] += 1
        revenue_in_month += float(o.get("total") or 0)
        guests_in_month  += int(o.get("guests") or 0)
        days.setdefault(ed, []).append({
            "id":           o.get("id"),
            "client_name":  o.get("client_name"),
            "event_time":   o.get("event_time"),
            "event_type":   o.get("event_type"),
            "package":      o.get("package"),
            "venue":        o.get("venue"),
            "guests":       o.get("guests"),
            "status":       o.get("status"),
            "total":        o.get("total"),
            "currency":     o.get("currency"),
        })

    for d in days.values():
        d.sort(key=lambda r: (r.get("event_time") or "00:00", r.get("id") or ""))

    return {
        "ok":              True,
        "year":            yr,
        "month":           mo,
        "days":            days,
        "totals": {
            "events":  events_in_month,
            "guests":  guests_in_month,
            "revenue": round(revenue_in_month, 2),
            "by_status": totals_by_status,
            "currency": catering_mod.DEFAULT_CURRENCY,
        },
    }


# ============================================================================
# Catering — Kitchen Prep (aggregator over orders + catering_menu).
# ============================================================================

@router.get("/catering/kitchen-prep")
async def catering_kitchen_prep_get(date: Optional[str] = None,
                                     days: int = 7) -> dict:
    """Aggregate every order in the next `days` (default 7) starting at
    `date` (YYYY-MM-DD; defaults today) into a per-day prep brief:
        - line items consolidated by name (qty + earliest event_time)
        - allergen brief — union of all allergens present in the day
        - dietary brief  — union of all dietary tags present in the day
        - headcount sum, event count
    The SPA renders this directly as a chef-friendly daily checklist."""
    import time as _t
    if days < 1: days = 1
    if days > 30: days = 30

    if date:
        try:
            base = _t.mktime(_t.strptime(date, "%Y-%m-%d"))
        except Exception:
            raise HTTPException(400, "date must be YYYY-MM-DD")
    else:
        n = _t.localtime()
        base = _t.mktime((n.tm_year, n.tm_mon, n.tm_mday, 0, 0, 0, 0, 0, n.tm_isdst))

    dates_window: list[str] = []
    for i in range(days):
        t = _t.localtime(base + i * 86400)
        dates_window.append(f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}")

    orders = catering_mod.load_orders()
    # Bucket by date.
    buckets: dict[str, list[dict]] = {d: [] for d in dates_window}
    for o in orders:
        ed = o.get("event_date") or ""
        if ed in buckets:
            if (o.get("status") or "") in ("cancelled",):
                continue
            buckets[ed].append(o)

    days_out: list[dict] = []
    for d in dates_window:
        rows = buckets[d]
        guest_sum = sum(int(r.get("guests") or 0) for r in rows)
        # Roll up items by EN name.
        item_roll: dict[str, dict] = {}
        allergens: set[str] = set()
        diets: set[str] = set()
        earliest_time = ""
        for r in rows:
            for a in (r.get("allergens") or []):
                if a: allergens.add(str(a).lower())
            for dt in (r.get("dietary") or []):
                if dt: diets.add(str(dt).lower())
            t = r.get("event_time") or ""
            if t and (not earliest_time or t < earliest_time):
                earliest_time = t
            for it in (r.get("items") or []):
                key = (it.get("name_en") or it.get("name_ar") or "?").strip()
                if not key: continue
                row = item_roll.setdefault(key, {
                    "name_en":   it.get("name_en") or "",
                    "name_ar":   it.get("name_ar") or "",
                    "qty":       0,
                    "orders":    [],
                })
                row["qty"] += int(it.get("qty") or 0)
                row["orders"].append({
                    "order_id":    r.get("id"),
                    "client_name": r.get("client_name"),
                    "event_time":  r.get("event_time"),
                    "qty":         it.get("qty"),
                })
        items_sorted = sorted(item_roll.values(), key=lambda x: -x["qty"])
        days_out.append({
            "date":          d,
            "event_count":   len(rows),
            "guest_sum":     guest_sum,
            "earliest_time": earliest_time,
            "allergens":     sorted(allergens),
            "dietary":       sorted(diets),
            "items":         items_sorted,
            "events":        [
                {"id": r.get("id"), "client_name": r.get("client_name"),
                  "event_time": r.get("event_time"), "venue": r.get("venue"),
                  "guests": r.get("guests"), "status": r.get("status"),
                  "package": r.get("package"), "event_type": r.get("event_type")}
                for r in rows
            ],
        })

    return {
        "ok":         True,
        "start_date": dates_window[0],
        "days":       days_out,
        "currency":   catering_mod.DEFAULT_CURRENCY,
    }


# ============================================================================
# Catering — Delivery (drivers + per-order assignments + daily manifest).
# ============================================================================

@router.get("/catering/delivery")
async def catering_delivery_get() -> dict:
    payload = catering_dlv_mod.load_all()
    return {
        "ok":             True,
        "drivers":        sorted(payload.get("drivers") or [],
                                  key=lambda r: (r.get("name") or "").lower()),
        "assignments":    payload.get("assignments") or [],
        "vehicle_types":  catering_dlv_mod.VEHICLE_TYPES,
        "statuses":       catering_dlv_mod.DRIVER_STATUSES,
    }


@router.post("/catering/delivery/reset")
async def catering_delivery_reset() -> dict:
    return {"ok": True, **catering_dlv_mod.reset_delivery()}


class CateringDriverIn(BaseModel):
    id:            Optional[str]   = None
    name:          Optional[str]   = None
    name_ar:       Optional[str]   = None
    phone:         Optional[str]   = None
    license_no:    Optional[str]   = None
    vehicle_type:  Optional[str]   = None
    vehicle_plate: Optional[str]   = None
    capacity_kg:   Optional[int]   = None
    zones:         Optional[list[str]] = None
    status:        Optional[str]   = None
    rating:        Optional[float] = None
    active:        Optional[bool]  = None
    notes:         Optional[str]   = None


@router.post("/catering/delivery/drivers")
async def catering_driver_add(payload: CateringDriverIn) -> dict:
    try:
        d = catering_dlv_mod.add_driver(payload.model_dump(exclude_none=True))
    except catering_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "driver": d}


@router.patch("/catering/delivery/drivers/{driver_id}")
async def catering_driver_update(driver_id: str, payload: CateringDriverIn) -> dict:
    try:
        out = catering_dlv_mod.update_driver(driver_id,
                                              payload.model_dump(exclude_none=True))
    except catering_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "driver not found")
    return {"ok": True, "driver": out}


@router.delete("/catering/delivery/drivers/{driver_id}")
async def catering_driver_delete(driver_id: str) -> dict:
    if not catering_dlv_mod.delete_driver(driver_id):
        raise HTTPException(404, "driver not found")
    return {"ok": True, "removed": True}


class CateringAssignmentIn(BaseModel):
    order_id:    str
    driver_id:   Optional[str] = None
    depart_time: Optional[str] = None
    arrive_time: Optional[str] = None
    route_note:  Optional[str] = None
    status:      Optional[str] = None


@router.post("/catering/delivery/assignments")
async def catering_assignment_upsert(payload: CateringAssignmentIn) -> dict:
    try:
        a = catering_dlv_mod.upsert_assignment(payload.model_dump(exclude_none=True))
    except catering_dlv_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "assignment": a}


@router.delete("/catering/delivery/assignments/{order_id}")
async def catering_assignment_delete(order_id: str) -> dict:
    if not catering_dlv_mod.delete_assignment(order_id):
        raise HTTPException(404, "assignment not found")
    return {"ok": True, "removed": True}


@router.get("/catering/delivery/manifest")
async def catering_delivery_manifest(date: Optional[str] = None) -> dict:
    """Build the dispatch manifest for `date` (YYYY-MM-DD, defaults today).
    Joins active catering orders (status in ready-to-deliver set) with
    their assignment + driver. Orders that haven't been assigned yet are
    listed under `unassigned`."""
    import time as _t
    if date:
        try:
            _t.strptime(date, "%Y-%m-%d")
        except Exception:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        target_date = date
    else:
        n = _t.localtime()
        target_date = f"{n.tm_year:04d}-{n.tm_mon:02d}-{n.tm_mday:02d}"

    orders = [o for o in catering_mod.load_orders()
              if (o.get("event_date") or "") == target_date
              and (o.get("status") or "") not in ("cancelled", "inquiry")]
    payload = catering_dlv_mod.load_all()
    drivers = {d.get("id"): d for d in (payload.get("drivers") or [])}
    assigns = {a.get("order_id"): a for a in (payload.get("assignments") or [])}

    routes: list[dict] = []
    unassigned: list[dict] = []
    by_driver: dict[str, list[dict]] = {}

    for o in orders:
        a = assigns.get(o.get("id"))
        if not a or not a.get("driver_id"):
            unassigned.append(o)
            continue
        drv = drivers.get(a.get("driver_id"))
        if not drv:
            unassigned.append(o)
            continue
        row = {
            "order":       o,
            "assignment":  a,
            "driver":      drv,
        }
        routes.append(row)
        by_driver.setdefault(drv["id"], []).append(row)

    routes.sort(key=lambda r: (r["assignment"].get("depart_time") or "99:99",
                                 r["order"].get("event_time") or "99:99"))
    unassigned.sort(key=lambda r: r.get("event_time") or "")

    return {
        "ok":          True,
        "date":        target_date,
        "routes":      routes,
        "unassigned":  unassigned,
        "by_driver":   by_driver,
        "totals": {
            "routes":     len(routes),
            "unassigned": len(unassigned),
            "drivers_in_use": len(by_driver),
        },
    }


# ============================================================================
# Kitchen — back-of-house layout, staff live positions, appliances.
# ============================================================================

# ----- Zones (back-of-house layout) -----

@router.get("/kitchen/layout")
async def kitchen_layout_get() -> dict:
    layout = kzones_mod.load_layout()
    return {
        "ok":         True,
        "floors":     layout["floors"],
        "zones":      layout["zones"],
        "canvas":     layout["canvas"],
        "zone_kinds": kzones_mod.ZONE_KINDS,
    }


@router.post("/kitchen/layout/reset")
async def kitchen_layout_reset() -> dict:
    return {"ok": True, **kzones_mod.reset_layout()}


class KitchenZoneIn(BaseModel):
    id:         Optional[str] = None
    name_en:    Optional[str] = None
    name_ar:    Optional[str] = None
    kind:       Optional[str] = None
    x:          Optional[int] = None
    y:          Optional[int] = None
    width:      Optional[int] = None
    height:     Optional[int] = None
    color:      Optional[str] = None
    sort_order: Optional[int] = None


@router.post("/kitchen/layout/zones")
async def kitchen_zone_add(payload: KitchenZoneIn) -> dict:
    try:
        z = kzones_mod.add_zone(payload.model_dump(exclude_none=True))
    except kzones_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "zone": z}


@router.patch("/kitchen/layout/zones/{zone_id}")
async def kitchen_zone_update(zone_id: str, payload: KitchenZoneIn) -> dict:
    out = kzones_mod.update_zone(zone_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "zone not found")
    return {"ok": True, "zone": out}


@router.delete("/kitchen/layout/zones/{zone_id}")
async def kitchen_zone_delete(zone_id: str) -> dict:
    if not kzones_mod.delete_zone(zone_id):
        raise HTTPException(404, "zone not found")
    return {"ok": True, "removed": True}


# ----- Staff + live positions -----

@router.get("/kitchen/staff")
async def kitchen_staff_get() -> dict:
    return {
        "ok":       True,
        "staff":    kstaff_mod.load_staff(),
        "roles":    kstaff_mod.ROLES,
        "statuses": kstaff_mod.STATUSES,
    }


@router.post("/kitchen/staff/reset")
async def kitchen_staff_reset() -> dict:
    return {"ok": True, "staff": kstaff_mod.reset_staff()}


class KitchenStaffStepIn(BaseModel):
    zone_id: str
    dwell_s: int


class KitchenStaffIn(BaseModel):
    id:         Optional[str] = None
    name:       Optional[str] = None
    name_ar:    Optional[str] = None
    role:       Optional[str] = None
    phone:      Optional[str] = None
    beacon_id:  Optional[str] = None
    shift:      Optional[str] = None
    status:     Optional[str] = None
    tint:       Optional[str] = None
    schedule:   Optional[list[KitchenStaffStepIn]] = None
    active:     Optional[bool] = None
    notes:      Optional[str] = None


def _staff_in_to_dict(p: KitchenStaffIn) -> dict:
    d = p.model_dump(exclude_none=True)
    if "schedule" in d:
        d["schedule"] = [(s if isinstance(s, dict) else dict(s)) for s in d["schedule"]]
    return d


@router.post("/kitchen/staff")
async def kitchen_staff_add(payload: KitchenStaffIn) -> dict:
    try:
        s = kstaff_mod.add_staff(_staff_in_to_dict(payload))
    except kstaff_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "staff": s}


@router.patch("/kitchen/staff/{staff_id}")
async def kitchen_staff_update(staff_id: str, payload: KitchenStaffIn) -> dict:
    out = kstaff_mod.update_staff(staff_id, _staff_in_to_dict(payload))
    if out is None:
        raise HTTPException(404, "staff not found")
    return {"ok": True, "staff": out}


@router.delete("/kitchen/staff/{staff_id}")
async def kitchen_staff_delete(staff_id: str) -> dict:
    if not kstaff_mod.delete_staff(staff_id):
        raise HTTPException(404, "staff not found")
    return {"ok": True, "removed": True}


@router.get("/kitchen/positions")
async def kitchen_positions_get() -> dict:
    """Lightweight polling endpoint — returns current zone + (x, y) for
    every active staff member. Designed for ~2s polling intervals."""
    import time as _t
    return {
        "ok":         True,
        "as_of":      int(_t.time()),
        "positions":  kstaff_mod.live_positions(),
    }


# ----- Appliances + sensors -----

@router.get("/kitchen/appliances")
async def kitchen_appliances_get() -> dict:
    """Catalog + current synthesised sensor readings per appliance."""
    apps = kappl_mod.load_appliances()
    readings = [kappl_mod.current_reading(a) for a in apps]
    # Merge for SPA convenience.
    by_id = {r["appliance_id"]: r for r in readings}
    out = [{**a, **(by_id.get(a["id"]) or {})} for a in apps]
    return {
        "ok":          True,
        "appliances":  out,
        "types":       kappl_mod.APPLIANCE_TYPES,
        "alert_kinds": kappl_mod.ALERT_KINDS,
    }


@router.get("/kitchen/appliances/{app_id}/history")
async def kitchen_appliance_history(app_id: str, hours: int = 24) -> dict:
    if hours < 1 or hours > 72:
        raise HTTPException(400, "hours must be 1-72")
    h = kappl_mod.history_for(app_id, hours=hours)
    if not h:
        raise HTTPException(404, "appliance not found")
    return {"ok": True, **h}


@router.get("/kitchen/appliances/histories")
async def kitchen_appliances_histories(hours: int = 72) -> dict:
    """Batch endpoint — returns history samples for every appliance in a
    single round-trip. Used by the grid view's inline 3-day mini-graphs
    so we don't fan out N parallel requests per page load."""
    if hours < 1 or hours > 72:
        raise HTTPException(400, "hours must be 1-72")
    apps = kappl_mod.load_appliances()
    out: dict[str, list[dict]] = {}
    for a in apps:
        h = kappl_mod.history_for(a["id"], hours=hours)
        out[a["id"]] = h.get("samples", []) if h else []
    return {"ok": True, "hours": hours, "histories": out}


@router.post("/kitchen/appliances/reset")
async def kitchen_appliances_reset() -> dict:
    return {"ok": True, **kappl_mod.reset_catalog()}


class KitchenApplianceIn(BaseModel):
    id:              Optional[str]   = None
    name_en:         Optional[str]   = None
    name_ar:         Optional[str]   = None
    type:            Optional[str]   = None
    zone_id:         Optional[str]   = None
    model:           Optional[str]   = None
    age_years:       Optional[int]   = None
    target_temp_c:   Optional[float] = None
    alarm_low_c:     Optional[float] = None
    alarm_high_c:    Optional[float] = None
    rated_power_kw:  Optional[float] = None
    capacity_l:      Optional[int]   = None
    active:          Optional[bool]  = None
    installed_at:    Optional[str]   = None
    notes:           Optional[str]   = None


@router.post("/kitchen/appliances")
async def kitchen_appliance_add(payload: KitchenApplianceIn) -> dict:
    try:
        a = kappl_mod.add_appliance(payload.model_dump(exclude_none=True))
    except kappl_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "appliance": a}


@router.patch("/kitchen/appliances/{app_id}")
async def kitchen_appliance_update(app_id: str, payload: KitchenApplianceIn) -> dict:
    out = kappl_mod.update_appliance(app_id, payload.model_dump(exclude_none=True))
    if out is None:
        raise HTTPException(404, "appliance not found")
    return {"ok": True, "appliance": out}


@router.delete("/kitchen/appliances/{app_id}")
async def kitchen_appliance_delete(app_id: str) -> dict:
    if not kappl_mod.delete_appliance(app_id):
        raise HTTPException(404, "appliance not found")
    return {"ok": True, "removed": True}


@router.get("/kitchen/alerts")
async def kitchen_alerts_get() -> dict:
    """Seeded historical alerts + any LIVE alerts derived from the
    current sensor readings (temp out of band, etc.). Live alerts are
    not persisted — they re-derive each request."""
    import time as _t
    now = int(_t.time())
    persisted = kappl_mod.load_alerts()
    apps = kappl_mod.load_appliances()
    live: list[dict] = []
    for a in apps:
        r = kappl_mod.current_reading(a)
        if r.get("in_alarm"):
            # Skip if we already have a seeded unresolved temp_high for
            # this appliance — avoid duplicate noise.
            already = any(
                p for p in persisted
                if p.get("appliance_id") == a["id"]
                and p.get("kind") in ("temp_high", "temp_low")
                and not p.get("resolved")
            )
            if already: continue
            kind = "temp_high" if r["temp_c"] > (a.get("alarm_high_c") or 999) else "temp_low"
            live.append({
                "id":            f"LIVE-{a['id']}",
                "appliance_id":  a["id"],
                "kind":          kind,
                "severity":      "alarm",
                "at":            now,
                "message":       f"{a.get('name_en')} reading {r['temp_c']}°C "
                                 f"(target {a.get('target_temp_c')}°C, "
                                 f"alarm band {a.get('alarm_low_c')}..{a.get('alarm_high_c')}°C).",
                "resolved":      False,
                "resolved_at":   None,
                "live":          True,
            })
    return {
        "ok":        True,
        "alerts":    [{**a, "live": False} for a in persisted] + live,
        "open_count": sum(1 for a in (persisted + live) if not a.get("resolved")),
    }


@router.post("/kitchen/alerts/{alert_id}/resolve")
async def kitchen_alert_resolve(alert_id: str) -> dict:
    if not kappl_mod.resolve_alert(alert_id):
        raise HTTPException(404, "alert not found (or already resolved)")
    return {"ok": True, "resolved": True}


# ----- Overview aggregator -----

@router.get("/kitchen/overview")
async def kitchen_overview_get() -> dict:
    """Single payload for the Kitchen → Overview dashboard."""
    import time as _t
    now = int(_t.time())
    layout = kzones_mod.load_layout()
    zones  = layout["zones"]
    apps   = kappl_mod.load_appliances()
    readings = {a["id"]: kappl_mod.current_reading(a) for a in apps}
    staff = kstaff_mod.load_staff()
    on_shift = [s for s in staff if (s.get("status") or "") == "on_shift" and s.get("active")]
    positions = kstaff_mod.live_positions(now)

    # Zone occupancy (live)
    occ: dict[str, int] = {z["id"]: 0 for z in zones}
    for p in positions:
        zid = p.get("zone_id")
        if zid and zid in occ:
            occ[zid] += 1
    busiest = sorted(occ.items(), key=lambda x: -x[1])[:3]

    # Power totals
    total_kw  = round(sum((r.get("power_kw") or 0) for r in readings.values()), 2)
    rated_kw  = round(sum((a.get("rated_power_kw") or 0) for a in apps), 2)
    in_alarm  = [r for r in readings.values() if r.get("in_alarm")]

    # Average temp by category
    cold_temps = [r["temp_c"] for a, r in zip(apps, readings.values())
                  if a.get("type") in ("walk_in_cooler", "reach_in_fridge",
                                        "beverage_fridge", "prep_table_fridge")]
    freezer_temps = [r["temp_c"] for a, r in zip(apps, readings.values())
                     if a.get("type") in ("walk_in_freezer", "reach_in_freezer",
                                            "blast_chiller", "ice_maker")]

    # Door-open count right now
    doors_open = [a for a, r in zip(apps, readings.values())
                   if r.get("door_status") == "open"]

    # Recent alerts (top 5 unresolved)
    alerts_payload = await kitchen_alerts_get()
    open_alerts = [a for a in alerts_payload["alerts"] if not a.get("resolved")][:5]

    return {
        "ok": True,
        "as_of": now,
        "totals": {
            "zones":          len(zones),
            "appliances":     len(apps),
            "appliances_alarm": len(in_alarm),
            "staff_total":    len(staff),
            "staff_on_shift": len(on_shift),
            "doors_open":     len(doors_open),
            "power_kw_now":   total_kw,
            "power_kw_rated": rated_kw,
            "power_load_pct": round((total_kw / rated_kw * 100) if rated_kw else 0, 1),
            "open_alerts":    alerts_payload["open_count"],
        },
        "avg_temp": {
            "cold_c":     round(sum(cold_temps) / len(cold_temps), 1) if cold_temps else None,
            "freezer_c":  round(sum(freezer_temps) / len(freezer_temps), 1) if freezer_temps else None,
        },
        "busiest_zones": [
            {"zone_id": zid, "name_en": next((z["name_en"] for z in zones if z["id"] == zid), zid),
              "count": count}
            for zid, count in busiest
        ],
        "doors_open":   [{"id": a["id"], "name_en": a.get("name_en"),
                           "zone_id": a.get("zone_id")} for a in doors_open],
        "appliances_in_alarm": [
            {"id": r["appliance_id"],
              "name_en": next((a.get("name_en") for a in apps if a["id"] == r["appliance_id"]), ""),
              "temp_c": r["temp_c"],
              "alarm_high_c": r["alarm_high_c"],
              "alarm_low_c":  r["alarm_low_c"]}
            for r in in_alarm
        ],
        "open_alerts": open_alerts,
    }


# ============================================================================
# Kitchen — AI-Camera config (people + actions + camera→zone map) and
# the analyze endpoint that asks Gemini "who is doing what" on a Frigate
# snapshot. The Frigate URL + Gemini API key both live in state.json
# (shared with the global AI-Camera Playground), so this endpoint just
# pulls a snapshot, builds a structured prompt embedding the operator's
# people + actions catalog, and asks Gemini to return JSON.
# ============================================================================

@router.get("/kitchen/camera/config")
async def kitchen_camera_config_get() -> dict:
    cfg = kcam_mod.load_config()
    zones = kzones_mod.load_layout()["zones"]
    zone_label_by_id = {z["id"]: z["name_en"] for z in zones}
    # Annotate actions with the zone's human name for the UI.
    actions = [
        {**a, "station_name":
            zone_label_by_id.get(a.get("station_zone_id") or "", "")}
        for a in cfg["actions"]
    ]
    return {
        "ok": True,
        "people":             cfg["people"],
        "actions":            actions,
        "camera_assignments": cfg["camera_assignments"],
        "zones":              [{"id": z["id"], "name_en": z["name_en"]} for z in zones],
    }


@router.post("/kitchen/camera/config/reset")
async def kitchen_camera_config_reset() -> dict:
    kcam_mod.reset_config()
    return await kitchen_camera_config_get()


# ----- People CRUD -----

class KitchenPersonIn(BaseModel):
    id:          Optional[str]  = None
    name:        Optional[str]  = None
    role:        Optional[str]  = None
    description: Optional[str]  = None
    active:      Optional[bool] = None


@router.post("/kitchen/camera/config/people")
async def kitchen_person_add(payload: KitchenPersonIn) -> dict:
    try:
        p = kcam_mod.add_person(payload.model_dump(exclude_none=True))
    except kcam_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "person": p}


@router.patch("/kitchen/camera/config/people/{person_id}")
async def kitchen_person_update(person_id: str, payload: KitchenPersonIn) -> dict:
    try:
        out = kcam_mod.update_person(person_id, payload.model_dump(exclude_none=True))
    except kcam_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "person not found")
    return {"ok": True, "person": out}


@router.delete("/kitchen/camera/config/people/{person_id}")
async def kitchen_person_delete(person_id: str) -> dict:
    if not kcam_mod.delete_person(person_id):
        raise HTTPException(404, "person not found")
    return {"ok": True, "removed": True}


# ----- Actions CRUD -----

class KitchenActionIn(BaseModel):
    id:              Optional[str]  = None
    label:           Optional[str]  = None
    description:     Optional[str]  = None
    station_zone_id: Optional[str]  = None
    color:           Optional[str]  = None
    active:          Optional[bool] = None


@router.post("/kitchen/camera/config/actions")
async def kitchen_action_add(payload: KitchenActionIn) -> dict:
    try:
        a = kcam_mod.add_action(payload.model_dump(exclude_none=True))
    except kcam_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "action": a}


@router.patch("/kitchen/camera/config/actions/{action_id}")
async def kitchen_action_update(action_id: str, payload: KitchenActionIn) -> dict:
    try:
        out = kcam_mod.update_action(action_id, payload.model_dump(exclude_none=True))
    except kcam_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "action not found")
    return {"ok": True, "action": out}


@router.delete("/kitchen/camera/config/actions/{action_id}")
async def kitchen_action_delete(action_id: str) -> dict:
    if not kcam_mod.delete_action(action_id):
        raise HTTPException(404, "action not found")
    return {"ok": True, "removed": True}


# ----- Camera-zone assignment -----

class KitchenCameraAssignIn(BaseModel):
    camera:  str
    zone_id: Optional[str] = ""
    label:   Optional[str] = ""


@router.post("/kitchen/camera/config/assignment")
async def kitchen_camera_assign(payload: KitchenCameraAssignIn) -> dict:
    try:
        out = kcam_mod.set_camera_zone(
            payload.camera, payload.zone_id or "", payload.label or "",
        )
    except kcam_mod._Refused as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "assignment": out}


# ----- Analyze: snapshot → Gemini → structured "who is doing what" -----

class KitchenAnalyzeIn(BaseModel):
    camera:   str
    provider: Optional[str] = None     # "gemini" | "deepseek" (default gemini)
    model:    Optional[str] = None     # bare model name, no provider prefix
    question: Optional[str] = None     # extra free-text question, optional


def _build_analysis_prompt(people: list[dict], actions: list[dict],
                             zone_label: str, extra_q: str) -> str:
    active_people = [p for p in people if p.get("active")]
    active_actions = [a for a in actions if a.get("active")]
    p_block = "\n".join(
        f"  - {p['name']} ({p.get('role') or 'staff'}): {p.get('description') or ''}"
        for p in active_people
    ) or "  (none configured)"
    a_block = "\n".join(
        f"  - \"{a['label']}\": {a.get('description') or ''}"
        for a in active_actions
    ) or "  (none configured)"
    zone_hint = (
        f"This camera is pointed at the **{zone_label}** in a Lebanese-"
        "restaurant kitchen. "
        if zone_label else
        "This camera is in a Lebanese-restaurant kitchen. "
    )
    extra_block = (
        f"\n\nADDITIONAL OPERATOR QUESTION:\n{extra_q.strip()}"
        if extra_q and extra_q.strip() else ""
    )
    return (
        f"{zone_hint}Analyse the frame and identify every visible person + "
        "what they are doing. Match each visible person to the known staff "
        "list below by their appearance description. Match what each one is "
        "doing to the known action list below — pick the single best label "
        "per person, or use \"unknown\" if nothing fits.\n\n"
        f"KNOWN STAFF (id · description):\n{p_block}\n\n"
        f"KNOWN ACTIONS (label · description):\n{a_block}\n\n"
        "Return a JSON object with this exact shape:\n"
        "{\n"
        "  \"summary\": \"one sentence describing the overall scene\",\n"
        "  \"detections\": [\n"
        "    {\n"
        "      \"person\":        \"matched staff name, or \\\"unknown person\\\"\",\n"
        "      \"action\":        \"matched action label, or \\\"unknown\\\"\",\n"
        "      \"confidence\":    0.0,\n"
        "      \"notes\":         \"short free-text observation\"\n"
        "    }\n"
        "  ],\n"
        "  \"answer\": \"answer to the operator's extra question, if any\"\n"
        "}\n"
        "confidence is 0..1. If no person is visible return detections=[]."
        + extra_block
    )


_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary":    {"type": "STRING"},
        "detections": {
            "type":  "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "person":     {"type": "STRING"},
                    "action":     {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "notes":      {"type": "STRING"},
                },
                "required": ["person", "action", "confidence"],
            },
        },
        "answer":     {"type": "STRING"},
    },
    "required": ["summary", "detections"],
}


async def _fetch_frigate_jpeg(camera: str) -> bytes:
    """Fetch the latest snapshot from Frigate. Raises HTTPException on
    failure so the route handler can surface the cause to the SPA."""
    import httpx  # local import — matches the rest of this file
    if not state.frigate_url:
        raise HTTPException(503, "Frigate URL not configured. Set it in Settings.")
    url = f"{state.frigate_url}/api/{camera}/latest.jpg"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params={"h": 720})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Frigate snapshot failed: {e}") from e
        return r.content


@router.post("/kitchen/camera/analyze")
async def kitchen_camera_analyze(payload: KitchenAnalyzeIn) -> dict:
    if not payload.camera:
        raise HTTPException(400, "camera is required")

    provider = (payload.provider or "gemini").strip().lower()
    if provider not in ("gemini", "deepseek", "openrouter"):
        provider = "gemini"

    if provider == "gemini" and not state.gemini_api_key:
        raise HTTPException(503, "Gemini API key not configured. Set it in Settings.")
    if provider == "deepseek" and not state.deepseek_api_key:
        raise HTTPException(503, "DeepSeek API key not configured. Set it in Settings.")
    if provider == "openrouter" and not state.openrouter_api_key:
        raise HTTPException(503, "OpenRouter API key not configured. Set it in Settings.")

    cfg = kcam_mod.load_config()
    zones = kzones_mod.load_layout()["zones"]
    zone_label_by_id = {z["id"]: z["name_en"] for z in zones}

    # Look up which zone (if any) this camera points at.
    cam_assign = next((c for c in cfg["camera_assignments"]
                        if c.get("camera") == payload.camera), None)
    zone_label = ""
    if cam_assign:
        z = zone_label_by_id.get(cam_assign.get("zone_id") or "", "")
        zone_label = cam_assign.get("label") or z

    # Fetch snapshot from Frigate.
    img_bytes = await _fetch_frigate_jpeg(payload.camera)

    # Build the analysis prompt.
    prompt = _build_analysis_prompt(
        cfg["people"], cfg["actions"], zone_label,
        payload.question or "",
    )

    out = await kvision.analyze(
        image_bytes=img_bytes,
        system_prompt=(
            "You are a kitchen monitoring AI. Read the camera frame and "
            "return ONLY the JSON object the user prompt describes."
        ),
        user_prompt=prompt,
        provider=provider,
        model=payload.model,
        # Gemini-only — DeepSeek's OpenAI-compatible API doesn't accept
        # a response_schema, so we just rely on response_format=json_object.
        response_schema=_ANALYSIS_SCHEMA if provider == "gemini" else None,
        expect_json=True,
        temperature=0.2,
    )
    if not out.get("ok"):
        raise HTTPException(502, f"{provider} call failed: {out.get('error')}")

    text = (out.get("text") or "").strip()
    try:
        parsed = json.loads(text) if text else {}
    except Exception:
        parsed = {"summary": text, "detections": [], "answer": ""}

    return {
        "ok":         True,
        "camera":     payload.camera,
        "zone_label": zone_label,
        "provider":   provider,
        "model":      out.get("model"),
        "prompt":     prompt,
        "result":     parsed,
        "latency_ms": out.get("latency_ms"),
    }


# ----- Vision models — Gemini + DeepSeek, unified shape -----

@router.get("/kitchen/vision/models")
async def kitchen_vision_models() -> dict:
    """Lists every vision-capable model the operator has access to,
    grouped by provider. The SPA flattens this into a single dropdown
    with provider chips."""
    return {"ok": True, "providers": await kvision.list_vision_models()}


# ============================================================================
# AI-Center — free-form Playground over a camera snapshot OR an uploaded
# image. Every ask is persisted (with thumbnail) so the operator can flip
# to the Playground History page and review past Q+A.
# ============================================================================

@router.post("/ai-center/ask")
async def ai_center_ask(
    question:        str = Form(...),
    provider:        Optional[str] = Form(None),
    model:           Optional[str] = Form(None),
    camera:          Optional[str] = Form(None),
    loop:            Optional[bool] = Form(False),
    loop_interval_s: Optional[int] = Form(None),
    file:            Optional[UploadFile] = File(None),
) -> dict:
    """Multipart endpoint. Either `camera` (snapshot via Frigate) or
    `file` (uploaded image) must be set — never both."""
    q = (question or "").strip()
    if not q:
        raise HTTPException(400, "question is required")
    if not camera and not file:
        raise HTTPException(400, "either camera or file must be provided")
    if camera and file:
        raise HTTPException(400, "pick a camera OR upload an image — not both")

    prov = (provider or "gemini").strip().lower()
    if prov not in ("gemini", "deepseek", "openrouter"):
        prov = "gemini"

    if prov == "gemini" and not state.gemini_api_key:
        raise HTTPException(503, "Gemini API key not configured. Set it in Settings.")
    if prov == "deepseek" and not state.deepseek_api_key:
        raise HTTPException(503, "DeepSeek API key not configured. Set it in Settings.")
    if prov == "openrouter" and not state.openrouter_api_key:
        raise HTTPException(503, "OpenRouter API key not configured. Set it in Settings.")

    # Resolve image bytes.
    source = ""
    img_bytes: bytes = b""
    if camera:
        source = "camera"
        img_bytes = await _fetch_frigate_jpeg(camera)
    else:
        source = "upload"
        try:
            img_bytes = await file.read()  # type: ignore[union-attr]
        except Exception as e:
            raise HTTPException(400, f"failed to read upload: {e}") from e
        if not img_bytes:
            raise HTTPException(400, "uploaded file is empty")

    # Free-form vision call — no JSON schema, the operator is asking a
    # plain-language question and wants a plain-language answer back.
    out = await kvision.analyze(
        image_bytes=img_bytes,
        system_prompt=(
            "You are a helpful vision assistant. Look at the image and "
            "answer the operator's question clearly and concisely. If the "
            "question can't be answered from the image, say so."
        ),
        user_prompt=q,
        provider=prov,
        model=model,
        response_schema=None,
        expect_json=False,
        temperature=0.3,
    )

    response_text = (out.get("text") or "").strip()
    error_text    = None if out.get("ok") else (out.get("error") or "unknown error")

    row = ai_center_mod.append_entry(
        image_bytes=img_bytes,
        question=q,
        response_text=response_text,
        provider=prov,
        model=out.get("model") or (model or ""),
        source=source,
        camera=camera,
        latency_ms=int(out.get("latency_ms") or 0),
        error=error_text,
        loop=bool(loop),
        loop_interval_s=loop_interval_s,
    )
    return {"ok": out.get("ok", False), "entry": row}


@router.get("/ai-center/history")
async def ai_center_history(limit: int = 100, offset: int = 0) -> dict:
    return ai_center_mod.list_entries(limit=limit, offset=offset)


@router.get("/ai-center/history/{entry_id}/image")
async def ai_center_history_image(entry_id: str) -> FileResponse:
    entry = ai_center_mod.get_entry(entry_id)
    if not entry:
        raise HTTPException(404, "entry not found")
    p = ai_center_mod.image_path(entry_id)
    if not p.exists():
        raise HTTPException(404, "image not found on disk")
    return FileResponse(p, media_type="image/jpeg")


@router.delete("/ai-center/history/{entry_id}")
async def ai_center_history_delete(entry_id: str) -> dict:
    removed = ai_center_mod.delete_entry(entry_id)
    if not removed:
        raise HTTPException(404, "entry not found")
    return {"ok": True}


@router.delete("/ai-center/history")
async def ai_center_history_clear() -> dict:
    ai_center_mod.clear_all()
    return {"ok": True}


# ============================================================================
# Kitchen — AI-History (background person-detection capture).
#
# The poller in kitchen_ai_history_task watches a configurable set of
# Frigate cameras, downloads (full-frame snapshot + person crop) for
# every person event, and appends a row to the ledger. These endpoints
# let the SPA configure the camera list, browse the ledger, and serve
# the stored JPEGs.
# ============================================================================

@router.get("/kitchen/ai-history")
async def kitchen_ai_history_get(
    limit: int = 50, offset: int = 0, camera: Optional[str] = None,
) -> dict:
    # Make sure the background task is alive — idempotent, so calling
    # this on every page load is fine.
    khist_task.ensure_running()
    page = khist_mod.list_events(limit=limit, offset=offset, camera=camera or None)
    return {
        "ok":               True,
        "selected_cameras": khist_mod.get_selected_cameras(),
        "scan_interval_s":  khist_mod.get_scan_interval(),
        "scan_interval_min": khist_mod.SCAN_INTERVAL_MIN,
        "scan_interval_max": khist_mod.SCAN_INTERVAL_MAX,
        "stats":            khist_mod.stats(),
        "poller_running":   khist_task.is_running(),
        "frigate_configured": bool(state.frigate_url),
        **page,
    }


class AiHistoryCamerasIn(BaseModel):
    cameras: list[str]


@router.post("/kitchen/ai-history/cameras")
async def kitchen_ai_history_cameras_set(payload: AiHistoryCamerasIn) -> dict:
    cams = khist_mod.set_selected_cameras(payload.cameras or [])
    khist_task.ensure_running()
    return {"ok": True, "selected_cameras": cams}


class AiHistoryIntervalIn(BaseModel):
    seconds: int


@router.post("/kitchen/ai-history/scan-interval")
async def kitchen_ai_history_interval_set(payload: AiHistoryIntervalIn) -> dict:
    v = khist_mod.set_scan_interval(payload.seconds)
    return {"ok": True, "scan_interval_s": v}


@router.get("/kitchen/ai-history/image/{event_id}")
async def kitchen_ai_history_image(event_id: str, kind: str = "full") -> FileResponse:
    # Accept full, crop (legacy alias for p0), or p0..p99 (one per
    # detected person in that scan). image_path() normalises further;
    # we just gate the input here so unknown kinds 400 instead of
    # silently serving the full frame.
    valid = (
        kind in ("full", "crop")
        or (kind.startswith("p") and kind[1:].isdigit()
            and 0 <= int(kind[1:]) <= 99)
    )
    if not valid:
        raise HTTPException(400, "kind must be 'full' or 'p0'..'p99'")
    path = khist_mod.image_path(event_id, kind)
    if not path.exists():
        raise HTTPException(404, "image not found")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/kitchen/ai-history/{event_id}")
async def kitchen_ai_history_delete(event_id: str) -> dict:
    if not khist_mod.delete_event(event_id):
        raise HTTPException(404, "event not found")
    return {"ok": True, "removed": True}


@router.post("/kitchen/ai-history/clear")
async def kitchen_ai_history_clear() -> dict:
    khist_mod.clear_all()
    return {"ok": True, "cleared": True}


# ============================================================================
# Kitchen — AI-Rules
# ============================================================================

@router.get("/kitchen/ai-rules")
async def kitchen_ai_rules_list() -> dict:
    # Idempotent — keep the engine alive whenever the SPA pings us.
    krules_engine.ensure_running()
    rules = krules_mod.list_rules()
    rules_with_stats = [
        {**r, "stats": krules_mod.stats_for_rule(r["id"])}
        for r in rules
    ]
    return {
        "ok":             True,
        "rules":          rules_with_stats,
        "trigger_modes":  krules_mod.TRIGGER_MODES,
        "engine_running": krules_engine.is_running(),
        "frigate_configured": bool(state.frigate_url),
        "gemini_configured":  bool(state.gemini_api_key),
        "ha_configured":      bool(state.homeassistant_url and state.homeassistant_token),
    }


class RuleIn(BaseModel):
    id:              Optional[str]  = None
    name:            Optional[str]  = None
    camera:          Optional[str]  = None
    prompt:          Optional[str]  = None
    provider:        Optional[str]  = None   # "gemini" | "deepseek" | "openrouter"
    model:           Optional[str]  = None
    trigger_mode:    Optional[str]  = None
    scan_interval_s: Optional[int]  = None
    ha_script:       Optional[str]  = None
    # Legacy field — only accepted on POST/PATCH so old payloads keep
    # working. The coercer in kitchen_rules.py migrates it to `ha_script`.
    ha_event_name:   Optional[str]  = None
    active:          Optional[bool] = None


@router.post("/kitchen/ai-rules")
async def kitchen_ai_rules_add(payload: RuleIn) -> dict:
    try:
        rule = krules_mod.add_rule(payload.model_dump(exclude_none=True))
    except krules_mod._Refused as e:
        raise HTTPException(400, str(e))
    krules_engine.ensure_running()
    return {"ok": True, "rule": rule}


@router.patch("/kitchen/ai-rules/{rule_id}")
async def kitchen_ai_rules_update(rule_id: str, payload: RuleIn) -> dict:
    try:
        out = krules_mod.update_rule(rule_id, payload.model_dump(exclude_none=True))
    except krules_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "rule not found")
    return {"ok": True, "rule": out}


@router.delete("/kitchen/ai-rules/{rule_id}")
async def kitchen_ai_rules_delete(rule_id: str) -> dict:
    if not krules_mod.delete_rule(rule_id):
        raise HTTPException(404, "rule not found")
    return {"ok": True, "removed": True}


@router.get("/kitchen/ai-rules/{rule_id}/iterations")
async def kitchen_ai_rule_iterations(
    rule_id: str, limit: int = 30, offset: int = 0,
    only_triggers: bool = False,
    from_ts: Optional[float] = None,
    to_ts:   Optional[float] = None,
) -> dict:
    page = krules_mod.list_iterations(
        rule_id=rule_id, limit=limit, offset=offset,
        only_triggers=only_triggers,
        from_ts=from_ts, to_ts=to_ts,
    )
    return {"ok": True, **page,
            "stats": krules_mod.stats_for_rule(rule_id)}


class IterationScoreIn(BaseModel):
    score: Optional[str] = None   # "correct" | "incorrect" | null


@router.post("/kitchen/ai-rules/iterations/{iter_id}/score")
async def kitchen_ai_rule_iteration_score(
    iter_id: str, payload: IterationScoreIn,
) -> dict:
    try:
        out = krules_mod.score_iteration(iter_id, payload.score)
    except krules_mod._Refused as e:
        raise HTTPException(400, str(e))
    if out is None:
        raise HTTPException(404, "iteration not found")
    return {"ok": True, "iteration": out}


@router.delete("/kitchen/ai-rules/iterations/{iter_id}")
async def kitchen_ai_rule_iteration_delete(iter_id: str) -> dict:
    if not krules_mod.delete_iteration(iter_id):
        raise HTTPException(404, "iteration not found")
    return {"ok": True, "removed": True}


@router.websocket("/kitchen/ai-rules/ws")
async def kitchen_ai_rules_ws(websocket: WebSocket) -> None:
    """Pushes `iteration` envelopes to the AI-Rules SPA whenever the
    engine runs a rule. Shape:

        {type: "iteration", rule_id, camera, iteration_id, ts,
         verdict, confidence, trigger_reason, provider, model,
         ha_fired, ha_error, error}

    The SPA uses these to blink rule rows red on fire (and refresh the
    iteration list if the matching rule's drawer is open)."""
    await websocket.accept()
    q = krules_engine.add_subscriber()
    try:
        # Snapshot envelope first so the page knows the WS is healthy.
        await websocket.send_json({"type": "snapshot", "ok": True})
        while True:
            msg = await q.get()
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        krules_engine.remove_subscriber(q)
        try: await websocket.close()
        except Exception: pass


class HaTestScriptIn(BaseModel):
    script: str


@router.post("/kitchen/ai-rules/ha/test-script")
async def kitchen_ai_rules_ha_test_script(payload: HaTestScriptIn) -> dict:
    """Fire a HA script on demand so the operator can verify the rule's
    target works before letting the engine fire it. Delegates to
    `krules_engine.call_ha_script` — the SAME function the engine uses
    when a rule's verdict goes positive, so any "test works but engine
    doesn't" issue is impossible by construction."""
    if not state.homeassistant_url or not state.homeassistant_token:
        raise HTTPException(503, "Home Assistant URL or token not configured. Set them in Settings.")
    script = (payload.script or "").strip()
    if not script:
        raise HTTPException(400, "script is required")
    variables = {
        "source":      "ai-rules-test",
        "rule_id":     "test",
        "rule_name":   "AI-Rules test fire",
        "camera":      "test",
        "verdict":     True,
        "confidence":  1.0,
        "explanation": "Manual test fire from the AI-Rules form.",
        "fired_at":    int(time.time()),
    }
    ok, err = await krules_engine.call_ha_script(script, variables, source="test")
    if not ok:
        raise HTTPException(502, err or "HA call failed")
    cleaned = script[len("script."):] if script.startswith("script.") else script
    return {"ok": True, "script": cleaned, "status": 200}


@router.get("/kitchen/ai-rules/image/{iter_id}")
async def kitchen_ai_rule_image(iter_id: str) -> FileResponse:
    path = krules_mod.image_path(iter_id)
    if not path.exists():
        raise HTTPException(404, "image not found")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
    in data/demos/restaurant/calls."""
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
# The agent's function tools read from data/demos/restaurant/snapshot.json. The
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
    q = restaurant_live_agent_service.subscribe()
    try:
        # Send the current status so the UI hydrates immediately.
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "status": restaurant_live_agent_service.status(),
            "calls":  restaurant_live_agent_service.active_calls(),
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
        restaurant_live_agent_service.unsubscribe(q)
        try: await websocket.close()
        except Exception: pass
