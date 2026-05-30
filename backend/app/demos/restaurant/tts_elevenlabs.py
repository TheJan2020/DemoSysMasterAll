"""
ElevenLabs streaming-TTS bridge for the restaurant Live Agent.

Wraps ElevenLabs's bidirectional WebSocket endpoint
(`wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input`) so
the call session can:

  - `start()`           — open the WS, send the initial voice-settings
                          envelope.
  - `send_text(chunk)`  — push a partial text token as Gemini emits it.
                          ElevenLabs synthesises incrementally; an empty
                          chunk forces a flush.
  - `flush()`           — explicit "finalize this utterance" signal so
                          the last few words don't sit in the buffer
                          waiting for a sentence boundary.
  - `cancel()`          — drop the in-flight synthesis on barge-in: we
                          send an end-of-stream sentinel, close the
                          socket, and clear the local audio buffer so
                          no stale frames reach the caller.
  - `audio_frames()`    — async generator that yields PCM-24kHz frames
                          as ElevenLabs streams them back. The Live
                          Agent's existing 24k→8k downsampler then
                          pipes them into the AudioSocket queue.

Output format is hard-coded to `pcm_24000` so it slots into the
existing Gemini-audio pipeline byte-for-byte — no new resampling code
needed on the receive side.

ElevenLabs charges per *character of input text*, not per audio
second, so we keep a running character counter and surface it on
`stats()` for the Debug page / per-call cost telemetry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncGenerator, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from ...core.state import state

logger = logging.getLogger("demo_restaurant.tts_elevenlabs")

# WS query parameters that come into the URL itself. ElevenLabs expects
# the model + output format on the URL, not in the JSON envelope.
_OUTPUT_FORMAT = "pcm_24000"


class ElevenLabsTTSSession:
    """One ElevenLabs streaming WebSocket per call. Not shared between
    calls — each call has its own session because the WS multiplexes
    only one utterance stream at a time."""

    def __init__(self, *, call_id: str,
                 voice_id: Optional[str] = None,
                 model_id: Optional[str] = None,
                 api_key:  Optional[str] = None) -> None:
        self.call_id  = call_id
        self.voice_id = (voice_id or state.elevenlabs_voice_id
                          or "EXAVITQu4vr4xnSDxMaL")
        self.model_id = (model_id or state.elevenlabs_model_id
                          or "eleven_multilingual_v2")
        self.api_key  = (api_key  or state.elevenlabs_api_key or "")

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._closed = False
        # Audio buffer: each item is a chunk of PCM-24k bytes. Bounded
        # so a slow consumer (the AudioSocket writer) can't blow up RAM
        # if synthesis briefly outpaces playback.
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._reader_task: Optional[asyncio.Task] = None
        # Per-call running totals — surfaced by stats() for the Debug page.
        self.chars_sent  = 0
        self.bytes_recvd = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the WS and send the BOS (beginning-of-stream) envelope
        with voice settings + auth. Raises if anything fails so the
        caller can fall back to Gemini's native voice cleanly."""
        if not self.api_key:
            raise RuntimeError("elevenlabs_api_key not set")
        # Output PCM at 24 kHz so it drops straight into the existing
        # Gemini-audio pipeline (24k → 8k downsampler). `inactivity_timeout`
        # is bumped to 180s so the WS doesn't close mid-call during a
        # long pause; `enable_logging=false` keeps it off ElevenLabs's
        # analytics for the demo.
        url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{self.voice_id}/stream-input"
            f"?model_id={self.model_id}"
            f"&output_format={_OUTPUT_FORMAT}"
            f"&inactivity_timeout=180"
        )
        logger.info(
            "ElevenLabs TTS connecting call=%s url=%s voice=%s model=%s key=%s…%s",
            self.call_id, url, self.voice_id, self.model_id,
            self.api_key[:4], self.api_key[-4:] if len(self.api_key) > 8 else "",
        )
        self._ws = await websockets.connect(
            url,
            additional_headers={"xi-api-key": self.api_key},
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        )
        # BOS envelope. Per ElevenLabs docs the first message MUST
        # include `text=" "` (single space) + voice_settings. The actual
        # spoken content arrives via subsequent send_text() calls. We
        # don't pass `auto_mode` on the URL because we explicitly
        # trigger generation per chunk below (`flush=true` on a final
        # empty text marks end-of-utterance).
        await self._ws.send(json.dumps({
            "text": " ",
            "voice_settings": {
                "stability":         0.55,
                "similarity_boost":  0.85,
                "style":             0.05,
                "use_speaker_boost": True,
            },
            "generation_config": {
                # LARGER chunks = smoother prosody. With the aggressive
                # [50,90,...] schedule the synthesiser starts a new
                # generation window every ~50 chars, which audibly
                # micro-pauses between every couple of words. Default
                # 120/160/... lets it think in phrases.
                "chunk_length_schedule": [120, 160, 250, 290],
            },
            "xi_api_key": self.api_key,
        }))
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"el-rd-{self.call_id}",
        )
        logger.info("ElevenLabs TTS opened call=%s", self.call_id)

    async def send_text(self, chunk: str) -> None:
        """Push a partial text token. ElevenLabs synthesises
        incrementally according to the `chunk_length_schedule` we set
        at BOS — we deliberately do NOT pass `try_trigger_generation`
        per chunk because that forces a fresh synthesis window for
        every word and produces audible micro-gaps between them."""
        if not chunk or self._closed or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"text": chunk}))
            self.chars_sent += len(chunk)
            logger.info("el→ call=%s chars=%d sent='%s'",
                         self.call_id, self.chars_sent,
                         chunk[:60].replace("\n", " "))
        except ConnectionClosed:
            self._closed = True
            logger.warning("el→ call=%s WS closed mid-send", self.call_id)

    async def flush(self) -> None:
        """Mark end of utterance so the model emits any tail audio. The
        WS stays open — start() opened it for the whole call."""
        if self._closed or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"text": "", "flush": True}))
        except ConnectionClosed:
            self._closed = True

    async def cancel(self) -> None:
        """Barge-in path: drop the in-flight synthesis. We close the WS
        (cheaper than waiting for a graceful end-of-stream we'll
        immediately discard) and drain the local audio queue so no
        stale frames reach the caller."""
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._reader_task is not None:
            self._reader_task.cancel()
        # Drain anything already buffered.
        try:
            while True:
                self._audio_q.get_nowait()
        except asyncio.QueueEmpty:
            pass

    async def close(self) -> None:
        """Normal call-end cleanup. Sends the EOS sentinel first so any
        in-progress audio finishes naturally, then closes the WS."""
        if self._ws is not None and not self._closed:
            try:
                await self._ws.send(json.dumps({"text": ""}))
            except Exception:
                pass
        await self.cancel()

    # ------------------------------------------------------------------
    # Audio frame producer
    # ------------------------------------------------------------------

    async def audio_frames(self) -> AsyncGenerator[bytes, None]:
        """Yields PCM-24kHz bytes as ElevenLabs streams them back. The
        consumer pushes these onto the Live Agent's `audio_out` queue
        (the existing 24k→8k downsampler then handles the rest)."""
        while not self._closed:
            try:
                chunk = await self._audio_q.get()
            except asyncio.CancelledError:
                break
            if not chunk:
                continue
            yield chunk

    # ------------------------------------------------------------------
    # Internal reader
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Pulls ElevenLabs's JSON envelopes off the WS and pushes the
        decoded PCM into `_audio_q`. ElevenLabs envelopes look like:

            {"audio": "<base64-pcm>", "isFinal": false, "alignment": …}
            {"audio": null, "isFinal": true}
        """
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                try:
                    msg = json.loads(raw) if isinstance(raw, (str, bytes)) else None
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                b64 = msg.get("audio")
                if b64:
                    try:
                        pcm = base64.b64decode(b64)
                    except Exception:
                        continue
                    self.bytes_recvd += len(pcm)
                    logger.info("el← call=%s pcm=%dB total=%d",
                                 self.call_id, len(pcm), self.bytes_recvd)
                    # Drop oldest if the consumer is lagging — silent
                    # frames on barge-in are better than RAM blowup.
                    try:
                        self._audio_q.put_nowait(pcm)
                    except asyncio.QueueFull:
                        try:
                            self._audio_q.get_nowait()
                            self._audio_q.put_nowait(pcm)
                        except Exception:
                            pass
                if msg.get("error") or msg.get("message"):
                    logger.warning("ElevenLabs TTS message call=%s payload=%s",
                                    self.call_id, str(msg)[:300])
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("ElevenLabs reader crashed call=%s: %s",
                            self.call_id, e)
        finally:
            self._closed = True

    # ------------------------------------------------------------------
    # Stats — surfaced on the Debug page so the operator can see cost
    # trending per call.
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "voice_id":   self.voice_id,
            "model_id":   self.model_id,
            "chars_sent": self.chars_sent,
            "bytes_recvd": self.bytes_recvd,
            "closed":     self._closed,
        }


def is_configured() -> bool:
    """Cheap predicate the Live Agent uses to decide whether to swap
    Gemini audio out for ElevenLabs streaming TTS on a given call.
    Now gated on the explicit `voice_provider` toggle so that just
    having an API key set (e.g. for the CAI path) doesn't auto-enable
    this bridge — the operator picks each mode in Settings."""
    if state.voice_provider != "gemini_with_elevenlabs_tts":
        return False
    return bool((state.elevenlabs_api_key or "").strip()) \
        and bool((state.elevenlabs_voice_id or "").strip())
