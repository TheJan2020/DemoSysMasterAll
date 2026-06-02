"""
WhatsApp auto-responder bot for Primewave.

Toggled by `state.pwa_whatsapp_bot_enabled`. When ON, every inbound
message dispatched from /whatsapp/webhook is fed to a Gemini text
model that has the same persona + KB + tools as the Live Agent
(`record_customer_info` etc.). Conversation history is kept per phone
on disk so context survives a service restart.

Skipped messages:
  - Outbound (key.fromMe true)
  - Group (jid ends @g.us)
  - LIDs with no resolvable phone (key.cleanedSenderPn missing)
  - Messages we've already processed (seen-id cache)
  - Plain protocolMessage / status updates with no text or voice

Voice notes are transcribed via `live_agent.transcribe_audio_bytes`
before being fed to the conversation. The text reply is sent back via
WaSender's /send-message and mirrored to the local inbox so the SPA
shows the response immediately.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from ...core.state import state
from . import whatsapp_inbox
from . import caller_context
from .live_agent import (
    _build_tools, _build_system_instruction,
    CUSTOMER_FIELDS, transcribe_audio_bytes,
)
from .wasender import build_client as build_wasender_client, normalize_phone

logger = logging.getLogger("demo_primewave.whatsapp_bot")

_DATA_DIR   = Path(__file__).resolve().parents[4] / "data" / "demos" / "primewave"
_STATE_PATH = _DATA_DIR / "whatsapp_bot_state.json"
_LOCK = threading.Lock()

HISTORY_CAP   = 40                # turns to keep per conversation
SEEN_IDS_CAP  = 1000              # dedup ring buffer size
TOOL_MAX_ITER = 5                 # max tool-call iterations per inbound

# Default chat model — fall back chain matches the transcript pipeline.
_MODELS = ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash")


def _now() -> int:
    return int(time.time())


# ---- State IO --------------------------------------------------------------

def _default_state() -> dict:
    return {"conversations": {}, "seen_ids": []}


def _load_locked() -> dict:
    if not _STATE_PATH.exists():
        return _default_state()
    try:
        d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("primewave bot state corrupt — starting fresh")
        return _default_state()
    if not isinstance(d, dict):
        return _default_state()
    d.setdefault("conversations", {})
    d.setdefault("seen_ids", [])
    return d


def _save_locked(s: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _with_state(fn):
    with _LOCK:
        s = _load_locked()
        try:
            return fn(s)
        finally:
            _save_locked(s)


# ---- Toggle ---------------------------------------------------------------

def is_enabled() -> bool:
    return bool(state.pwa_whatsapp_bot_enabled)


def set_enabled(on: bool) -> bool:
    state.pwa_whatsapp_bot_enabled = bool(on)
    state.save()
    return state.pwa_whatsapp_bot_enabled


# ---- Conversation introspection (used by /whatsapp/bot/conversations) -----

def list_conversations() -> list[dict]:
    with _LOCK:
        convs = _load_locked().get("conversations") or {}
    out = []
    for phone, conv in convs.items():
        out.append({
            "phone":         phone,
            "pushName":      conv.get("pushName") or "",
            "last_seen":     conv.get("last_seen") or 0,
            "turn_count":    len(conv.get("history") or []),
            "customer_data": conv.get("customer_data") or {},
        })
    out.sort(key=lambda c: c.get("last_seen") or 0, reverse=True)
    return out


def clear_conversation(phone: str) -> bool:
    def _do(s):
        convs = s.get("conversations") or {}
        if phone in convs:
            del convs[phone]
            return True
        return False
    return _with_state(_do)


def clear_all_conversations() -> int:
    def _do(s):
        n = len(s.get("conversations") or {})
        s["conversations"] = {}
        return n
    return _with_state(_do)


# ---- Message extraction ---------------------------------------------------

def _extract_phone(msg: dict) -> str:
    """Resolve sender phone — same precedence as the SPA's waPhoneOf:
    key.cleanedSenderPn > key.senderPn > key.participantPn > fallback."""
    k = msg.get("key") or {}
    for src in (k.get("cleanedSenderPn"), k.get("senderPn"),
                k.get("participantPn"),
                msg.get("from"), msg.get("sender"),
                k.get("remoteJid"), msg.get("remoteJid")):
        if isinstance(src, str):
            head = src.split("@")[0]
            digits = "".join(c for c in head if c.isdigit())
            if digits:
                return digits
    return ""


def _extract_text(msg: dict) -> str:
    for k in ("messageBody", "text", "body"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    inner = msg.get("message") or {}
    if not isinstance(inner, dict):
        return ""
    v = inner.get("conversation")
    if isinstance(v, str) and v.strip():
        return v.strip()
    ext = inner.get("extendedTextMessage")
    if isinstance(ext, dict) and isinstance(ext.get("text"), str):
        return ext["text"].strip()
    img = inner.get("imageMessage")
    if isinstance(img, dict) and isinstance(img.get("caption"), str):
        return img["caption"].strip()
    return ""


async def _transcribe_voice_if_any(msg: dict) -> str:
    """If `msg` is a voice note, run it through the Live Agent's
    transcribe helper. Reuses the decrypt pipeline + whatsapp_media
    cache via `whatsapp_bot._find_media_block` from restaurant."""
    inner = msg.get("message") or {}
    if not isinstance(inner.get("audioMessage"), dict):
        return ""
    from ..restaurant import whatsapp_bot as rest_bot
    block, kind, _ = rest_bot._find_media_block(msg)
    if not block or kind != "audio":
        return ""
    audio_bytes, fetched_mime = await rest_bot._fetch_media_bytes(block, "audio")
    if not audio_bytes:
        return ""
    mime = (fetched_mime or "audio/ogg").split(";")[0].strip()
    return await transcribe_audio_bytes(audio_bytes, mime)


# ---- Tool execution -------------------------------------------------------

def _apply_customer_info(conv: dict, args: dict) -> dict:
    """Bot-side version of the live agent's _apply_customer_info — same
    fields, persists into the conversation's customer_data dict."""
    updated: dict[str, str] = {}
    cd = conv.setdefault("customer_data", {})
    for k in CUSTOMER_FIELDS:
        v = args.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        cd[k] = v
        updated[k] = v
    if updated:
        logger.info("primewave bot: customer_info updated %s for %s",
                    list(updated.keys()), conv.get("phone"))
    return {"saved": list(updated.keys()), "current": dict(cd)}


