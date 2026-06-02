"""Primewave Main Demo Live Agent.

Knowledge-only Gemini Live + AudioSocket bridge, single voice provider
(Gemini), no tools, no recording, no escalation. The smaller cousin of
clinic/live_agent.py — same wire protocol, much less surface area. When
Primewave needs WhatsApp / supervisor / tools later we can crib from
clinic, but for now the agent just answers calls, speaks the configured
persona, and reads from the on-disk knowledge base.

Wire-level details mirror clinic exactly so the FreePBX dialplan can
target this listener with the same `AudioSocket(<uuid>, host:port)` call
without any protocol changes.
"""
from __future__ import annotations

import asyncio
import audioop
import json
import logging
import shutil
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from ...core.state import state

logger = logging.getLogger("primewave_live_agent")

# ---- AudioSocket framing (chan_audiosocket) --------------------------------
_AS_HANGUP = 0x00
_AS_UUID   = 0x01
_AS_DTMF   = 0x02
_AS_ERROR  = 0x03
_AS_AUDIO  = 0x10

_SAMPLE_WIDTH = 2
_GEMINI_API_VERSION = "v1alpha"

# ---- On-disk overrides — Settings page POSTs persona/kb here ---------------
_DATA_DIR     = Path(__file__).resolve().parents[4] / "data" / "demos" / "primewave"
_PERSONA_PATH = _DATA_DIR / "persona.txt"
_KB_PATH      = _DATA_DIR / "kb.txt"
_CALLS_DIR    = _DATA_DIR / "calls"

# Customer-info schema — fields the `record_customer_info` tool can set.
# Kept here so the SPA can render the same labels + enum hints.
CUSTOMER_FIELDS = ["main_interest", "name", "phone", "location",
                   "project_phase", "purchase_intent"]
CUSTOMER_FIELD_LABELS = {
    "main_interest":   "Main interest",
    "name":            "Name",
    "phone":           "Phone",
    "location":        "Location",
    "project_phase":   "Project phase",
    "purchase_intent": "Purchase intent",
}


DEFAULT_PERSONA = """# Nora — Primewave platform intake agent

You are Nora (نورة), the AI voice agent for Primewave — a Saudi
company building operator-grade AI for voice, vision and smart-home
automation. You answer phone calls that land on the company's main
line.

## Voice & tone
- Warm, professional, concise. Never robotic.
- Sentences short. One question at a time.
- Use the caller's first name once they share it.

## Arabic gender — default to MASCULINE
- Address the caller with masculine forms by default. Switch to
  feminine only after hearing a clearly female voice or a woman's
  name.

## Language — Arabic by default
- Greet in Arabic (Najdi / Hijazi). Detect the caller's language
  from their first reply and switch smoothly: English, Urdu, Tagalog,
  French, or mixed Arabic-English.

## Greeting (always Arabic, exact wording)
"السلام عليكم، شركة برايم ويف. أنا نورة. كيف أقدر أخدمك اليوم؟"

## Who you talk to
Callers are usually prospects who saw a demo, partners following up,
or existing customers with questions about their deployment. Your
job is to:
1. Find out which use-case they care about (clinic, restaurant, smart
   home, AI cameras, voice automation, or "general questions").
2. Capture name + mobile so a Primewave engineer can call back.
3. If they want a live walkthrough, mention that the platform has
   browsable demos at demosys.primewave2.tech (clinic + restaurant
   are live now).
4. For pricing / SLAs / contracts — say a Primewave engineer will
   follow up; do NOT quote numbers.

## You DO NOT
- Promise delivery dates, pricing, or contractual terms.
- Discuss customer data from other deployments.
- Hand out personal contact details of staff.

## Identity check
There is no caller database for this demo. Just ask politely for the
caller's name and mobile, and acknowledge them by name from then on.
"""

DEFAULT_KB = """## About Primewave
Primewave builds operator-grade AI for voice, vision, smart-home /
office automation, and industry verticals. Headquartered in Riyadh,
Saudi Arabia.

## What we ship
- **Voice agents** — Arabic-first phone receptionists (Gemini Live +
  AudioSocket on FreePBX). Live in Clinic and Restaurant verticals.
- **AI cameras** — vision rules over Frigate NVR streams, kitchen
  monitoring, motion-triggered alerts to Home Assistant scripts.
- **Smart home / office automation** — Home Assistant integration,
  scripts, MQTT.
- **WhatsApp bot** — WaSender integration for templated outreach +
  inbox.

## Live demos (browsable)
- demosys.primewave2.tech/demo/clinic    — Mate-Layla clinic intake
- demosys.primewave2.tech/demo/restaurant — Mate-Rashed restaurant
  intake + kitchen AI rules

## What we don't do
- We are NOT a generic SaaS — every vertical is operator-tuned.
- We do NOT sell raw API access. Customers get a hosted deployment.

## Pricing
Talk to a Primewave engineer for pricing. Do not quote numbers on
the phone.

## Contact
Mobile and name capture during the call → a Primewave engineer will
call back within one business day.
"""


def load_persona() -> str:
    if _PERSONA_PATH.exists():
        try:
            txt = _PERSONA_PATH.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            logger.exception("failed to read %s", _PERSONA_PATH)
    return DEFAULT_PERSONA


def load_kb() -> str:
    if _KB_PATH.exists():
        try:
            txt = _KB_PATH.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            logger.exception("failed to read %s", _KB_PATH)
    return DEFAULT_KB


