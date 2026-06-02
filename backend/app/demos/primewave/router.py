"""Primewave Main demo — login / me / logout + Live Agent control plane
(config / persona / KB / status). Same shape as clinic's API surface
but with the slim agent backing it."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...core.state import state
from . import config
from .live_agent import (
    primewave_live_agent_service,
    load_persona, load_kb, save_persona, save_kb,
    list_saved_calls, load_saved_call, call_audio_path, delete_saved_call,
    list_agent_tools,
    CUSTOMER_FIELDS, CUSTOMER_FIELD_LABELS,
)
from . import ai_center as ai_center_mod
# Reuse the restaurant module's vision adapter — same Gemini / DeepSeek /
# OpenRouter glue, no need to fork it for primewave. Imported lazily-ish
# so primewave can boot without restaurant if that ever becomes a refactor.
from ..restaurant import kitchen_vision as kvision
from . import whatsapp_inbox
from . import whatsapp_bot
from . import contacts as contacts_mod
from .wasender import build_client as build_wasender_client, normalize_phone
from .ami import AMIService, AMICredentials
import asyncio as _asyncio
from ..auth import clear_session, issue_session, require_session

logger = logging.getLogger("demo_primewave")
router  = APIRouter()
_SLUG   = config.SLUG


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
# Integrations — exposes the *shared* state.frigate_url here so the demo
# can configure it from inside its own auth context (without crossing
# into the master-Basic-Auth-protected /api/frigate/* surface).
# ============================================================================

class IntegrationsOut(BaseModel):
    frigate_url: str


class IntegrationsIn(BaseModel):
    frigate_url: Optional[str] = None


@router.get("/integrations")
async def integrations_get(request: Request) -> IntegrationsOut:
    require_session(request, _SLUG)
    return IntegrationsOut(frigate_url=state.frigate_url or "")


@router.post("/integrations")
async def integrations_set(body: IntegrationsIn, request: Request) -> IntegrationsOut:
    require_session(request, _SLUG)
    if body.frigate_url is not None:
        v = (body.frigate_url or "").strip().rstrip("/")
        # Stored as None when blanked out — same convention as restaurant.
        state.frigate_url = v or None
        state.save()
    return IntegrationsOut(frigate_url=state.frigate_url or "")


# ============================================================================
# Live Agent — config / persona / kb / status
# ============================================================================

class AgentConfigOut(BaseModel):
    enabled:              bool
    bind_host:            str
    bind_port:            int
    voice:                str
    greeting:             str
    max_call_s:           int
    interruption_enabled: bool


class AgentConfigIn(BaseModel):
    enabled:              Optional[bool] = None
    bind_host:            Optional[str] = None
    bind_port:            Optional[int] = None
    voice:                Optional[str] = None
    greeting:             Optional[str] = None
    max_call_s:           Optional[int] = None
    interruption_enabled: Optional[bool] = None


def _current() -> AgentConfigOut:
    return AgentConfigOut(
        enabled              = bool(state.pwa_enabled),
        bind_host            = state.pwa_bind_host or "0.0.0.0",
        bind_port            = int(state.pwa_bind_port or 8094),
        voice                = state.pwa_voice or "Aoede",
        greeting             = state.pwa_greeting or "",
        max_call_s           = int(state.pwa_max_call_s or 900),
        interruption_enabled = bool(state.pwa_interruption_enabled),
    )


@router.get("/agent/config")
async def agent_get_config(request: Request) -> AgentConfigOut:
    require_session(request, _SLUG)
    return _current()


@router.post("/agent/config")
async def agent_set_config(body: AgentConfigIn, request: Request) -> AgentConfigOut:
    require_session(request, _SLUG)
    if body.enabled is not None:
        state.pwa_enabled = bool(body.enabled)
    if body.bind_host is not None:
        state.pwa_bind_host = body.bind_host.strip() or "0.0.0.0"
    if body.bind_port is not None:
        try:
            port = int(body.bind_port)
        except (TypeError, ValueError):
            raise HTTPException(400, "bind_port must be an int")
        if not (1024 <= port <= 65535):
            raise HTTPException(400, "bind_port must be 1024-65535")
        state.pwa_bind_port = port
    if body.voice is not None:
        state.pwa_voice = body.voice.strip() or "Aoede"
    if body.greeting is not None:
        state.pwa_greeting = body.greeting
    if body.max_call_s is not None:
        try:
            sec = int(body.max_call_s)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_call_s must be an int")
        if sec < 60:
            raise HTTPException(400, "max_call_s must be >= 60")
        state.pwa_max_call_s = sec
    if body.interruption_enabled is not None:
        state.pwa_interruption_enabled = bool(body.interruption_enabled)
    state.save()
    # Apply immediately — start / stop / rebind listener as needed.
    try:
        primewave_live_agent_service.apply_config()
    except Exception:
        logger.exception("primewave agent apply_config failed")
    return _current()


class CallInitIn(BaseModel):
    uuid:         str
    caller_phone: Optional[str] = ""


# ----- Supervisor escalation (AMI + ChanSpy) -------------------------------

def _load_ami_credentials() -> AMICredentials:
    """Pull AMI creds from state — same lookup pattern as restaurant's
    escalation.json but using state.json since primewave doesn't carry
    a per-demo escalation.json file."""
    return AMICredentials(
        host=(state.pwa_ami_host or "").strip(),
        port=int(state.pwa_ami_port or 5038),
        username=(state.pwa_ami_username or "").strip(),
        secret=(state.pwa_ami_secret or "").strip(),
    )


_ami_service = AMIService(_load_ami_credentials)


class EscalationConfigOut(BaseModel):
    supervisor_extension: str
    supervisor_caller_id: str
    ami_host:             str
    ami_port:             int
    ami_username:         str
    ami_secret_set:       bool
    ami_secret_masked:    str


class EscalationConfigIn(BaseModel):
    supervisor_extension: Optional[str] = None
    supervisor_caller_id: Optional[str] = None
    ami_host:             Optional[str] = None
    ami_port:             Optional[int] = None
    ami_username:         Optional[str] = None
    ami_secret:           Optional[str] = None


def _escalation_payload() -> EscalationConfigOut:
    return EscalationConfigOut(
        supervisor_extension = state.pwa_supervisor_extension or "",
        supervisor_caller_id = state.pwa_supervisor_caller_id or "Supervisor",
        ami_host             = state.pwa_ami_host or "",
        ami_port             = int(state.pwa_ami_port or 5038),
        ami_username         = state.pwa_ami_username or "",
        ami_secret_set       = bool(state.pwa_ami_secret),
        ami_secret_masked    = _mask(state.pwa_ami_secret),
    )


@router.get("/agent/escalation")
async def agent_get_escalation(request: Request) -> EscalationConfigOut:
    require_session(request, _SLUG)
    return _escalation_payload()


@router.post("/agent/escalation")
async def agent_set_escalation(body: EscalationConfigIn, request: Request) -> EscalationConfigOut:
    require_session(request, _SLUG)
    if body.supervisor_extension is not None:
        state.pwa_supervisor_extension = body.supervisor_extension.strip()
    if body.supervisor_caller_id is not None:
        state.pwa_supervisor_caller_id = body.supervisor_caller_id.strip() or "Supervisor"
    if body.ami_host is not None:
        state.pwa_ami_host = body.ami_host.strip()
    if body.ami_port is not None:
        try:
            p = int(body.ami_port)
        except (TypeError, ValueError):
            raise HTTPException(400, "ami_port must be an int")
        state.pwa_ami_port = p
    if body.ami_username is not None:
        state.pwa_ami_username = body.ami_username.strip()
    # Secret only updates when the operator typed something; blank means
    # "keep existing" — same convention as the rest of our credential UIs.
    if body.ami_secret:
        state.pwa_ami_secret = body.ami_secret.strip()
    state.save()
    return _escalation_payload()


@router.post("/agent/calls/{call_id}/engage_supervisor")
async def agent_engage_supervisor(call_id: str, request: Request,
                                  mode: str = "barge") -> dict:
    """Ring the configured supervisor extension and drop them into the
    live call via ChanSpy. `mode` = listen | whisper | barge (default).

    Returns 200 with ok=False on AMI failure so the SPA can show the
    error inline rather than as a toast."""
    require_session(request, _SLUG)
    call = primewave_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    extension = (state.pwa_supervisor_extension or "").strip()
    return await _ami_service.dial_supervisor(call_id, extension, spy_mode=mode)


@router.post("/agent/calls/{call_id}/ack_flag")
async def agent_ack_flag(call_id: str, request: Request) -> dict:
    """Clear the supervisor flag on a live call (operator click on the
    Dashboard's 'Acknowledge' button)."""
    require_session(request, _SLUG)
    call = primewave_live_agent_service.get_call(call_id)
    if call is None:
        raise HTTPException(404, "call not found or already ended")
    call.ack_flag()
    return {"ok": True}


@router.post("/agent/call-init")
async def agent_call_init(body: CallInitIn) -> dict:
    """PBX dialplan hook — called over HTTP from extensions_custom.conf
    right before the AudioSocket leg connects, so the Live Agent knows
    WHO is calling. No auth required (the LAN PBX is the only thing
    that should hit this; the path is master-Basic-Auth-exempt under
    /api/demo/primewave/*)."""
    primewave_live_agent_service.record_call_init(
        body.uuid or "", body.caller_phone or "",
    )
    return {"ok": True}


@router.get("/agent/status")
async def agent_status(request: Request) -> dict:
    require_session(request, _SLUG)
    return primewave_live_agent_service.status()


# ----- Persona + KB ---------------------------------------------------------

class TextBlob(BaseModel):
    text: str


@router.get("/agent/persona")
async def agent_get_persona(request: Request) -> TextBlob:
    require_session(request, _SLUG)
    return TextBlob(text=load_persona())


@router.post("/agent/persona")
async def agent_save_persona(body: TextBlob, request: Request) -> TextBlob:
    require_session(request, _SLUG)
    save_persona(body.text)
    return TextBlob(text=load_persona())


@router.get("/agent/kb")
async def agent_get_kb(request: Request) -> TextBlob:
    require_session(request, _SLUG)
    return TextBlob(text=load_kb())


@router.post("/agent/kb")
async def agent_save_kb(body: TextBlob, request: Request) -> TextBlob:
    require_session(request, _SLUG)
    save_kb(body.text)
    return TextBlob(text=load_kb())


@router.get("/agent/tools")
async def agent_tools(request: Request) -> dict:
    """Introspected snapshot of every tool the Live Agent can call —
    used by the Configuration page's Tools section. Pulled live from
    the agent's tool definitions so adding a tool in live_agent.py
    automatically shows up here."""
    require_session(request, _SLUG)
    return {"tools": list_agent_tools()}


# ----- Collected customer data ---------------------------------------------
# In-progress live data already rides on /agent/status (each entry in
# `calls[]` includes `customer_data`). Ended-call records are persisted
# to data/demos/primewave/calls/<id>.json and listed here.

@router.get("/agent/customer-schema")
async def agent_customer_schema(request: Request) -> dict:
    """Field order + labels the SPA renders as table columns."""
    require_session(request, _SLUG)
    return {"fields": CUSTOMER_FIELDS, "labels": CUSTOMER_FIELD_LABELS}


@router.get("/agent/calls")
async def agent_list_calls(request: Request, limit: int = 100) -> dict:
    """Unified intake feed — voice calls (calls/<dir>) and WhatsApp bot
    conversations (whatsapp_bot_state.json) merged into one list,
    sorted by recency. Each row carries a `source` field ("call" or
    "whatsapp") so the SPA can render a channel tag."""
    require_session(request, _SLUG)
    calls = list_saved_calls(limit=limit)
    for c in calls:
        c.setdefault("source", "call")

    # WhatsApp bot conversations — shape into the same row schema.
    wa_rows: list[dict] = []
    for conv in whatsapp_bot.list_conversations():
        phone = conv.get("phone") or ""
        wa_rows.append({
            "id":            "wa-" + phone,
            "source":        "whatsapp",
            "started_at":    conv.get("last_seen"),  # newest msg time
            "ended_at":      conv.get("last_seen"),
            "duration_s":    0,
            "peer":          "WhatsApp · +" + phone,
            "call_id":       None,
            "uuid":          None,
            "customer_data": conv.get("customer_data") or {},
            "turn_count":    conv.get("turn_count") or 0,
            "has_caller_wav": False,
            "has_agent_wav":  False,
            "has_mixed_wav":  False,
            "pushName":      conv.get("pushName") or "",
        })

    merged = (calls + wa_rows)
    merged.sort(key=lambda r: (r.get("started_at") or 0), reverse=True)
    return {"calls": merged[: max(0, int(limit))]}


@router.delete("/agent/calls/{call_id}")
async def agent_delete_call(call_id: str, request: Request) -> dict:
    """Delete an intake record. `wa-<phone>` ids route to the WhatsApp
    bot conversation store; everything else is a call dir."""
    require_session(request, _SLUG)
    if call_id.startswith("wa-"):
        phone = call_id[3:]
        ok = whatsapp_bot.clear_conversation(phone)
    else:
        ok = delete_saved_call(call_id)
    if not ok:
        raise HTTPException(404, "intake not found")
    return {"ok": True}


@router.get("/agent/calls/{call_id}")
async def agent_call_detail(call_id: str, request: Request) -> dict:
    """Full meta.json — includes the transcript `turns` list."""
    require_session(request, _SLUG)
    rec = load_saved_call(call_id)
    if not rec:
        raise HTTPException(404, "call not found")
    return rec


@router.get("/agent/calls/{call_id}/audio/{side}")
async def agent_call_audio(call_id: str, side: str, request: Request) -> FileResponse:
    """WAV download for one side: caller | agent | mixed."""
    require_session(request, _SLUG)
    p = call_audio_path(call_id, side)
    if not p:
        raise HTTPException(404, f"{side} audio not found")
    return FileResponse(p, media_type="audio/wav",
                        filename=f"{call_id}-{side}.wav")


# ============================================================================
# AI Center — Playground + history.
#
# All endpoints are exempt from master Basic Auth (they sit under
# /api/demo/primewave/*) and are gated by the demo's own login. Frigate
# camera list + snapshot fetches are proxied here so the SPA never has
# to cross over to the master-auth-protected /api/frigate/* surface.
# ============================================================================


async def _fetch_frigate_jpeg(camera: str) -> bytes:
    """Snapshot from Frigate. Raises HTTPException so the route can
    surface the cause to the SPA."""
    import httpx
    if not state.frigate_url:
        raise HTTPException(503,
            "Frigate URL not configured. Set it in Call Center → Settings.")
    url = f"{state.frigate_url}/api/{camera}/latest.jpg"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(url, params={"h": 720})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Frigate snapshot failed: {e}") from e
        return r.content


@router.get("/ai-center/camera/{camera}/snapshot")
async def ai_center_camera_snapshot(camera: str, request: Request):
    """Proxy a single Frigate snapshot JPEG so the SPA can render a
    live preview without crossing into the master-Basic-Auth /api/frigate
    surface. SPA refreshes this every ~1.5s for the preview feed."""
    require_session(request, _SLUG)
    # Defence in depth — camera names from Frigate are alphanumeric +
    # underscores/dashes. Reject anything else before we paste into the URL.
    if not camera or not all(c.isalnum() or c in "-_" for c in camera):
        raise HTTPException(400, "invalid camera name")
    img = await _fetch_frigate_jpeg(camera)
    from fastapi.responses import Response as _R
    return _R(
        content=img,
        media_type="image/jpeg",
        # Tell the browser NOT to cache — we want every refresh to fetch fresh.
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/ai-center/cameras")
async def ai_center_cameras(request: Request) -> dict:
    """List cameras from the configured Frigate instance.
    Surfaced here so the SPA stays within /api/demo/primewave/*."""
    require_session(request, _SLUG)
    if not state.frigate_url:
        return {"ok": False, "cameras": [],
                "detail": "Frigate URL not configured. Set it in Call Center → Settings."}
    import httpx
    url = f"{state.frigate_url}/api/config"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url)
            r.raise_for_status()
            cfg = r.json() or {}
    except Exception as e:
        raise HTTPException(502, f"Frigate /api/config failed: {e}") from e
    cams = list((cfg.get("cameras") or {}).keys())
    return {"ok": True, "cameras": sorted(cams)}


@router.get("/ai-center/vision-models")
async def ai_center_vision_models(request: Request) -> dict:
    """Same data as restaurant's /kitchen/vision-models — providers +
    models the operator has access to."""
    require_session(request, _SLUG)
    return {"ok": True, "providers": await kvision.list_vision_models()}


@router.post("/ai-center/ask")
async def ai_center_ask(
    request:         Request,
    question:        str = Form(...),
    provider:        Optional[str] = Form(None),
    model:           Optional[str] = Form(None),
    camera:          Optional[str] = Form(None),
    loop:            Optional[bool] = Form(False),
    loop_interval_s: Optional[int] = Form(None),
    file:            Optional[UploadFile] = File(None),
) -> dict:
    """Multipart endpoint — caller supplies either `camera` (snapshot
    via Frigate) or `file` (uploaded image), plus a free-form
    question. Returns the model's answer and the persisted entry."""
    require_session(request, _SLUG)

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
        raise HTTPException(503, "Gemini API key not configured.")
    if prov == "deepseek" and not state.deepseek_api_key:
        raise HTTPException(503, "DeepSeek API key not configured.")
    if prov == "openrouter" and not state.openrouter_api_key:
        raise HTTPException(503, "OpenRouter API key not configured.")

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

    out = await kvision.analyze(
        image_bytes=img_bytes,
        system_prompt=(
            "You are a helpful vision assistant. Look at the image and "
            "answer the operator's question clearly and concisely. If "
            "the question can't be answered from the image, say so."
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
async def ai_center_history(request: Request, limit: int = 100, offset: int = 0) -> dict:
    require_session(request, _SLUG)
    return ai_center_mod.list_entries(limit=limit, offset=offset)


@router.get("/ai-center/history/{entry_id}/image")
async def ai_center_history_image(entry_id: str, request: Request) -> FileResponse:
    require_session(request, _SLUG)
    entry = ai_center_mod.get_entry(entry_id)
    if not entry:
        raise HTTPException(404, "entry not found")
    p = ai_center_mod.image_path(entry_id)
    if not p.exists():
        raise HTTPException(404, "image not found on disk")
    return FileResponse(p, media_type="image/jpeg")


@router.delete("/ai-center/history/{entry_id}")
async def ai_center_history_delete(entry_id: str, request: Request) -> dict:
    require_session(request, _SLUG)
    removed = ai_center_mod.delete_entry(entry_id)
    if not removed:
        raise HTTPException(404, "entry not found")
    return {"ok": True}


@router.delete("/ai-center/history")
async def ai_center_history_clear(request: Request) -> dict:
    require_session(request, _SLUG)
    ai_center_mod.clear_all()
    return {"ok": True}


# ============================================================================
# WhatsApp — WaSender send + webhook receive + local inbox
# ============================================================================
# Single source of truth is `whatsapp_inbox.json`. When the operator sends
# a message we append to the inbox locally (so the chat view updates
# instantly even before WaSender confirms delivery). When WaSender posts
# to /webhook we also append. The SPA groups by phone number client-side.

class WhatsAppConfigOut(BaseModel):
    api_key_set:        bool
    api_key_masked:     str
    personal_token_set: bool
    personal_token_masked: str
    session_id:         str


class WhatsAppConfigIn(BaseModel):
    api_key:        Optional[str] = None
    personal_token: Optional[str] = None
    session_id:     Optional[str] = None


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "…" + s[-4:]


def _wa_config_payload() -> WhatsAppConfigOut:
    return WhatsAppConfigOut(
        api_key_set        = bool(state.pwa_wasender_api_key),
        api_key_masked     = _mask(state.pwa_wasender_api_key),
        personal_token_set = bool(state.pwa_wasender_personal_token),
        personal_token_masked = _mask(state.pwa_wasender_personal_token),
        session_id         = state.pwa_wasender_session_id or "",
    )


@router.get("/whatsapp/config")
async def whatsapp_config_get(request: Request) -> WhatsAppConfigOut:
    require_session(request, _SLUG)
    return _wa_config_payload()


@router.post("/whatsapp/config")
async def whatsapp_config_set(body: WhatsAppConfigIn, request: Request) -> WhatsAppConfigOut:
    require_session(request, _SLUG)
    if body.api_key is not None:
        v = body.api_key.strip()
        if v:
            state.pwa_wasender_api_key = v
    if body.personal_token is not None:
        v = body.personal_token.strip()
        if v:
            state.pwa_wasender_personal_token = v
    if body.session_id is not None:
        state.pwa_wasender_session_id = body.session_id.strip()
    state.save()
    return _wa_config_payload()


@router.delete("/whatsapp/config")
async def whatsapp_config_clear(request: Request) -> WhatsAppConfigOut:
    """Wipe stored credentials (for rotating)."""
    require_session(request, _SLUG)
    state.pwa_wasender_api_key = ""
    state.pwa_wasender_personal_token = ""
    state.pwa_wasender_session_id = ""
    state.save()
    return _wa_config_payload()


@router.get("/whatsapp/status")
async def whatsapp_status(request: Request) -> dict:
    """Ping WaSender to verify the API key is good + return inbox stats.
    Surfaces the webhook URL the operator should paste into WaSender's
    dashboard so incoming messages reach this VM."""
    require_session(request, _SLUG)
    api_key = state.pwa_wasender_api_key or ""
    personal = state.pwa_wasender_personal_token or ""
    if not api_key:
        return {
            "ok":            False,
            "error":         "WaSender API key not configured.",
            "inbox":         whatsapp_inbox.inbox_stats(),
            "webhook_url":   str(request.url_for("primewave_whatsapp_webhook")),
        }
    client = build_wasender_client(api_key, personal)
    ping = await client.ping()
    return {
        "ok":           bool(ping.get("ok")),
        "ping":         ping,
        "inbox":        whatsapp_inbox.inbox_stats(),
        "webhook_url":  str(request.url_for("primewave_whatsapp_webhook")),
        "session_id":   state.pwa_wasender_session_id or "",
    }


# Public webhook — NO session auth (WaSender doesn't carry a cookie).
# Master-Basic-Auth-exempt by virtue of being under /api/demo/primewave/*.
@router.post("/whatsapp/webhook", name="primewave_whatsapp_webhook")
async def whatsapp_webhook(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    added = whatsapp_inbox.store_webhook(payload)
    logger.info("primewave whatsapp webhook: stored %d new row(s)", added)

    # Fan freshly-arrived message rows into the auto-responder bot.
    # No-op if the toggle is off — handle_inbound() short-circuits.
    if whatsapp_bot.is_enabled():
        rows = whatsapp_inbox._candidate_rows(payload)
        for row in rows:
            _asyncio.create_task(whatsapp_bot.handle_inbound(row))
    return {"ok": True, "stored": added,
            "bot_dispatched": len(whatsapp_inbox._candidate_rows(payload))
                              if whatsapp_bot.is_enabled() else 0}


# ----- Bot status / toggle / conversations --------------------------------

class WhatsAppBotToggleIn(BaseModel):
    enabled: bool


@router.get("/whatsapp/bot/status")
async def whatsapp_bot_status(request: Request) -> dict:
    require_session(request, _SLUG)
    return {
        "enabled":       whatsapp_bot.is_enabled(),
        "conversations": len(whatsapp_bot.list_conversations()),
    }


@router.post("/whatsapp/bot/toggle")
async def whatsapp_bot_toggle(body: WhatsAppBotToggleIn,
                              request: Request) -> dict:
    require_session(request, _SLUG)
    enabled = whatsapp_bot.set_enabled(body.enabled)
    logger.info("primewave whatsapp bot toggled: enabled=%s", enabled)
    return {"enabled": enabled}


@router.get("/whatsapp/bot/conversations")
async def whatsapp_bot_conversations(request: Request) -> dict:
    require_session(request, _SLUG)
    return {"conversations": whatsapp_bot.list_conversations()}


@router.delete("/whatsapp/bot/conversations/{phone}")
async def whatsapp_bot_clear_conversation(phone: str, request: Request) -> dict:
    require_session(request, _SLUG)
    ok = whatsapp_bot.clear_conversation(phone)
    if not ok:
        raise HTTPException(404, "conversation not found")
    return {"ok": True}


@router.delete("/whatsapp/bot/conversations")
async def whatsapp_bot_clear_all(request: Request) -> dict:
    require_session(request, _SLUG)
    removed = whatsapp_bot.clear_all_conversations()
    return {"ok": True, "removed": removed}


@router.get("/whatsapp/inbox")
async def whatsapp_inbox_dump(request: Request) -> dict:
    require_session(request, _SLUG)
    return {"messages": whatsapp_inbox.list_inbox()}


@router.delete("/whatsapp/inbox")
async def whatsapp_inbox_clear(request: Request) -> dict:
    require_session(request, _SLUG)
    removed = whatsapp_inbox.clear_inbox()
    return {"ok": True, "removed": removed}


class WhatsAppSendIn(BaseModel):
    to:   str
    text: str


@router.post("/whatsapp/send")
async def whatsapp_send(body: WhatsAppSendIn, request: Request) -> dict:
    """Send a text via WaSender. On success, append a local row to the
    inbox so the chat view reflects the send immediately."""
    require_session(request, _SLUG)
    if not (body.text or "").strip():
        raise HTTPException(400, "text is required")
    if not (body.to or "").strip():
        raise HTTPException(400, "to is required")

    api_key  = state.pwa_wasender_api_key or ""
    personal = state.pwa_wasender_personal_token or ""
    client = build_wasender_client(api_key, personal)
    result = await client.send_text(body.to, body.text)

    if result.get("ok"):
        phone = normalize_phone(body.to)
        whatsapp_inbox.append_outbound(
            to=phone,
            text=body.text,
            message_id=result.get("message_id") or "",
        )
    return result


# ----- Media (send + outgoing serve + incoming proxy) ---------------------
# WaSender's send-message API accepts a public URL for media (imageUrl /
# videoUrl / audioUrl / documentUrl). We save the operator's upload to a
# per-machine dir, then hand WaSender a public URL pointing at our own
# /whatsapp/outgoing/{filename} route. WaSender's servers fetch it, then
# forward to WhatsApp.
#
# For INCOMING media we proxy through /whatsapp/media/{message_id} so the
# SPA's <img>/<video>/<audio> can show it without bouncing through
# WaSender's auth-required CDN URLs in-browser.

import uuid as _uuid
from pathlib import Path as _Path

_WA_OUTGOING_DIR = _Path(__file__).resolve().parents[4] \
    / "data" / "demos" / "primewave" / "whatsapp_outgoing"


def _detect_media_kind(content_type: str, filename: str) -> str:
    """Map an upload's content_type / extension onto WaSender's
    messageType enum: image | video | audio | document.

    Content-Type FIRST, extension only as fallback. Critical for
    voice-note recordings — the browser saves them as `.webm` (audio in
    a webm container), with Content-Type `audio/webm;codecs=opus`. If
    we look at the extension first we'd see `.webm` and classify as
    video, skip the OGG transcode, and ship a video file to WhatsApp.
    """
    ct = (content_type or "").lower().split(";")[0].strip()
    ext = _Path(filename or "").suffix.lower().lstrip(".")
    # 1. Trust the Content-Type when it's an explicit top-level type.
    if ct.startswith("audio/"): return "audio"
    if ct.startswith("image/"): return "image"
    if ct.startswith("video/"): return "video"
    # 2. Fall back to extension when Content-Type is generic/missing.
    if ext in {"jpg", "jpeg", "png", "gif", "webp"}:            return "image"
    if ext in {"mp3", "ogg", "oga", "opus", "m4a", "aac", "wav"}: return "audio"
    if ext in {"mp4", "mov", "webm", "mkv", "m4v"}:             return "video"
    return "document"


@router.post("/whatsapp/send-media")
async def whatsapp_send_media(
    request: Request,
    to:      str = Form(...),
    caption: str = Form(""),
    kind:    Optional[str] = Form(None),       # optional override
    file:    UploadFile = File(...),
) -> dict:
    """Send an image / video / audio / document via WaSender.

    The upload is stored at whatsapp_outgoing/<uuid>.<ext>. WaSender
    fetches the resulting public URL (Cloudflare tunnel exposes it as
    https://demosys.primewave2.tech/...) and forwards to WhatsApp.
    """
    require_session(request, _SLUG)
    if not (to or "").strip():
        raise HTTPException(400, "to is required")
    if not file or not file.filename:
        raise HTTPException(400, "file is required")
    api_key = state.pwa_wasender_api_key or ""
    if not api_key:
        return {"ok": False, "status": 0,
                "error": "WaSender API key not configured."}

    # Save under a randomised filename — collision-proof + path-traversal-proof.
    ext = (_Path(file.filename).suffix.lower()[:10] or ".bin")
    safe_name = f"{_uuid.uuid4().hex}{ext}"
    _WA_OUTGOING_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _WA_OUTGOING_DIR / safe_name
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    out_path.write_bytes(data)

    # Detect media kind BEFORE the transcode so we know whether to fix audio.
    detected = _detect_media_kind(file.content_type or "", file.filename)
    kind_for_check = (kind or detected).strip().lower()
    logger.info("primewave send-media: filename=%r content_type=%r → detected=%s, using=%s",
                file.filename, file.content_type, detected, kind_for_check)

    # ---- Voice-note fix: WhatsApp ONLY accepts OGG/Opus for `audio`
    # messages (renders as video clip otherwise). And Chrome's OGG/Opus
    # decoder also chokes on stream-copied containers from MediaRecorder
    # webm — playback truncates after a few seconds because the OGG
    # page granules carry over webm-style timestamps. A fresh re-encode
    # via libopus produces clean OpusHead + properly-paginated Opus
    # frames that BOTH WhatsApp and Chrome decode end-to-end.
    if kind_for_check == "audio" and ext.lower() not in (".ogg", ".oga", ".opus"):
        ogg_name = f"{_uuid.uuid4().hex}.ogg"
        ogg_path = _WA_OUTGOING_DIR / ogg_name
        import subprocess
        try:
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", str(out_path),
                 "-vn",                          # drop any video track
                 "-c:a", "libopus", "-b:a", "32k",
                 "-ar", "48000", "-ac", "1",     # opus-native rate, mono
                 "-application", "voip",         # voice-tuned encoder
                 "-frame_duration", "20",        # 20ms frames — universally decoded
                 "-vbr", "on",
                 "-f", "ogg", str(ogg_path)],
                capture_output=True, timeout=30,
            )
        except Exception as e:
            logger.warning("primewave audio re-encode subprocess failed: %s", e)
            proc = None

        if proc and proc.returncode == 0 and ogg_path.exists() and ogg_path.stat().st_size > 0:
            try: out_path.unlink()
            except Exception: pass
            out_path = ogg_path
            safe_name = ogg_name
            logger.info("primewave audio → fresh ogg/opus 48k voip (%d bytes): %s",
                        ogg_path.stat().st_size, safe_name)
        else:
            stderr = proc.stderr.decode("utf-8", "ignore")[:300] if proc else "(no proc)"
            logger.warning("primewave ffmpeg re-encode failed: %s", stderr)

    # Build the public URL WaSender will fetch from. Uses the request's
    # own scheme + host — works as long as the SPA is being served via
    # the public tunnel hostname (which the operator's browser already
    # is, since they got here through it).
    public_url = str(request.url_for("primewave_whatsapp_outgoing",
                                       filename=safe_name))
    private_host = any(s in public_url for s in (
        "localhost", "127.0.0.1", "://10.", "://192.168.", "://172.16.",
        "://172.17.", "://172.18.", "://172.19.", "://172.2",
        "://172.30.", "://172.31.",
    ))
    if private_host:
        return {"ok": False, "status": 0, "url": public_url,
                "error": ("Media URL would point at a private/loopback "
                          f"address ({public_url}) that WaSender can't "
                          "reach. Browse the SPA via demosys.primewave2.tech "
                          "instead of a LAN URL, then retry.")}

    detected = _detect_media_kind(file.content_type or "", file.filename)
    kind = (kind or detected).strip().lower()
    if kind not in ("image", "video", "audio", "document"):
        kind = detected

    body: dict = {"to": normalize_phone(to), "messageType": kind}
    if kind == "image":
        body["imageUrl"] = public_url
        if caption.strip(): body["text"] = caption.strip()
    elif kind == "video":
        body["videoUrl"] = public_url
        if caption.strip(): body["text"] = caption.strip()
    elif kind == "audio":
        body["audioUrl"] = public_url
        # ptt=true tells WhatsApp to render as a voice note (PTT — push to
        # talk). Without it some clients show the OGG/Opus as a music
        # file / video clip and refuse to play. Explicit mimetype matches
        # the transcoded output above.
        body["ptt"] = True
        body["mimetype"] = "audio/ogg; codecs=opus"
    else:
        body["documentUrl"] = public_url
        body["fileName"] = file.filename
        if caption.strip(): body["text"] = caption.strip()

    # POST to WaSender — manual because the WasenderClient only handles
    # plain text. Same Bearer header pattern.
    import httpx
    from .wasender import WASENDER_BASE_URL
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{WASENDER_BASE_URL}/send-message",
                                  headers=headers, json=body)
    except httpx.RequestError as e:
        return {"ok": False, "status": 0, "error": f"network: {e}"}

    try:
        payload = r.json()
    except Exception:
        payload = {"raw_text": (r.text or "")[:500]}

    if 200 <= r.status_code < 300:
        data_field = (payload.get("data") or {}) if isinstance(payload, dict) else {}
        msg_id = data_field.get("message_id") or ""
        # Mirror to the local inbox so the chat view shows it instantly.
        # Embed a primewave://local pointer so the renderer can show it
        # without round-tripping to WaSender.
        outbound_row = {
            "id":           msg_id or f"local-{_uuid.uuid4().hex[:12]}",
            "from_me":      True,
            "to":           normalize_phone(to),
            "jid":          f"{normalize_phone(to)}@s.whatsapp.net",
            "text":         caption.strip() if caption else "",
            "received_at":  int(__import__("time").time()),
            "source":       "outbound-media",
            "media_kind":   kind,
            "media_local":  safe_name,
            "media_url":    public_url,
        }
        whatsapp_inbox.store_webhook({"message": outbound_row, "id": outbound_row["id"]})
        return {"ok": True, "status": r.status_code, "message_id": msg_id,
                "url": public_url, "kind": kind}
    err = (
        (isinstance(payload, dict) and (
            payload.get("error") or payload.get("message")
            or payload.get("detail")))
        or f"HTTP {r.status_code}"
    )
    return {"ok": False, "status": r.status_code, "error": err, "raw": payload}


@router.get("/whatsapp/outgoing/{filename}",
            name="primewave_whatsapp_outgoing")
async def whatsapp_serve_outgoing(filename: str):
    """Serve a file the operator uploaded for /whatsapp/send-media.
    Intentionally NOT session-gated — WaSender fetches the URL with no
    cookies. The random uuid filename is the only access control.

    Cache-Control: no-store prevents Cloudflare from caching the response.
    Without this, the CDN sometimes serves partial-content responses for
    Range requests during cache fill, which cuts off audio playback in
    the browser even though the underlying file is complete.
    """
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    p = _WA_OUTGOING_DIR / filename
    if not p.exists():
        raise HTTPException(404, "not found")
    import mimetypes
    mt, _ = mimetypes.guess_type(filename)
    return FileResponse(
        p,
        media_type=(mt or "application/octet-stream"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


_WA_MEDIA_CACHE = _Path(__file__).resolve().parents[4] \
    / "data" / "demos" / "primewave" / "whatsapp_media"
_WA_TRANSCRIPT_CACHE = _Path(__file__).resolve().parents[4] \
    / "data" / "demos" / "primewave" / "whatsapp_transcripts"

_EXT_FOR_KIND = {
    "audio":    "ogg",
    "image":    "jpg",
    "video":    "mp4",
    "document": "bin",
}


@router.get("/whatsapp/media/{message_id}")
async def whatsapp_proxy_incoming(message_id: str, request: Request):
    """Return the DECRYPTED media bytes for an inbound WhatsApp message.

    WhatsApp media is E2E-encrypted: the URL WaSender carries on
    `message.audioMessage.url` (etc.) points at WhatsApp's CDN, but the
    bytes are ciphertext. The block also carries the `mediaKey` we need
    for HKDF + AES-CBC decryption. Restaurant's `whatsapp_bot` module
    already implements the full handshake — reuse it here instead of
    forking 200 lines of crypto.

    Cached on disk because WaSender's presigned URLs expire in minutes
    and re-fetching wouldn't work after that.
    """
    require_session(request, _SLUG)
    if not message_id or len(message_id) > 128 or "/" in message_id or ".." in message_id:
        raise HTTPException(400, "invalid message_id")

    rows = whatsapp_inbox.list_inbox()
    msg = next((r for r in rows if r.get("id") == message_id
                or (r.get("key") or {}).get("id") == message_id), None)
    if not msg:
        raise HTTPException(404, "message not found")

    # Outbound rows already point at our own /outgoing/ route — redirect.
    if msg.get("media_url"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=msg["media_url"], status_code=302)

    # Reuse the restaurant module's media helpers (envelope shapes +
    # decrypt logic are identical across demos).
    from ..restaurant import whatsapp_bot as wa_bot
    block, kind, fname = wa_bot._find_media_block(msg)
    if not block or not kind:
        raise HTTPException(404, "message has no media block")

    ext = _EXT_FOR_KIND.get(kind, "bin")
    if kind == "document" and fname and "." in fname:
        ext = fname.rsplit(".", 1)[-1].lower()[:8] or "bin"
    cache_path = _WA_MEDIA_CACHE / f"{message_id}.{ext}"
    download_name = fname if (kind == "document" and fname) else f"{message_id}.{ext}"

    mime = wa_bot._mime_from_block(block, kind)
    if cache_path.exists():
        return FileResponse(
            str(cache_path), media_type=mime, filename=download_name,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    _WA_MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
    media_bytes, fetched_mime = await wa_bot._fetch_media_bytes(block, kind)
    if not media_bytes:
        raise HTTPException(502, "media fetch or decrypt failed — "
                                 "check journalctl for the bot debug log")
    if wa_bot._looks_like_media(media_bytes, kind):
        try: cache_path.write_bytes(media_bytes)
        except Exception:
            logger.exception("primewave: cache write failed for %s", message_id)
    else:
        logger.warning("primewave whatsapp_media: bytes don't match "
                       "expected %s container for id=%s — serving uncached",
                       kind, message_id)

    from fastapi.responses import Response as _Resp
    return _Resp(
        content=media_bytes,
        media_type=(fetched_mime or mime),
        headers={
            "Content-Disposition": f'inline; filename="{download_name}"',
            "Cache-Control":       "no-store, must-revalidate",
        },
    )


@router.get("/whatsapp/transcript/{message_id}")
async def whatsapp_transcript(message_id: str, request: Request) -> dict:
    """Transcribe a voice-note message via Gemini audio-understanding.
    Works for both outbound (file in whatsapp_outgoing/) and inbound
    (use the decrypt-and-cache pipeline) audio messages.

    Result is cached at data/demos/primewave/whatsapp_transcripts/
    <message_id>.txt so subsequent fetches are instant.

    Returns {"text": "...", "lang": "...", "cached": bool}. Never raises
    for normal failure modes — returns {text: "", error: "..."} so the
    SPA can render a graceful 'transcription unavailable' state.
    """
    require_session(request, _SLUG)
    if not message_id or len(message_id) > 128 \
            or "/" in message_id or ".." in message_id:
        raise HTTPException(400, "invalid message_id")

    _WA_TRANSCRIPT_CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = _WA_TRANSCRIPT_CACHE / f"{message_id}.txt"
    if cache_path.exists():
        try:
            return {"text": cache_path.read_text(encoding="utf-8").strip(),
                    "cached": True}
        except Exception:
            pass

    # Locate the audio bytes — outbound rows point at whatsapp_outgoing/,
    # inbound rows go through the decrypt pipeline (or the existing
    # whatsapp_media/ cache).
    rows = whatsapp_inbox.list_inbox()
    msg = next((r for r in rows if r.get("id") == message_id
                or (r.get("key") or {}).get("id") == message_id), None)
    if not msg:
        return {"text": "", "error": "message not found"}

    audio_bytes: Optional[bytes] = None
    mime = "audio/ogg"
    if msg.get("media_kind") == "audio" and msg.get("media_local"):
        # Outbound — read directly from the local outgoing dir.
        local_path = _WA_OUTGOING_DIR / msg["media_local"]
        if local_path.exists():
            audio_bytes = local_path.read_bytes()
            mime = "audio/ogg"
    else:
        # Inbound — reuse the proxy's decrypt pipeline. Prefer the
        # whatsapp_media cache if it's already there.
        cache_audio = _WA_MEDIA_CACHE / f"{message_id}.ogg"
        if cache_audio.exists():
            audio_bytes = cache_audio.read_bytes()
            mime = "audio/ogg"
        else:
            from ..restaurant import whatsapp_bot as wa_bot
            block, kind, _ = wa_bot._find_media_block(msg)
            if not block or kind != "audio":
                return {"text": "", "error": "not a voice note"}
            audio_bytes, fetched_mime = await wa_bot._fetch_media_bytes(block, "audio")
            mime = (fetched_mime or "audio/ogg").split(";")[0].strip()
            if audio_bytes and wa_bot._looks_like_media(audio_bytes, "audio"):
                _WA_MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
                try: (cache_audio).write_bytes(audio_bytes)
                except Exception: pass

    if not audio_bytes:
        return {"text": "", "error": "audio bytes unavailable"}
    if not state.gemini_api_key:
        return {"text": "", "error": "Gemini API key not configured"}

    # Gemini audio-understanding — same prompt structure as clinic's
    # _enhance_transcript, scoped to single-speaker voice notes.
    try:
        from google import genai as _genai
        from google.genai import types as _types
        client = _genai.Client(api_key=state.gemini_api_key)
        part = _types.Part.from_bytes(data=audio_bytes, mime_type=mime)
        prompt = (
            "Transcribe this WhatsApp voice note exactly as spoken. "
            "Preserve the original language — Arabic stays Arabic, "
            "English stays English, mixed stays mixed. Do NOT translate. "
            "Return ONLY the transcript text — no commentary, no quotes, "
            "no language label. If the audio is unintelligible, return "
            "an empty string."
        )
        for model in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=[part, prompt],
                )
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    try: cache_path.write_text(text, encoding="utf-8")
                    except Exception: pass
                    return {"text": text, "model": model, "cached": False}
            except Exception as e:
                logger.warning("primewave transcript %s model %s failed: %s",
                               message_id, model, e)
                continue
        return {"text": "", "error": "all transcription models failed"}
    except Exception as e:
        logger.exception("primewave transcript %s crashed", message_id)
        return {"text": "", "error": f"{type(e).__name__}: {e}"}


# ============================================================================
# Contacts — operator-managed leads (separate from per-call snapshots)
# ============================================================================
# Same six fields the agent's `record_customer_info` tool captures, plus
# Email and Lead Source which the operator manages by hand. 20-row Riyadh
# dummy seed gets written on first read so the SPA's Contacts → List page
# isn't empty.

class ContactIn(BaseModel):
    name:          Optional[str] = None
    phone:         Optional[str] = None
    email:         Optional[str] = None
    location:      Optional[str] = None
    interest:      Optional[str] = None
    project_phase: Optional[str] = None
    lead_source:   Optional[str] = None


@router.get("/contacts")
async def contacts_list(request: Request) -> dict:
    require_session(request, _SLUG)
    return {"contacts": contacts_mod.list_contacts()}


@router.post("/contacts")
async def contacts_create(body: ContactIn, request: Request) -> dict:
    """Create a new contact. Empty fields are stored as empty strings —
    the SPA can patch them in later via PUT."""
    require_session(request, _SLUG)
    row = contacts_mod.upsert_contact(body.model_dump(), contact_id=None)
    return {"ok": True, "contact": row}


@router.put("/contacts/{contact_id}")
async def contacts_update(contact_id: str, body: ContactIn,
                          request: Request) -> dict:
    require_session(request, _SLUG)
    row = contacts_mod.upsert_contact(body.model_dump(), contact_id=contact_id)
    return {"ok": True, "contact": row}


@router.delete("/contacts/{contact_id}")
async def contacts_delete(contact_id: str, request: Request) -> dict:
    require_session(request, _SLUG)
    ok = contacts_mod.delete_contact(contact_id)
    if not ok:
        raise HTTPException(404, "contact not found")
    return {"ok": True}


@router.post("/contacts/reset")
async def contacts_reset(request: Request) -> dict:
    """Wipe + re-seed with the built-in 20-row Riyadh dummy set."""
    require_session(request, _SLUG)
    rows = contacts_mod.reset_contacts()
    return {"ok": True, "contacts": rows}