# ---- Agent loop -----------------------------------------------------------

def _system_instruction_for_bot(conv: dict) -> str:
    """Persona + KB + WhatsApp-channel hint + per-conversation caller
    context (phone, contact lookup, past history). The persona was
    written for voice calls so we add a prefix telling the model to
    behave for text chat AND that we already know the caller's phone."""
    base = _build_system_instruction()
    phone    = conv.get("phone") or ""
    captured = conv.get("customer_data") or {}

    # Returning-caller block — name, past calls, past WhatsApp context.
    # Empty string if we have no record of this phone.
    returning_block = caller_context.format_for_prompt(
        caller_context.lookup(phone),
        channel="whatsapp",
    )

    channel_hint = (
        "## CHANNEL = WhatsApp (text chat)\n"
        "You are replying via WhatsApp text, NOT a voice call. So:\n"
        "  - Do NOT open every reply with the full greeting line. Greet "
        "only on the very first message of the conversation.\n"
        "  - Keep replies SHORT — 1–3 sentences is ideal for WhatsApp.\n"
        "  - Use voice-style Arabic / English as appropriate to what the "
        "caller wrote, but write as text — no spoken-only filler.\n"
        "  - Call `record_customer_info` whenever the caller shares a "
        "field (name / phone / location / interest / project_phase / "
        "purchase_intent), the same as on a voice call.\n"
    )

    # Caller context — what we ALREADY know from the WhatsApp envelope
    # + previous turns. CRITICAL: the model needs to be told these are
    # facts so it doesn't ask for them as if it had no context.
    ctx_lines = []
    if phone:
        ctx_lines.append(f"  - WhatsApp sender phone: +{phone}")
    if captured:
        for k, v in captured.items():
            if k == "phone": continue   # already shown above
            ctx_lines.append(f"  - {k}: {v}")

    caller_ctx_block = (
        "\n## CALLER CONTEXT — already known to you\n"
        + ("\n".join(ctx_lines) if ctx_lines else "  (nothing captured yet)")
        + "\n\nIMPORTANT — phone number rules on WhatsApp:\n"
        "  - The caller is messaging from +" + phone + " (WhatsApp "
        "shows us the sender phone automatically). You ALREADY HAVE "
        "this number. Do NOT ask 'what is your phone number?' as if "
        "you don't know it.\n"
        "  - INSTEAD, when you reach the point in the intake flow "
        "where you'd ask for phone, send a confirmation like:\n"
        "      'تمام، أتواصل معك على نفس هذا الرقم +" + phone + "؟ "
        "أو تفضل رقم ثاني؟'\n"
        "      'OK, shall I follow up on this same number +" + phone
        + ", or a different one?'\n"
        "  - If they confirm OR don't object → call "
        "`record_customer_info(phone=\"" + phone + "\")` once.\n"
        "  - If they give a DIFFERENT number → call "
        "`record_customer_info(phone=\"<new digits>\")` with that one "
        "instead.\n"
        "Other captured fields above are also confirmed unless the "
        "caller corrects them.\n"
    )

    return channel_hint + "\n" + base + caller_ctx_block + returning_block