def save_persona(text: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PERSONA_PATH.write_text((text or "").strip() + "\n", encoding="utf-8")


def save_kb(text: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _KB_PATH.write_text((text or "").strip() + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# Tool — record_customer_info
# ----------------------------------------------------------------------------
# Single tool with all-optional fields so the model can call it multiple
# times during the call as the caller shares each piece. Updates merge
# into CallSession.customer_data; the SPA polls /agent/status to see
# in-progress collection live.

def _build_tools() -> list:
    return [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="record_customer_info",
                description=(
                    "Save one or more fields about the caller. Call this "
                    "every time the caller shares a new piece of info, "
                    "with just the field(s) that changed — you do NOT need "
                    "to repeat fields you've already sent on this call. "
                    "If the caller corrects a value, call again with the "
                    "corrected value and that field will be overwritten."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "main_interest": types.Schema(
                            type=types.Type.STRING,
                            description="B2B or B2C — what segment the caller is asking about.",
                            enum=["B2B", "B2C"],
                        ),
                        "name": types.Schema(
                            type=types.Type.STRING,
                            description="Caller's name (first or full).",
                        ),
                        "phone": types.Schema(
                            type=types.Type.STRING,
                            description="Call-back number (Saudi mobiles start +966 or 05).",
                        ),
                        "location": types.Schema(
                            type=types.Type.STRING,
                            description="City or neighbourhood (e.g. Riyadh, Jeddah).",
                        ),
                        "project_phase": types.Schema(
                            type=types.Type.STRING,
                            description="Where the caller's project is in its lifecycle.",
                            enum=["design", "under_construction", "finishing", "operational"],
                        ),
                        "purchase_intent": types.Schema(
                            type=types.Type.STRING,
                            description="Whether the caller is actively budgeting or just exploring.",
                            enum=["purchasing", "exploring"],
                        ),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="flag_for_supervisor",
                description=(
                    "Raise a red flag for a human supervisor to take over "
                    "this call. The Call Center Dashboard surfaces the "
                    "flagged row in red with the reason you provide. Use "
                    "this when the caller is angry, frustrated, asks for "
                    "a manager / human, threatens a complaint, raises a "
                    "topic you can't handle (legal, contract negotiation, "
                    "pricing), or whenever continuing alone would only "
                    "make things worse. KEEP TALKING after calling this "
                    "tool — do NOT mention 'I'm flagging this' out loud. "
                    "A supervisor will join silently or take over."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "reason":   types.Schema(
                            type=types.Type.STRING,
                            description="One short sentence the supervisor will see on the Dashboard.",
                        ),
                        "severity": types.Schema(
                            type=types.Type.STRING,
                            enum=["normal", "high"],
                            description="'high' = drop everything (angry, complaint); "
                                        "'normal' = check when free. Default 'normal'.",
                        ),
                    },
                    required=["reason"],
                ),
            ),
        ]),
    ]


async def transcribe_audio_bytes(audio_bytes: bytes, mime: str = "audio/ogg") -> str:
    """Send audio bytes to Gemini for transcription. Returns the plain
    transcript text, or "" on failure. Used by both the /whatsapp/transcript
    HTTP endpoint AND the auto-responder bot — keeping the prompt + model
    fallback chain in one place."""
    if not audio_bytes or not state.gemini_api_key:
        return ""
    try:
        client = genai.Client(api_key=state.gemini_api_key)
        part = types.Part.from_bytes(data=audio_bytes, mime_type=mime)
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
                    return text
            except Exception as e:
                logger.warning("primewave transcribe_audio_bytes model %s failed: %s", model, e)
                continue
    except Exception:
        logger.exception("primewave transcribe_audio_bytes crashed")
    return ""


def list_agent_tools() -> list[dict]:
    """Introspect `_build_tools()` and return a JSON-serialisable
    snapshot of every function the agent can call. Used by the SPA's
    Configuration page to document the tool surface to operators."""
    out: list[dict] = []
    for tool in _build_tools():
        decls = getattr(tool, "function_declarations", None) or []
        for fn in decls:
            params_out: list[dict] = []
            schema = getattr(fn, "parameters", None)
            properties = getattr(schema, "properties", None) or {}
            required = set(getattr(schema, "required", None) or [])
            for pname, psch in properties.items():
                # Gemini types.Schema → plain dict-style row.
                ptype = getattr(psch, "type", None)
                ptype_str = getattr(ptype, "name", None) or str(ptype or "").upper()
                if ptype_str.startswith("TYPE."):
                    ptype_str = ptype_str.split(".", 1)[1]
                params_out.append({
                    "name":        pname,
                    "type":        ptype_str.lower(),
                    "description": getattr(psch, "description", "") or "",
                    "enum":        list(getattr(psch, "enum", None) or []),
                    "required":    pname in required,
                })
            out.append({
                "name":        getattr(fn, "name", ""),
                "description": getattr(fn, "description", "") or "",
                "parameters":  params_out,
            })
    return out


def _apply_customer_info(session: "CallSession", args: dict) -> dict:
    """Merge the tool args into the call's customer_data. Returns the
    updated dict so we can ack it back to Gemini."""
    updated: dict[str, str] = {}
    for k in CUSTOMER_FIELDS:
        v = args.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        session.customer_data[k] = v
        updated[k] = v
    if updated:
        logger.info("primewave call %s: customer_info %s",
                    session.call_id, updated)
    return {"saved": list(updated.keys()),
            "current": dict(session.customer_data)}


# ----------------------------------------------------------------------------
# Call recording — WAV writers (caller, agent, mixed) and dir-based save
# ----------------------------------------------------------------------------
# On disk:
#   data/demos/primewave/calls/<dir_id>/
#     meta.json    — { id, call_id, started_at, ended_at, duration_s,
#                      peer, uuid, customer_data, turns, voice }
#     caller.wav   — 8 kHz mono, what the caller said
#     agent.wav    — 8 kHz mono, what the agent said (silence between turns)
#     mixed.wav    — 8 kHz mono, overlay — what the room sounded like
# dir_id format:  YYYYMMDDTHHMMSS_<call_id>  (sortable + unique)

