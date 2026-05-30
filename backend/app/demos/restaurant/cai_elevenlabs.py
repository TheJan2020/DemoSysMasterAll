"""
ElevenLabs Conversational AI (CAI) bridge — end-to-end voice path that
swaps Gemini Live entirely for ElevenLabs's bundled STT + LLM + TTS
stack on a per-call basis.

Two responsibilities:

  1. AGENT LIFECYCLE — `ensure_agent()` is called on every relevant
     state change (persona / KB saved, voice_id changed, tools changed)
     plus on backend startup. It computes a hash of the desired agent
     config, compares to what's persisted in state, and either:
       - creates a new agent (POST /v1/convai/agents/create) if no
         `elevenlabs_agent_id` is stored yet
       - patches the existing agent (PATCH /v1/convai/agents/{id})
         if the hash drifted
       - no-ops if the hash matches
     The result is that the operator never has to touch ElevenLabs's
     dashboard — they edit persona/KB in the SPA, hit Save, and the
     agent updates silently on the next ensure_agent() call.

  2. PER-CALL WS SESSION — `Session` opens
     wss://api.elevenlabs.io/v1/convai/conversation?agent_id=<id>,
     pumps caller PCM frames in, and pumps agent PCM frames + tool
     calls out. Tool calls dispatch through the SAME execute_tool() the
     Gemini path uses, so every restaurant tool keeps working
     unchanged.

Audio format note: ElevenLabs CAI accepts PCM 16k for user input and
returns PCM 16k for agent output by default. The live_agent's existing
8k↔16k resampler (caller side) reuses cleanly. For the agent output
the existing 24k→8k downsampler doesn't apply directly — we resample
16k→8k here before pushing onto the AudioSocket queue.
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import hashlib
import json
import logging
from typing import Any, Callable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from ...core.state import state
from .agent_tools import build_tools, execute_tool

logger = logging.getLogger("demo_restaurant.cai_elevenlabs")

_BASE_REST = "https://api.elevenlabs.io"
_BASE_WS   = "wss://api.elevenlabs.io"

# ElevenLabs CAI defaults — pinned so the agent config we PATCH is
# deterministic. The operator changes the *voice* via the Settings
# voice_id field; everything else is wired by us.
_DEFAULT_LLM         = "gemini-2.5-flash"   # CAI supports Gemini as its LLM
_DEFAULT_TEMPERATURE = 0.5
_DEFAULT_LANGUAGE    = "ar"

# Audio format ElevenLabs sends agent voice in. We use `ulaw_8000` —
# the actual telephony codec — because earlier attempts with PCM
# (pcm_16000 then pcm_8000) hit a chipmunk effect: ElevenLabs's
# advertised PCM rate didn't reliably match the rate the agent
# synthesised at, so our downsampler over-/under-corrected. µ-law
# 8 kHz is unambiguous (the bytes ARE 8000 per second of audio) and
# we decode in one line with audioop.ulaw2lin → PCM-8k 16-bit which is
# exactly what AudioSocket wants. Also avoids any resample step.
_AGENT_FORMAT   = "ulaw_8000"


# ============================================================================
# Tool schema translator — our google.genai FunctionDeclaration objects
# → ElevenLabs CAI client_tool dicts.
# ============================================================================

# Gemini types.Type enum values map directly to JSON Schema strings.
_GEMINI_TYPE_TO_JSON = {
    "STRING":  "string",
    "INTEGER": "integer",
    "NUMBER":  "number",
    "BOOLEAN": "boolean",
    "OBJECT":  "object",
    "ARRAY":   "array",
}


def _schema_to_jsonschema(schema: Any, *, fallback_desc: str = "") -> dict:
    """Convert a google.genai types.Schema (or dict) into a plain
    JSON Schema dict ElevenLabs CAI accepts. Handles nested OBJECT and
    ARRAY recursively.

    CAI requires every property to carry a `description` — without one
    the create endpoint 422s with "Must set one of: description,
    dynamic_variable, is_system_provided, or constant_value". Many of
    our Gemini FunctionDeclarations omit descriptions on obvious
    fields like `guest_name`; we synthesise a fallback from the
    property name in that case so the schema always validates."""
    if schema is None:
        return {"type": "object", "properties": {},
                "description": fallback_desc or "(no fields)"}
    def g(o: Any, k: str, default=None):
        if hasattr(o, k):
            return getattr(o, k)
        if isinstance(o, dict):
            return o.get(k, default)
        return default
    raw_type = g(schema, "type")
    type_str = (str(raw_type).split(".")[-1] if raw_type else "OBJECT").upper()
    js: dict = {"type": _GEMINI_TYPE_TO_JSON.get(type_str, "string")}
    desc = g(schema, "description") or fallback_desc
    # Always emit a description, even if it's just the field name —
    # CAI's validator refuses properties without one.
    js["description"] = str(desc or "Parameter")
    if js["type"] == "object":
        props = g(schema, "properties") or {}
        js_props: dict = {}
        for name, child in props.items():
            # Use the property name as a fallback description so each
            # nested property satisfies CAI's per-field requirement.
            js_props[name] = _schema_to_jsonschema(
                child, fallback_desc=str(name).replace("_", " "),
            )
        js["properties"] = js_props
        req = g(schema, "required")
        if req:
            js["required"] = list(req)
    elif js["type"] == "array":
        items = g(schema, "items")
        if items is not None:
            js["items"] = _schema_to_jsonschema(items, fallback_desc="item")
    return js


def _our_tools_for_cai() -> list[dict]:
    """Take the FunctionDeclarations from agent_tools.build_tools() and
    re-shape them into ElevenLabs CAI's `client_tools` payload. CAI tools
    are dispatched back to the client (us) during a conversation — the
    LLM sees them in its prompt, decides when to call one, and we run
    the actual function via execute_tool()."""
    out: list[dict] = []
    for tool in build_tools():
        for fn in (getattr(tool, "function_declarations", None) or []):
            name = getattr(fn, "name", None)
            if not name:
                continue
            out.append({
                "type":        "client",
                "name":        name,
                "description": getattr(fn, "description", "") or "",
                "parameters":  _schema_to_jsonschema(getattr(fn, "parameters", None)),
                "expects_response": True,
            })
    return out


# ============================================================================
# Agent lifecycle — create / update / hash
# ============================================================================

def _build_agent_config(*, persona: str, kb: str, voice_id: str,
                         model_id: str, first_message: str) -> dict:
    """The canonical body we POST/PATCH to ElevenLabs. Pinning every
    field here means `ensure_agent()` can compute a stable hash and
    detect drift cheaply."""
    return {
        "name": "PW Demo — Restaurant (Mate)",
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt":      persona,
                    "llm":         _DEFAULT_LLM,
                    "temperature": _DEFAULT_TEMPERATURE,
                    "tools":       _our_tools_for_cai(),
                },
                "first_message": first_message or "",
                "language":      _DEFAULT_LANGUAGE,
            },
            "asr": {
                "quality": "high",
                "user_input_audio_format": "pcm_16000",
            },
            "tts": {
                "voice_id":                  voice_id,
                "model_id":                  model_id,
                "agent_output_audio_format": _AGENT_FORMAT,
            },
        },
        "platform_settings": {
            "knowledge_base": (
                [{"name": "Prime Mate Restaurant — Knowledge Base",
                  "type": "text", "text": kb}]
                if kb.strip() else []
            ),
        },
    }


def _config_hash(cfg: dict) -> str:
    """Stable hash of the agent config so we only PATCH on real drift."""
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# Stored hash so we don't PATCH every call. Kept in-memory for the
# process lifetime; on backend restart we re-derive on first ensure().
_last_hash: dict[str, str] = {}


async def ensure_agent(*, persona: str, kb: str, voice_id: str,
                         model_id: str, first_message: str = "") -> Optional[str]:
    """Idempotent: returns the agent_id on success, None if not
    configured (no API key). Creates the agent on first call, PATCHes
    it on every subsequent call where the hash drifted, no-ops
    otherwise. Caller is responsible for persisting the returned
    agent_id to state if it changed."""
    api_key = (state.elevenlabs_api_key or "").strip()
    if not api_key:
        return None

    cfg = _build_agent_config(
        persona=persona, kb=kb, voice_id=voice_id, model_id=model_id,
        first_message=first_message,
    )
    h = _config_hash(cfg)
    agent_id = (state.elevenlabs_agent_id or "").strip()

    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if not agent_id:
                logger.info("CAI ensure_agent: creating new agent")
                r = await client.post(
                    f"{_BASE_REST}/v1/convai/agents/create",
                    headers=headers, json=cfg,
                )
                if r.status_code >= 400:
                    body = (r.text or "")[:600]
                    msg  = f"CAI agent create HTTP {r.status_code}: {body}"
                    logger.warning(msg)
                    # Raise so the caller can surface the actual reason
                    # (quota exceeded, validation error, account doesn't
                    # have Conversational AI enabled, etc.) to the UI.
                    raise RuntimeError(msg)
                resp = r.json() if r.content else {}
                agent_id = resp.get("agent_id") or resp.get("id") or ""
                if not agent_id:
                    msg = f"CAI agent create: no id in response body={str(resp)[:600]}"
                    logger.warning(msg)
                    raise RuntimeError(msg)
                state.elevenlabs_agent_id = agent_id
                state.save()
                _last_hash[agent_id] = h
                logger.info("CAI agent created id=%s", agent_id)
                return agent_id
            # Drift check + patch
            if _last_hash.get(agent_id) == h:
                return agent_id
            logger.info("CAI ensure_agent: patching %s (hash drift)", agent_id)
            r = await client.patch(
                f"{_BASE_REST}/v1/convai/agents/{agent_id}",
                headers=headers, json=cfg,
            )
            if r.status_code >= 400:
                logger.warning("CAI agent patch failed status=%s body=%s",
                                r.status_code, (r.text or "")[:600])
                # PATCH failure is non-fatal — the old agent still works
                return agent_id
            _last_hash[agent_id] = h
            return agent_id
    except httpx.HTTPError as e:
        logger.warning("CAI ensure_agent HTTP failed: %s", e)
        raise RuntimeError(f"CAI HTTP error: {e}") from e


def is_active() -> bool:
    """Cheap predicate: is the CAI voice path the operator's currently
    selected provider AND do we have everything we need?"""
    return (
        state.voice_provider == "elevenlabs_cai"
        and bool((state.elevenlabs_api_key or "").strip())
    )


# ============================================================================
# Per-call WS session
# ============================================================================

class Session:
    """One ElevenLabs CAI WebSocket per phone call. The live_agent
    spawns this when `voice_provider == elevenlabs_cai` is set; the
    CallSession.audio_in / audio_out queues become the bridge between
    AudioSocket and the CAI WS."""

    def __init__(self, *, call_id: str, agent_id: str,
                 audio_in_q: asyncio.Queue,    # caller PCM (16k bytes)
                 audio_out_q: asyncio.Queue,   # agent PCM-8k bytes for AudioSocket (fallback)
                 on_agent_audio_8k: Optional[Callable[[bytes], Any]] = None,
                 on_caller_text: Optional[Callable[[str], None]] = None,
                 on_agent_text:  Optional[Callable[[str], None]] = None,
                 ctx: Optional[dict] = None) -> None:
        self.call_id = call_id
        self.agent_id = agent_id
        self.audio_in_q  = audio_in_q
        self.audio_out_q = audio_out_q
        # Direct-write callback for agent PCM-8k frames. When set, we
        # bypass `audio_out_q` entirely and call this with each decoded
        # 8 kHz chunk. The live_agent wires this to `CallSession._send_audio`
        # which chunks straight into AudioSocket — no resample roundtrip,
        # no wasted CPU, no audible smearing from the 8→24→8 detour the
        # queue path required to coexist with Gemini's 24 kHz pipeline.
        self._on_agent_audio_8k = on_agent_audio_8k
        self._on_caller_text = on_caller_text
        self._on_agent_text  = on_agent_text
        self._ctx = ctx or {}
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._closed = False
        self._downstate: Any = None   # audioop ratecv state for fallback path
        self._send_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None

    # ----- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        api_key = (state.elevenlabs_api_key or "").strip()
        if not api_key:
            raise RuntimeError("elevenlabs_api_key not set")
        url = f"{_BASE_WS}/v1/convai/conversation?agent_id={self.agent_id}"
        logger.info("CAI session connecting call=%s agent=%s", self.call_id, self.agent_id)
        self._ws = await websockets.connect(
            url,
            additional_headers={"xi-api-key": api_key},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        # Initial config envelope. Some CAI deployments auto-init on
        # WS connect; sending an explicit init is harmless and forces
        # the session to start in a known state.
        try:
            await self._ws.send(json.dumps({
                "type": "conversation_initiation_client_data",
                "conversation_config_override": {},
            }))
        except Exception:
            # Older agents don't require this — just continue.
            pass
        self._send_task = asyncio.create_task(
            self._send_loop(), name=f"cai-snd-{self.call_id}",
        )
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name=f"cai-rcv-{self.call_id}",
        )
        logger.info("CAI session opened call=%s", self.call_id)

    async def close(self) -> None:
        self._closed = True
        for t in (self._send_task, self._recv_task):
            if t is not None and not t.done():
                t.cancel()
        if self._ws is not None:
            try: await self._ws.close()
            except Exception: pass
            self._ws = None

    # ----- send-side: caller audio → ElevenLabs ------------------------

    async def _send_loop(self) -> None:
        """Drain caller PCM-16k frames off audio_in_q and forward as
        base64 to CAI. ElevenLabs expects user audio inside a JSON
        envelope: {type: user_audio_chunk, user_audio_chunk: <b64>}."""
        try:
            while not self._closed and self._ws is not None:
                try:
                    chunk = await asyncio.wait_for(self.audio_in_q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    continue
                b64 = base64.b64encode(chunk).decode("ascii")
                try:
                    await self._ws.send(json.dumps({
                        "type": "user_audio_chunk",
                        "user_audio_chunk": b64,
                    }))
                except ConnectionClosed:
                    self._closed = True
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("CAI send loop ended call=%s: %r", self.call_id, e)

    # ----- recv-side: ElevenLabs → caller audio + tool calls -----------

    async def _recv_loop(self) -> None:
        """Process every JSON envelope ElevenLabs sends on the WS:
          - "audio" / "agent_audio_chunk" → resample + push to AudioSocket
          - "client_tool_call"            → run via execute_tool, reply
          - "user_transcript" / "agent_transcript" → broadcast / log
          - "ping"                        → reply pong
          - "interruption"                → drain audio_out_q
          - "conversation_end"            → close
        """
        try:
            assert self._ws is not None
            async for raw in self._ws:
                if self._closed:
                    break
                try:
                    msg = json.loads(raw) if isinstance(raw, (str, bytes)) else None
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                t = msg.get("type")

                # Audio chunks ----------------------------------------
                if t in ("audio", "agent_audio_chunk", "audio_event"):
                    ev = msg.get("audio_event") or msg.get("audio") or msg
                    b64 = (ev.get("audio_base_64") if isinstance(ev, dict) else None) \
                          or msg.get("audio_base_64") or msg.get("audio")
                    if b64 and isinstance(b64, str):
                        try:
                            raw = base64.b64decode(b64)
                        except Exception:
                            continue
                        await self._push_agent_audio(raw)
                    continue

                # Tool calls ------------------------------------------
                if t == "client_tool_call":
                    call = msg.get("client_tool_call") or {}
                    name = call.get("tool_name") or call.get("name") or ""
                    args = call.get("parameters") or call.get("arguments") or {}
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except Exception: args = {}
                    tool_call_id = call.get("tool_call_id") or call.get("id") or ""
                    logger.info("CAI tool_call call=%s name=%s id=%s",
                                 self.call_id, name, tool_call_id)
                    try:
                        result = execute_tool(name, args or {}, self._ctx)
                    except Exception as e:
                        result = {"error": f"{type(e).__name__}: {e}"}
                    try:
                        await self._ws.send(json.dumps({
                            "type":         "client_tool_result",
                            "tool_call_id": tool_call_id,
                            "result":       json.dumps(result, ensure_ascii=False),
                            "is_error":     bool(isinstance(result, dict) and result.get("error")),
                        }))
                    except ConnectionClosed:
                        self._closed = True
                        return
                    continue

                # Transcripts -----------------------------------------
                if t in ("user_transcript", "user_transcription_event"):
                    text = (msg.get("user_transcription_event") or msg).get("user_transcript") \
                           or msg.get("text") or ""
                    if text and self._on_caller_text:
                        try: self._on_caller_text(str(text))
                        except Exception: pass
                    continue
                if t in ("agent_response", "agent_response_event"):
                    ev = msg.get("agent_response_event") or msg
                    text = (ev.get("agent_response") if isinstance(ev, dict) else None) \
                           or msg.get("text") or ""
                    if text and self._on_agent_text:
                        try: self._on_agent_text(str(text))
                        except Exception: pass
                    continue

                # Interruption (caller barge-in) ----------------------
                if t in ("interruption", "interruption_event"):
                    self._drain_audio_out()
                    continue

                # Keep-alive ------------------------------------------
                if t == "ping":
                    ev = msg.get("ping_event") or {}
                    try:
                        await self._ws.send(json.dumps({
                            "type":     "pong",
                            "event_id": ev.get("event_id"),
                        }))
                    except Exception:
                        pass
                    continue

                # End -------------------------------------------------
                if t in ("conversation_end", "internal_vad_score_event"):
                    if t == "conversation_end":
                        self._closed = True
                        return
                    continue

                # Anything else — log once for debugging
                logger.debug("CAI unhandled msg call=%s type=%s", self.call_id, t)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("CAI recv loop crashed call=%s: %r", self.call_id, e)
        finally:
            self._closed = True

    # ----- helpers -----------------------------------------------------

    async def _push_agent_audio(self, raw: bytes) -> None:
        """Decode CAI's µ-law-8k chunk to PCM-8k 16-bit. If the
        live_agent gave us a direct AudioSocket-write callback
        (`on_agent_audio_8k`), use it — this skips the audio_out_q +
        _write_loop pipeline entirely, which means no 8→24→8 resample
        roundtrip and no audible smearing from the linear-interpolation
        resampler. Falls back to the queue path (with the round-trip
        upsample so Gemini's downsampler net-cancels) only when no
        direct callback was provided."""
        if not raw:
            return
        if _AGENT_FORMAT == "ulaw_8000":
            try:
                pcm8k = audioop.ulaw2lin(raw, 2)
            except Exception as e:
                logger.warning("call %s: ulaw2lin failed: %r", self.call_id, e)
                return
        else:
            pcm8k = raw
        if not pcm8k:
            return
        if self._on_agent_audio_8k is not None:
            # Direct path — chunk straight into AudioSocket. Best fidelity.
            try:
                res = self._on_agent_audio_8k(pcm8k)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.warning("call %s: on_agent_audio_8k failed: %r",
                                self.call_id, e)
            return
        # Fallback — queue + upsample so the legacy _write_loop's 24→8
        # downsample net-cancels and the audio plays at correct pitch.
        try:
            pcm24k, self._downstate = audioop.ratecv(
                pcm8k, 2, 1, 8000, 24000, self._downstate,
            )
        except Exception as e:
            logger.warning("call %s: 8k→24k ratecv failed: %r", self.call_id, e)
            return
        if not pcm24k:
            return
        try:
            self.audio_out_q.put_nowait(pcm24k)
        except asyncio.QueueFull:
            try: self.audio_out_q.get_nowait()
            except Exception: pass
            try: self.audio_out_q.put_nowait(pcm24k)
            except Exception: pass

    def _drain_audio_out(self) -> None:
        try:
            while True:
                self.audio_out_q.get_nowait()
        except asyncio.QueueEmpty:
            pass