def _history_to_contents(history: list[dict]) -> list:
    """Turn the saved history (user/model rows) into Gemini Content list."""
    out = []
    for turn in history:
        role = "user" if turn.get("role") == "user" else "model"
        text = turn.get("text") or ""
        if not text:
            continue
        out.append(types.Content(role=role,
                                 parts=[types.Part(text=text)]))
    return out


async def _run_agent_loop(conv: dict) -> str:
    """Drive the multi-turn function-calling loop. Returns the final
    text reply (possibly empty)."""
    if not state.gemini_api_key:
        return ""
    client = genai.Client(api_key=state.gemini_api_key)

    contents = _history_to_contents(conv.get("history") or [])
    if not contents:
        return ""

    cfg = types.GenerateContentConfig(
        system_instruction=_system_instruction_for_bot(conv),
        tools=_build_tools(),
        temperature=0.6,
    )

    last_err: Optional[Exception] = None
    for iteration in range(TOOL_MAX_ITER):
        # Try the model chain on each iteration so a 503/rate-limit on
        # one model doesn't kill the whole turn.
        resp = None
        for model in _MODELS:
            try:
                resp = await client.aio.models.generate_content(
                    model=model, contents=contents, config=cfg,
                )
                if resp:
                    break
            except Exception as e:
                last_err = e
                logger.warning("primewave bot: model %s failed: %s", model, e)
                continue
        if not resp:
            raise last_err or RuntimeError("all chat models failed")

        cands = getattr(resp, "candidates", None) or []
        if not cands:
            return ""
        cand = cands[0]
        content = getattr(cand, "content", None)
        parts = list(getattr(content, "parts", None) or [])

        tool_calls = [p.function_call for p in parts
                      if getattr(p, "function_call", None)]
        text_chunks = [p.text for p in parts if getattr(p, "text", None)]

        if tool_calls:
            # Echo the model's tool-call turn into the contents so the
            # follow-up tool response references it.
            contents.append(types.Content(role="model", parts=parts))
            tool_responses_parts = []
            for fc in tool_calls:
                args = dict(getattr(fc, "args", None) or {})
                name = getattr(fc, "name", "")
                if name == "record_customer_info":
                    result = _apply_customer_info(conv, args)
                else:
                    logger.warning("primewave bot: unknown tool %s", name)
                    result = {"error": f"unknown tool {name}"}
                tool_responses_parts.append(types.Part(
                    function_response=types.FunctionResponse(
                        name=name, response={"result": result},
                    ),
                ))
            contents.append(types.Content(role="user", parts=tool_responses_parts))
            continue  # next iteration → model produces final text

        # No tool call — model returned text. We're done.
        return "".join(text_chunks).strip()

    # Hit the tool-iteration cap — bail with a soft message.
    logger.warning("primewave bot: hit TOOL_MAX_ITER for %s", conv.get("phone"))
    return ""


# ---- Public entry point ---------------------------------------------------

async def handle_inbound(msg: dict) -> dict:
    """Called from /whatsapp/webhook after each stored message. Returns
    a diagnostic dict; never raises."""
    try:
        return await _handle_inbound_inner(msg)
    except Exception as e:
        logger.exception("primewave bot handle_inbound crashed")
        return {"ok": False, "action": "error", "reason": f"{type(e).__name__}: {e}"}