def _write_wav(path: Path, frames: list[bytes], rate_hz: int = 8000) -> None:
    if not frames:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(b"".join(frames))


def _write_agent_wav(path: Path,
                     agent_chunks: list[tuple[float, bytes]],
                     total_s: float,
                     rate_hz: int = 8000) -> None:
    """Render the agent-only timeline as a full-call WAV — silence between
    the chunks — so the reader hears the agent talk in real call time."""
    if not agent_chunks:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per_sec = rate_hz * 2
    last = agent_chunks[-1]
    span_s = max(total_s, last[0] + (len(last[1]) / bytes_per_sec))
    total_bytes = int(span_s * bytes_per_sec)
    if total_bytes <= 0:
        return
    buf = bytearray(total_bytes)
    for offset_s, chunk in agent_chunks:
        start = int(offset_s * bytes_per_sec) & ~1
        end = start + len(chunk)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[start:end] = chunk
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(bytes(buf))


def _write_mixed_wav(path: Path,
                     caller_frames: list[bytes],
                     agent_chunks: list[tuple[float, bytes]],
                     total_s: float,
                     rate_hz: int = 8000) -> None:
    """Overlay agent on top of caller at the right offsets — single
    'as the room sounded' WAV. Sample-by-sample audioop.add."""
    bytes_per_sec = rate_hz * 2
    caller_bytes = b"".join(caller_frames)
    span_s = total_s
    if agent_chunks:
        last = agent_chunks[-1]
        span_s = max(span_s, last[0] + (len(last[1]) / bytes_per_sec))
    span_s = max(span_s, len(caller_bytes) / bytes_per_sec)
    total_bytes = int(span_s * bytes_per_sec)
    if total_bytes <= 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    mixed = bytearray(total_bytes)
    n = min(len(caller_bytes), total_bytes)
    mixed[:n] = caller_bytes[:n]
    for offset_s, chunk in agent_chunks:
        start = int(offset_s * bytes_per_sec) & ~1
        end = start + len(chunk)
        if end > len(mixed):
            mixed.extend(b"\x00" * (end - len(mixed)))
        existing = bytes(mixed[start:end])
        if len(existing) < len(chunk):
            existing = existing + b"\x00" * (len(chunk) - len(existing))
        try:
            summed = audioop.add(existing, chunk, 2)
        except audioop.error:
            summed = chunk
        mixed[start:start + len(summed)] = summed
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(bytes(mixed))


def _save_call(session: "CallSession") -> Optional[str]:
    """Persist the just-ended call to its own subdirectory. Returns the
    dir_id used on disk. Best-effort — any failure here never blocks
    call teardown.

    Empty calls (no audio, no turns, no customer data) are skipped so
    the History page stays clean."""
    if (not session.turns
        and not session._caller_pcm8k
        and not session._agent_pcm8k
        and not session.customer_data):
        return None

    ended_at = time.time()
    started_at = session.started_at
    duration_s = max(0.0, ended_at - started_at)
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime(started_at))
    dir_id = f"{ts}_{session.call_id}"
    call_dir = _CALLS_DIR / dir_id

    try:
        _write_wav(call_dir / "caller.wav", session._caller_pcm8k)
    except Exception:
        logger.exception("primewave: write caller.wav failed")
    try:
        _write_agent_wav(call_dir / "agent.wav",
                         session._agent_pcm8k, duration_s)
    except Exception:
        logger.exception("primewave: write agent.wav failed")
    try:
        _write_mixed_wav(call_dir / "mixed.wav",
                         session._caller_pcm8k, session._agent_pcm8k, duration_s)
    except Exception:
        logger.exception("primewave: write mixed.wav failed")

    meta = {
        "id":              dir_id,
        "call_id":         session.call_id,
        "started_at":      started_at,
        "ended_at":        ended_at,
        "duration_s":      int(duration_s),
        "peer":            session.peer,
        "uuid":            session.uuid,
        "caller_phone":    session.caller_phone or "",
        "customer_data":   dict(session.customer_data),
        "turns":           list(session.turns),
        "voice":           state.pwa_voice or "Aoede",
        # Persisted supervisor flag (if the agent raised one during the call).
        # Survives across the active → completed transition so Call History
        # can show the red marker + reason after the call ends.
        "active_flag":     dict(session.active_flag) if session.active_flag else None,
        # Offline-enhancement state — flips through pending → running →
        # done | failed as `_enhance_transcript` runs in the background.
        "enhanced_turns":  None,
        "enhanced_status": "pending",
    }
    try:
        call_dir.mkdir(parents=True, exist_ok=True)
        (call_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("primewave: write meta.json failed")
    return dir_id


def list_saved_calls(limit: int = 100) -> list[dict]:
    """Return saved-call summaries (newest first). Each row includes
    customer_data (for the Data Collected page) AND the audio / turn
    flags (for the Call History page) — both pages read the same list."""
    if not _CALLS_DIR.exists():
        return []
    rows: list[dict] = []
    for d in _CALLS_DIR.iterdir():
        if not d.is_dir():
            continue
        mp = d / "meta.json"
        if not mp.exists():
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append({
            "id":                  m.get("id") or d.name,
            "call_id":             m.get("call_id"),
            "started_at":          m.get("started_at"),
            "ended_at":            m.get("ended_at"),
            "duration_s":          m.get("duration_s", 0),
            "peer":                m.get("peer"),
            "uuid":                m.get("uuid"),
            "caller_phone":        m.get("caller_phone") or "",
            "active_flag":         m.get("active_flag"),
            "customer_data":       m.get("customer_data") or {},
            "turn_count":          len(m.get("turns") or []),
            "enhanced_status":     m.get("enhanced_status") or (
                "done" if m.get("enhanced_turns") else "pending"
            ),
            "enhanced_turn_count": len(m.get("enhanced_turns") or []),
            "has_caller_wav":      (d / "caller.wav").exists(),
            "has_agent_wav":       (d / "agent.wav").exists(),
            "has_mixed_wav":       (d / "mixed.wav").exists(),
        })
    rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return rows[: max(0, int(limit))]


def load_saved_call(call_id: str) -> Optional[dict]:
    """Return the full meta.json for one saved call (includes `turns`),
    or None if not found."""
    mp = _CALLS_DIR / call_id / "meta.json"
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return None


def call_audio_path(call_id: str, side: str) -> Optional[Path]:
    """Resolve the WAV path for one side ('caller', 'agent', 'mixed').
    Returns None if the file doesn't exist."""
    if side not in ("caller", "agent", "mixed"):
        return None
    p = _CALLS_DIR / call_id / f"{side}.wav"
    return p if p.exists() else None


def delete_saved_call(call_id: str) -> bool:
    d = _CALLS_DIR / call_id
    if not d.exists() or not d.is_dir():
        return False
    try:
        shutil.rmtree(d)
        return True
    except Exception:
        logger.exception("primewave: failed to delete %s", d)
        return False


# ----------------------------------------------------------------------------
# Offline transcript enhancement — Gemini "audio understanding"
# ----------------------------------------------------------------------------
# Gemini Live's live transcription is approximate and sometimes mis-renders
# the Arabic / English mix. After the call ends we re-transcribe the mixed
# WAV with a non-Live Gemini model (response_schema = list of turns) and
# patch meta.json with `enhanced_turns`. The History page prefers that
# field when it's present.

_ENHANCE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]


def _patch_meta(call_dir: Path, patch: dict) -> None:
    meta_path = call_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    meta.update(patch)
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("primewave: patch meta.json failed for %s", call_dir.name)


async def _enhance_transcript(call_dir_id: str) -> None:
    """Re-transcribe mixed.wav with Gemini offline and write
    `enhanced_turns` back to meta.json. Best-effort — never raises."""
    call_dir = _CALLS_DIR / call_dir_id
    mixed_path = call_dir / "mixed.wav"
    if not mixed_path.exists():
        _patch_meta(call_dir, {"enhanced_status": "failed",
                               "enhanced_error": "mixed.wav missing"})
        return
    if not state.gemini_api_key:
        _patch_meta(call_dir, {"enhanced_status": "failed",
                               "enhanced_error": "Gemini API key not set"})
        return

    _patch_meta(call_dir, {"enhanced_status": "running"})
    try:
        wav_bytes = mixed_path.read_bytes()
        client = genai.Client(api_key=state.gemini_api_key)
        audio_part = types.Part.from_bytes(
            data=wav_bytes, mime_type="audio/wav",
        )
        prompt = (
            "This is a phone-call recording between a Saudi-based call "
            "center agent named Noura (نورة, agent — usually Arabic with "
            "a Syrian / Levantine flavour, sometimes switches to English) "
            "and a caller (المتصل). Produce an accurate, faithful "
            "transcript as a JSON array of turns in the order spoken. "
            "Each turn has:\n"
            "  - role: \"agent\" or \"caller\"\n"
            "  - text: what was actually said, preserving the original "
            "language (Arabic stays Arabic, English stays English). Do "
            "NOT translate. Do NOT paraphrase. Do NOT add commentary.\n"
            "Merge consecutive utterances from the same speaker. Skip "
            "silence, breathing, and DTMF tones. If a section is "
            "unintelligible, write [unintelligible] for that turn's text."
        )
        schema = types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "role": types.Schema(
                        type=types.Type.STRING,
                        enum=["agent", "caller"],
                    ),
                    "text": types.Schema(type=types.Type.STRING),
                },
                required=["role", "text"],
            ),
        )
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        )

        last_err: Optional[Exception] = None
        text: Optional[str] = None
        for model in _ENHANCE_MODELS:
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=[audio_part, prompt],
                    config=cfg,
                )
                text = getattr(resp, "text", None)
                if text:
                    break
            except Exception as e:
                last_err = e
                logger.warning("primewave enhance %s: model %s failed: %s",
                               call_dir_id, model, e)
                continue
        if not text:
            raise last_err or RuntimeError("no model returned text")

        turns = json.loads(text)
        if not isinstance(turns, list):
            raise ValueError("model output is not a JSON array")
        clean: list[dict] = []
        for t in turns:
            if not isinstance(t, dict): continue
            role = str(t.get("role") or "").strip().lower()
            body = str(t.get("text") or "").strip()
            if role not in ("agent", "caller") or not body:
                continue
            clean.append({"role": role, "text": body})

        _patch_meta(call_dir, {
            "enhanced_turns":  clean,
            "enhanced_status": "done",
            "enhanced_error":  None,
        })
        logger.info("primewave enhance %s: stored %d turns",
                    call_dir_id, len(clean))
    except Exception as e:
        logger.exception("primewave enhance %s failed", call_dir_id)
        _patch_meta(call_dir, {
            "enhanced_status": "failed",
            "enhanced_error":  f"{type(e).__name__}: {e}",
        })


def _build_system_instruction() -> str:
    """Assemble persona + KB + the current wall-clock so the agent
    never invents a day."""
    parts = [load_persona().strip(), "", "## Knowledge base", load_kb().strip()]
    parts += [
        "",
        "## Current time (system clock — never invent a different day)",
        time.strftime("%A %Y-%m-%d %H:%M %Z"),
    ]
    return "\n".join(parts)


# ============================================================================
# Per-call session
# ============================================================================