async def _handle_inbound_inner(msg: dict) -> dict:
    if not is_enabled():
        return {"ok": True, "action": "skip", "reason": "bot_disabled"}

    k = msg.get("key") or {}
    if k.get("fromMe"):
        return {"ok": True, "action": "skip", "reason": "outbound"}

    msg_id = msg.get("id") or k.get("id") or ""
    jid    = k.get("remoteJid") or msg.get("remoteJid") or ""
    if isinstance(jid, str) and jid.endswith("@g.us"):
        return {"ok": True, "action": "skip", "reason": "group"}

    phone = _extract_phone(msg)
    if not phone:
        return {"ok": True, "action": "skip", "reason": "no_phone"}

    # Dedupe by message_id.
    def _dedup(s):
        seen = s.get("seen_ids") or []
        if msg_id and msg_id in seen:
            return True
        if msg_id:
            seen.append(msg_id)
            s["seen_ids"] = seen[-SEEN_IDS_CAP:]
        return False
    if msg_id and _with_state(_dedup):
        return {"ok": True, "action": "skip", "reason": "duplicate"}

    # Extract text — direct first, then voice transcription.
    text = _extract_text(msg)
    via  = "text"
    if not text:
        text = await _transcribe_voice_if_any(msg)
        via  = "voice-transcript" if text else via
    if not text:
        return {"ok": True, "action": "skip", "reason": "no_text_extractable"}

    # Append user turn + run agent. Hold the lock only for the IO bits.
    def _start_turn(s):
        convs = s.setdefault("conversations", {})
        conv = convs.setdefault(phone, {
            "phone":         phone,
            "pushName":      msg.get("pushName") or "",
            "history":       [],
            "customer_data": {},
            "last_seen":     _now(),
        })
        if msg.get("pushName"):
            conv["pushName"] = msg["pushName"]
        conv["last_seen"] = _now()
        # Seed the WhatsApp sender phone into customer_data immediately
        # so the system instruction can show it as "already captured" on
        # the very first turn — the model won't ask for a number it
        # already has. The model can still overwrite this later if the
        # caller offers a different callback number.
        cd = conv.setdefault("customer_data", {})
        if not cd.get("phone"):
            cd["phone"] = phone
        conv["history"].append({"role": "user", "text": text, "ts": _now(),
                                "via": via})
        if len(conv["history"]) > HISTORY_CAP * 2:
            conv["history"] = conv["history"][-HISTORY_CAP * 2:]
        # Return a deep-copy so the agent loop mutates it freely without
        # holding the lock; we re-merge on completion.
        import copy
        return phone, copy.deepcopy(conv)
    phone_out, working_conv = _with_state(_start_turn)
    logger.info("primewave bot: processing %s phone=%s via=%s text=%r",
                msg_id, phone_out, via, text[:80])

    reply_text = await _run_agent_loop(working_conv)

    def _finish_turn(s):
        convs = s.setdefault("conversations", {})
        # Merge the customer_data the agent updated back in.
        conv = convs.setdefault(phone_out, working_conv)
        cd_now = conv.setdefault("customer_data", {})
        cd_now.update(working_conv.get("customer_data") or {})
        if reply_text:
            conv["history"].append({"role": "model", "text": reply_text,
                                    "ts": _now()})
        conv["last_seen"] = _now()
    _with_state(_finish_turn)

    if not reply_text:
        return {"ok": True, "action": "no_reply", "phone": phone_out}

    # Send the reply.
    api_key  = state.pwa_wasender_api_key or ""
    personal = state.pwa_wasender_personal_token or ""
    if not api_key:
        return {"ok": False, "action": "send_failed",
                "reason": "wasender_api_key_not_configured"}
    client = build_wasender_client(api_key, personal)
    send_result = await client.send_text(phone_out, reply_text)
    if send_result.get("ok"):
        whatsapp_inbox.append_outbound(
            to=normalize_phone(phone_out),
            text=reply_text,
            message_id=send_result.get("message_id") or "",
        )
        return {"ok": True, "action": "replied", "phone": phone_out,
                "text_preview": reply_text[:120]}
    return {"ok": False, "action": "send_failed",
            "reason": send_result.get("error") or "wasender_error",
            "raw": send_result.get("raw")}