class CallSession:
    def __init__(self, call_id: str, peer: str,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 svc: "PrimewaveLiveAgentService") -> None:
        self.call_id = call_id
        self.peer    = peer
        self.reader  = reader
        self.writer  = writer
        self.svc     = svc
        self.uuid: Optional[str] = None
        self.started_at = time.time()
        # Caller → Gemini (16 kHz mono PCM after upsample)
        self.audio_in:  asyncio.Queue = asyncio.Queue(maxsize=200)
        # Gemini → Caller (24 kHz mono PCM, downsampled in write loop)
        self.audio_out: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.stop_evt = asyncio.Event()
        self._upstate = None
        self._downstate = None
        self._out_leftover: bytes = b""
        self._next_send_at: Optional[float] = None
        # Half-duplex echo gate — drop caller audio until 350ms past the
        # end of the agent's last outgoing frame (prevents the agent's
        # own voice from re-entering Gemini's VAD via speakerphone).
        self.echo_until: float = 0.0
        # Transcripts (for journal logs only; no persistence yet).
        self.heard_text = ""
        self.spoken_text = ""
        # Customer-info fields the agent fills in via the
        # `record_customer_info` tool. Live-updated by the receive loop
        # so /agent/status streams the in-progress version to the SPA;
        # persisted to disk in _save_call() when the call ends.
        self.customer_data: dict[str, str] = {}
        # Per-call recording buffers — same shape as clinic. Caller
        # frames arrive continuously over AudioSocket (one every 20ms)
        # so concatenating them yields a perfect wall-clock timeline.
        # Agent chunks are intermittent (Gemini only emits while it's
        # speaking) — tag each chunk with seconds-since-call-start so
        # the mixer can overlay it at the right offset.
        self._caller_pcm8k: list[bytes] = []
        self._agent_pcm8k:  list[tuple[float, bytes]] = []
        # Transcript turns — [{role: "caller"|"agent", text, ts}].
        self.turns: list[dict] = []
        # Caller phone — populated by the service when the AudioSocket
        # UUID matches a pre-call hint from /agent/call-init. Empty when
        # the dialplan didn't pre-notify (older config; still works,
        # we just lose the returning-caller personalisation).
        self.caller_phone: str = ""
        # Supervisor flag — non-None when the agent (or a manual operator
        # click) has raised this call for a human to take over. Snapshots
        # in active_calls() include it so a Dashboard that connects
        # after the flag was raised still sees the red row.
        self.active_flag: Optional[dict] = None

    def set_flag(self, flag: dict) -> None:
        """Mark this call as needing supervisor attention. Mutated by
        the agent's `flag_for_supervisor` tool and persisted on the
        session so re-fetches of /agent/status keep showing it."""
        self.active_flag = {
            "reason":   str(flag.get("reason") or "").strip() or "(no reason given)",
            "severity": (flag.get("severity") or "normal").strip().lower(),
            "source":   flag.get("source") or "agent",
            "ts":       time.time(),
        }
        logger.info("primewave call %s: FLAG raised — %s (%s)",
                    self.call_id, self.active_flag["reason"],
                    self.active_flag["severity"])

    def ack_flag(self) -> None:
        if self.active_flag:
            logger.info("primewave call %s: flag acknowledged", self.call_id)
        self.active_flag = None

    def _append_turn(self, role: str, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        # Coalesce consecutive same-role chunks so the persisted
        # transcript reads as continuous speech rather than a stutter
        # of token-sized fragments.
        if self.turns and self.turns[-1].get("role") == role:
            self.turns[-1]["text"] = (self.turns[-1]["text"] + " " + t).strip()
            self.turns[-1]["ts"] = time.time()
        else:
            self.turns.append({"role": role, "text": t, "ts": time.time()})

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
        header = await reader.readexactly(3)
        msg_type = header[0]
        length   = struct.unpack(">H", header[1:3])[0]
        payload  = await reader.readexactly(length) if length else b""
        return msg_type, payload

    async def _send_audio(self, pcm8k: bytes) -> None:
        FRAME = 320  # 20 ms @ 8 kHz, 16-bit mono
        n_frames = len(pcm8k) // FRAME
        if n_frames == 0:
            return
        self.echo_until = max(
            self.echo_until, time.time() + n_frames * 0.020 + 0.35,
        )
        now = time.monotonic()
        if self._next_send_at is None or self._next_send_at < now - 0.05:
            self._next_send_at = now
        for i in range(0, len(pcm8k), FRAME):
            chunk = pcm8k[i:i + FRAME]
            self.writer.write(bytes([_AS_AUDIO]) + struct.pack(">H", len(chunk)) + chunk)
            await self.writer.drain()
            self._next_send_at += 0.020
            delay = self._next_send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def _hangup(self) -> None:
        try:
            self.writer.write(bytes([_AS_HANGUP, 0, 0]))
            await self.writer.drain()
        except Exception:
            pass

    # ---- pumps -------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self.stop_evt.is_set():
                msg_type, payload = await self._read_frame(self.reader)
                if msg_type == _AS_HANGUP:
                    logger.info("primewave call %s: peer hangup", self.call_id)
                    self.stop_evt.set(); return
                if msg_type == _AS_UUID:
                    self.uuid = payload.hex()
                    # Convert hex digits → canonical 8-4-4-4-12 UUID
                    # string so the lookup key matches whatever the PBX
                    # dialplan POSTed to /agent/call-init.
                    if len(self.uuid) == 32:
                        canonical = (
                            f"{self.uuid[0:8]}-{self.uuid[8:12]}-"
                            f"{self.uuid[12:16]}-{self.uuid[16:20]}-"
                            f"{self.uuid[20:32]}"
                        )
                    else:
                        canonical = self.uuid
                    phone = self.svc.consume_uuid_hint(canonical)
                    if phone:
                        self.caller_phone = phone
                        logger.info("primewave call %s: caller_phone=%s "
                                    "(from /agent/call-init hint)",
                                    self.call_id, phone)
                    continue
                if msg_type == _AS_DTMF:
                    digit = payload.decode("ascii", "replace") if payload else ""
                    logger.info("primewave call %s: DTMF %s", self.call_id, digit)
                    continue
                if msg_type == _AS_ERROR:
                    logger.warning("primewave call %s: peer error: %r",
                                   self.call_id, payload)
                    continue
                if msg_type == _AS_AUDIO and payload:
                    # Capture the raw 8 kHz caller frame BEFORE any
                    # echo-gate / resample — recording stays lossless.
                    self._caller_pcm8k.append(payload)
                    # Echo gate (half-duplex): suppresses caller mic while
                    # the agent is talking so the agent's audio echoing
                    # back through the phone speaker doesn't loop. BUT —
                    # the gate also blocks barge-in: when it's closed,
                    # Gemini never hears the caller's voice and AAD can't
                    # fire. So skip the gate entirely when the operator
                    # has interruption enabled, leaning on Gemini Live's
                    # own VAD / echo cancellation to handle overlap.
                    if (not state.pwa_interruption_enabled
                            and time.time() < self.echo_until):
                        continue
                    pcm16k, self._upstate = audioop.ratecv(
                        payload, _SAMPLE_WIDTH, 1, 8000, 16000, self._upstate,
                    )
                    try:
                        self.audio_in.put_nowait(pcm16k)
                    except asyncio.QueueFull:
                        try: self.audio_in.get_nowait()
                        except Exception: pass
                        try: self.audio_in.put_nowait(pcm16k)
                        except Exception: pass
        except asyncio.IncompleteReadError as e:
            logger.info("primewave call %s: TCP closed by peer "
                        "(IncompleteRead: %d/%d bytes)",
                        self.call_id, len(e.partial), e.expected)
        except ConnectionResetError:
            logger.info("primewave call %s: TCP reset by peer (PBX hung up)",
                        self.call_id)
        except Exception:
            logger.exception("primewave call %s: read loop crashed", self.call_id)
        finally:
            self.stop_evt.set()

    async def _write_loop(self) -> None:
        # CRITICAL: Asterisk's app_audiosocket has a hardcoded ~2-second
        # "no activity" timeout. If we stop sending audio frames for >2s,
        # Asterisk tears down the call with
        #   "Reached timeout after 2000 ms of no activity on AudioSocket
        #    connection".
        # This bites when Gemini Live throttles audio output (e.g.
        # concurrent sessions on a preview model demoting the older
        # session). The fix: tick at 20ms cadence and send EITHER real
        # agent audio OR silence — never let the socket go idle.
        #
        # IMPORTANT: only REAL audio extends the echo gate (self.echo_until).
        # The gate suppresses caller-mic frames so the agent doesn't hear
        # itself looping back. If silence frames bumped the gate every
        # 20ms, the caller's mic would be permanently muted and Gemini
        # would never hear them respond after the greeting.
        FRAME    = 320           # 20ms @ 8 kHz, 16-bit mono signed
        SILENCE  = bytes(FRAME)  # all zeros = silence
        INTERVAL = 0.02          # 20ms tick
        loop     = asyncio.get_event_loop()
        next_tick = loop.time()
        try:
            while not self.stop_evt.is_set():
                # 1. Drain any agent audio Gemini has handed us since the
                #    last tick. Non-blocking — if there's nothing, fall
                #    through and emit silence.
                while True:
                    try:
                        pcm24k = self.audio_out.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if not pcm24k:
                        continue
                    pcm8k, self._downstate = audioop.ratecv(
                        pcm24k, _SAMPLE_WIDTH, 1, 24000, 8000, self._downstate,
                    )
                    if pcm8k:
                        self._out_leftover += pcm8k

                # 2. Emit exactly one 20ms frame this tick — real audio
                #    if buffered, otherwise silence (keeps AudioSocket
                #    active so Asterisk doesn't fire its 2s timeout).
                if len(self._out_leftover) >= FRAME:
                    chunk = self._out_leftover[:FRAME]
                    self._out_leftover = self._out_leftover[FRAME:]
                    # Real agent audio → extend the echo gate by one
                    # frame + 350ms margin so the caller's mic stays
                    # muted while this frame plays out at the far end.
                    self.echo_until = max(
                        self.echo_until, time.time() + 0.020 + 0.35,
                    )
                    offset_s = time.time() - self.started_at
                    self._agent_pcm8k.append((offset_s, chunk))
                else:
                    chunk = SILENCE  # NO echo-gate extension

                # 3. Write the frame directly to the AudioSocket TCP —
                #    we're already paced at 20ms by the outer tick loop,
                #    so we skip _send_audio's internal pacing/echo logic.
                try:
                    self.writer.write(
                        bytes([_AS_AUDIO])
                        + struct.pack(">H", len(chunk))
                        + chunk
                    )
                    await self.writer.drain()
                except Exception as e:
                    logger.warning("primewave call %s: writer.drain failed: %s",
                                   self.call_id, e)
                    break

                # 4. Sleep until the next 20ms boundary. If we fell
                #    behind, reset the clock instead of burning a tight
                #    catch-up loop.
                next_tick += INTERVAL
                sleep_for = next_tick - loop.time()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                elif sleep_for < -0.1:
                    next_tick = loop.time()
        except Exception as e:
            logger.warning("primewave call %s: write loop ended: %s", self.call_id, e)
        finally:
            self.stop_evt.set()

    async def _gemini_loop(self) -> None:
        if not state.gemini_api_key:
            logger.error("primewave call %s: Gemini API key not set", self.call_id)
            return
        model = state.gemini_model
        client = genai.Client(
            api_key=state.gemini_api_key,
            http_options={"api_version": _GEMINI_API_VERSION},
        )

        # Wait briefly so the UUID frame (with its caller-phone hint)
        # has time to land before we freeze the system instruction.
        # AudioSocket typically sends UUID within the first ~50ms.
        for _ in range(20):
            if self.uuid: break
            await asyncio.sleep(0.025)

        # Build system instruction. If we know the caller's phone (from
        # the dialplan's /agent/call-init pre-notify), enrich it with
        # the returning-caller block — contact match + past WhatsApp +
        # past voice calls. Empty string if the caller is unknown.
        system_text = _build_system_instruction()
        if self.caller_phone:
            try:
                from . import caller_context as _ctx
                ret = _ctx.format_for_prompt(
                    _ctx.lookup(self.caller_phone), channel="voice",
                )
                if ret:
                    system_text = system_text + ret
                    logger.info("primewave call %s: injected returning-caller context (phone=%s)",
                                self.call_id, self.caller_phone)
            except Exception:
                logger.exception("primewave call %s: returning-caller context failed",
                                 self.call_id)

        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=system_text)],
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=state.pwa_voice or "Aoede",
                    ),
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=not bool(state.pwa_interruption_enabled),
                    # HIGH sensitivity → barge-in fires as soon as the
                    # caller starts speaking, not after a clear voice
                    # buildup. Pairs with the echo-gate bypass in
                    # _read_loop when interruption is enabled.
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=600,
                    prefix_padding_ms=200,
                ),
            ),
            tools=_build_tools(),
        )

        try:
            async with client.aio.live.connect(model=model, config=cfg) as session:
                logger.info("primewave call %s: Gemini Live connected (model=%s)",
                            self.call_id, model)

                if state.pwa_greeting:
                    try:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[
                                types.Part(text=f"(system) Greet the caller now with: {state.pwa_greeting}")
                            ]),
                            turn_complete=True,
                        )
                    except Exception as e:
                        logger.warning("primewave call %s: greeting failed: %s",
                                       self.call_id, e)

                async def feed():
                    try:
                        while not self.stop_evt.is_set():
                            try:
                                chunk = await asyncio.wait_for(self.audio_in.get(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if not chunk:
                                continue
                            await session.send_realtime_input(
                                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.info("primewave call %s: feed loop ended: %r",
                                    self.call_id, e)
                    finally:
                        self.stop_evt.set()

                async def receive():
                    try:
                        while not self.stop_evt.is_set():
                            async for resp in session.receive():
                                data_bytes = getattr(resp, "data", None)
                                if data_bytes:
                                    try: self.audio_out.put_nowait(data_bytes)
                                    except asyncio.QueueFull:
                                        try: self.audio_out.get_nowait()
                                        except Exception: pass
                                        try: self.audio_out.put_nowait(data_bytes)
                                        except Exception: pass
                                sc = getattr(resp, "server_content", None)
                                if sc:
                                    if getattr(sc, "interrupted", False):
                                        drained = 0
                                        while not self.audio_out.empty():
                                            try:
                                                self.audio_out.get_nowait(); drained += 1
                                            except Exception: break
                                        self._out_leftover = b""
                                        self.echo_until = 0.0
                                        if drained:
                                            logger.info("primewave call %s: interrupted (%d frames dropped)",
                                                        self.call_id, drained)
                                    it = getattr(sc, "input_transcription", None)
                                    if it and getattr(it, "text", None):
                                        self.heard_text += it.text
                                        self._append_turn("caller", it.text)
                                    ot = getattr(sc, "output_transcription", None)
                                    if ot and getattr(ot, "text", None):
                                        self.spoken_text += ot.text
                                        self._append_turn("agent", ot.text)
                                # Tool call → execute → ack a FunctionResponse.
                                tc = getattr(resp, "tool_call", None)
                                if tc:
                                    responses = []
                                    for fc in (tc.function_calls or []):
                                        args = dict(fc.args or {})
                                        if fc.name == "record_customer_info":
                                            result = _apply_customer_info(self, args)
                                        elif fc.name == "flag_for_supervisor":
                                            self.set_flag({**args, "source": "agent"})
                                            result = {"ok": True,
                                                      "flag": dict(self.active_flag or {})}
                                        else:
                                            logger.warning("primewave call %s: unknown tool %s",
                                                           self.call_id, fc.name)
                                            result = {"error": f"unknown tool {fc.name}"}
                                        responses.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={"result": result},
                                        ))
                                    if responses:
                                        try:
                                            await session.send_tool_response(
                                                function_responses=responses)
                                        except Exception as e:
                                            logger.warning("primewave call %s: tool response send failed: %s",
                                                           self.call_id, e)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.info("primewave call %s: receive loop ended: %r",
                                    self.call_id, e)
                    finally:
                        self.stop_evt.set()

                await asyncio.gather(feed(), receive(), return_exceptions=True)
                logger.info("primewave call %s: Gemini Live session exited cleanly",
                            self.call_id)
        except Exception:
            logger.exception("primewave call %s: Gemini Live failed", self.call_id)
        finally:
            # Mark whether Gemini side closed first vs. PBX TCP — useful for
            # diagnosing whether the close was Live-API-initiated or PBX-initiated.
            if not self.stop_evt.is_set():
                logger.info("primewave call %s: _gemini_loop exiting, stop_evt not set yet",
                            self.call_id)
            else:
                logger.info("primewave call %s: _gemini_loop exit (stop_evt already set)",
                            self.call_id)
            self.stop_evt.set()

    async def run(self) -> None:
        max_s = max(60, int(state.pwa_max_call_s or 900))
        async def deadline():
            await asyncio.sleep(max_s)
            logger.info("primewave call %s: hit max duration %ds",
                        self.call_id, max_s)
            self.stop_evt.set()
        tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._write_loop()),
            asyncio.create_task(self._gemini_loop()),
            asyncio.create_task(deadline()),
        ]
        try:
            await self.stop_evt.wait()
        finally:
            for t in tasks:
                t.cancel()
            await self._hangup()


# ============================================================================
# Service — TCP listener + per-call dispatch
# ============================================================================

class PrimewaveLiveAgentService:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._calls: dict[str, CallSession] = {}
        self.bound_at: Optional[float] = None
        self.last_error: Optional[str] = None
        # UUID → {caller_phone, ts} short-lived cache populated by the
        # /agent/call-init endpoint (POSTed from the PBX dialplan just
        # before the AudioSocket connects). Entries older than 60s are
        # purged so a stale hint can't bleed into the next call that
        # reuses the same UUID.
        self._uuid_hints: dict[str, dict] = {}

    def record_call_init(self, audiosocket_uuid: str, caller_phone: str) -> None:
        """Called by the /agent/call-init endpoint. The AudioSocket
        connection that arrives next with this UUID will pick up the
        caller phone here."""
        if not audiosocket_uuid:
            return
        # Purge old hints — pre-call notifications older than 60s are
        # almost certainly stale (the call never connected).
        now = time.time()
        for k in list(self._uuid_hints.keys()):
            if now - (self._uuid_hints[k].get("ts") or 0) > 60:
                self._uuid_hints.pop(k, None)
        self._uuid_hints[audiosocket_uuid.lower()] = {
            "caller_phone": "".join(c for c in (caller_phone or "") if c.isdigit()),
            "ts":           now,
        }
        logger.info("primewave call-init: uuid=%s caller_phone=%s",
                    audiosocket_uuid, caller_phone)

    def consume_uuid_hint(self, audiosocket_uuid: str) -> str:
        """Look up + pop the caller_phone for an incoming AudioSocket
        UUID. Returns "" if no hint is on file."""
        if not audiosocket_uuid:
            return ""
        hint = self._uuid_hints.pop(audiosocket_uuid.lower(), None)
        if not hint:
            return ""
        return hint.get("caller_phone") or ""

    def _resolved_bind(self) -> tuple[bool, str, int]:
        return (bool(state.pwa_enabled),
                state.pwa_bind_host or "0.0.0.0",
                int(state.pwa_bind_port or 8094))

    def apply_config(self) -> None:
        enabled, host, port = self._resolved_bind()
        if enabled and self._server is None:
            self.start()
        elif (not enabled) and self._server is not None:
            asyncio.create_task(self.stop())
        elif self._server is not None:
            sock = next(iter(self._server.sockets or []), None)
            if sock:
                cur_host, cur_port = sock.getsockname()[:2]
                if cur_port != port or cur_host != host:
                    asyncio.create_task(self._restart())

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="primewave-live-agent")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try: await self._server.wait_closed()
            except Exception: pass
            self._server = None
            self.bound_at = None
        for c in list(self._calls.values()):
            c.stop_evt.set()
        self._calls.clear()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _restart(self) -> None:
        await self.stop()
        await asyncio.sleep(0.1)
        self.start()

    async def _run(self) -> None:
        _enabled, host, port = self._resolved_bind()
        try:
            self._server = await asyncio.start_server(self._handle, host, port)
            self.bound_at = time.time()
            self.last_error = None
            logger.info("PrimewaveLiveAgent listening on %s:%d", host, port)
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.exception("PrimewaveLiveAgent bind/run failed")

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_label = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        call_id = uuid.uuid4().hex[:10]
        session = CallSession(call_id, peer_label, reader, writer, self)
        self._calls[call_id] = session
        # Log alongside the count BEFORE accept — if we already had 1 active
        # call when this one came in, the count goes 1→2 and we can see that
        # both ran simultaneously.
        logger.info("PrimewaveLiveAgent: new call %s from %s "
                    "(now %d active: %s)",
                    call_id, peer_label, len(self._calls),
                    ",".join(self._calls.keys()))
        try:
            await session.run()
        except Exception:
            logger.exception("primewave call %s crashed", call_id)
        finally:
            self._calls.pop(call_id, None)
            duration = time.time() - session.started_at if session.started_at else 0
            logger.info("primewave call %s ended after %.1fs "
                        "(%d still active: %s)",
                        call_id, duration, len(self._calls),
                        ",".join(self._calls.keys()) or "none")
            # Persist collected data + audio + transcript for the
            # Call History / Data Collected pages. Best-effort.
            saved_id = _save_call(session)
            # Kick the offline enhancement pass in the background — it
            # re-transcribes the mixed WAV with a non-Live Gemini model
            # and writes `enhanced_turns` back to meta.json. Skipped if
            # _save_call decided the call was empty (returned None).
            if saved_id:
                asyncio.create_task(_enhance_transcript(saved_id))
            try: writer.close()
            except Exception: pass

    def active_calls(self) -> list[dict]:
        out = []
        for c in self._calls.values():
            out.append({
                "call_id":       c.call_id,
                "peer":          c.peer,
                "uuid":          c.uuid,
                "started_at":    c.started_at,
                "customer_data": dict(c.customer_data),
                "caller_phone":  c.caller_phone or "",
                "active_flag":   dict(c.active_flag) if c.active_flag else None,
            })
        return out

    def get_call(self, call_id: str) -> Optional[CallSession]:
        """Lookup an in-flight call by call_id — used by the engage /
        ack supervisor endpoints in router.py."""
        return self._calls.get(call_id)

    def status(self) -> dict:
        enabled, host, port = self._resolved_bind()
        return {
            "enabled":     enabled,
            "bind_host":   host,
            "bind_port":   port,
            "bound":       self._server is not None,
            "bound_at":    self.bound_at,
            "last_error":  self.last_error,
            "active":      len(self._calls),
            "calls":       self.active_calls(),
        }


primewave_live_agent_service = PrimewaveLiveAgentService()
