"""
Minimal Asterisk Manager Interface (AMI) client.

We don't pull `panoramisk` — AMI is a plain key:value TCP protocol that's
trivial to speak with `asyncio.open_connection`, and avoiding the dep means
no version conflicts on Windows and one less thing to `pip install` per
machine. The client is purpose-built for ONE operation right now: originate
a call from the supervisor's extension and bridge them into a live clinic
call via ChanSpy (barge mode = full 3-way).

A future extension would add `find_channel_for_uuid()` to look up the
AudioSocket channel for a given call_id; for v1 we ring the supervisor's
phone and play the `beep` tone so they know the integration fired — they
can then use FreePBX's *0 (zap-barge) feature code from their phone to
listen in, or we add ChanSpy auto-bridging in v2 once we know the user's
channel-naming convention.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("demo_restaurant.ami")

# AMI uses CRLF line endings and a blank line as the message terminator.
_LINE_END = b"\r\n"
_MSG_END  = b"\r\n\r\n"


@dataclass
class AMICredentials:
    host: str
    port: int
    username: str
    secret: str

    def is_complete(self) -> bool:
        return bool(self.host and self.username and self.secret and self.port > 0)


def _encode_action(fields: dict) -> bytes:
    """Serialise a {key: value} dict into AMI wire format."""
    parts: list[bytes] = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"{k}: {v}".encode("utf-8"))
    return _LINE_END.join(parts) + _MSG_END


def _decode_message(buf: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in buf.split(b"\r\n"):
        if not line:
            continue
        try:
            k, _, v = line.decode("utf-8", "replace").partition(":")
        except Exception:
            continue
        out[k.strip()] = v.strip()
    return out


class AMIClient:
    """Single-shot AMI client. Connects per action — the OS gives us
    sub-millisecond TCP handshakes to localhost / a LAN PBX, and avoiding
    long-lived state means we never have to deal with reconnect logic or
    stale sockets when the FreePBX user changes their credentials in the
    Configuration page. Throughput is irrelevant: a supervisor dial-in
    fires maybe once per minute at peak."""

    def __init__(self, creds: AMICredentials, *, timeout_s: float = 5.0):
        self.creds = creds
        self.timeout = timeout_s

    async def _read_message(self, reader: asyncio.StreamReader) -> dict[str, str]:
        """Read up to the next blank-line terminator."""
        chunks: list[bytes] = []
        while True:
            try:
                chunk = await asyncio.wait_for(reader.readuntil(_MSG_END), timeout=self.timeout)
            except asyncio.IncompleteReadError as e:
                chunks.append(e.partial)
                break
            chunks.append(chunk)
            break
        return _decode_message(b"".join(chunks))

    async def _read_until_event(
        self,
        reader: asyncio.StreamReader,
        end_event: str,
        max_messages: int = 500,
    ) -> list[dict[str, str]]:
        """Drain messages until we see an Event line equal to `end_event`
        (e.g. CoreShowChannelsComplete). Stops at max_messages just in
        case the stream never terminates — safety belt."""
        out: list[dict[str, str]] = []
        for _ in range(max_messages):
            msg = await self._read_message(reader)
            if not msg:
                break
            out.append(msg)
            if (msg.get("Event") or "") == end_event:
                break
        return out

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict[str, str]:
        # Server sends a greeting on connect (e.g. "Asterisk Call Manager/X.Y.Z").
        # Drain it before issuing Login.
        try:
            await asyncio.wait_for(reader.readuntil(_LINE_END), timeout=self.timeout)
        except Exception:
            pass
        writer.write(_encode_action({
            "Action":   "Login",
            "Username": self.creds.username,
            "Secret":   self.creds.secret,
            "Events":   "off",
        }))
        await writer.drain()
        return await self._read_message(reader)

    async def _logoff(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(_encode_action({"Action": "Logoff"}))
            await writer.drain()
        except Exception:
            pass

    async def originate(
        self,
        *,
        channel: str,
        application: Optional[str] = None,
        data: Optional[str] = None,
        context: Optional[str] = None,
        extension: Optional[str] = None,
        priority: Optional[int] = None,
        caller_id: str = "Supervisor Dial-in",
        timeout_ms: int = 30_000,
        async_originate: bool = True,
    ) -> dict[str, str]:
        """Issue an AMI Originate. Exactly one of (application+data) OR
        (context+extension+priority) should be set — the wrapper doesn't
        enforce it; Asterisk errors out cleanly if both are given."""
        if not self.creds.is_complete():
            return {"Response": "Error", "Message": "AMI credentials incomplete (host / username / secret / port not all set)"}

        action_id = str(_uuid.uuid4())
        action = {
            "Action":      "Originate",
            "ActionID":    action_id,
            "Channel":     channel,
            "CallerID":    caller_id,
            "Timeout":     timeout_ms,
            "Async":       "true" if async_originate else "false",
        }
        if application is not None:
            action["Application"] = application
            if data is not None:
                action["Data"] = data
        if context is not None:
            action["Context"] = context
        if extension is not None:
            action["Exten"] = extension
        if priority is not None:
            action["Priority"] = priority

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.creds.host, self.creds.port),
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("AMI connect failed (%s:%s): %s", self.creds.host, self.creds.port, e)
            return {"Response": "Error", "Message": f"connect to {self.creds.host}:{self.creds.port} failed: {e}"}

        try:
            login = await self._login(reader, writer)
            if (login.get("Response") or "").lower() != "success":
                msg = login.get("Message") or "authentication failed"
                logger.warning("AMI login rejected: %s", msg)
                return {"Response": "Error", "Message": f"AMI login: {msg}"}

            writer.write(_encode_action(action))
            await writer.drain()
            resp = await self._read_message(reader)
            await self._logoff(writer)
            return resp
        finally:
            try: writer.close(); await writer.wait_closed()
            except Exception: pass

    async def core_show_channels(self) -> list[dict[str, str]]:
        """Snapshot of every active channel on the PBX. Each entry is the
        raw CoreShowChannel event dict — has keys like Channel, ChannelState,
        Application, ApplicationData, ConnectedLineName, Duration, etc.
        Returns [] on any connect / auth / parse failure (we don't raise
        because the caller falls back to ring+beep cleanly)."""
        if not self.creds.is_complete():
            return []
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.creds.host, self.creds.port),
                timeout=self.timeout,
            )
        except Exception as e:
            logger.warning("AMI connect for CoreShowChannels failed: %s", e)
            return []
        try:
            login = await self._login(reader, writer)
            if (login.get("Response") or "").lower() != "success":
                return []
            writer.write(_encode_action({
                "Action":   "CoreShowChannels",
                "ActionID": str(_uuid.uuid4()),
            }))
            await writer.drain()
            events = await self._read_until_event(reader, "CoreShowChannelsComplete")
            await self._logoff(writer)
            return [e for e in events if (e.get("Event") or "") == "CoreShowChannel"]
        finally:
            try: writer.close(); await writer.wait_closed()
            except Exception: pass


# --- Service singleton ------------------------------------------------------

class AMIService:
    """Reads creds from escalation.json on every call (cheap — single small
    file), so editing them from the Configuration page takes effect on the
    next click with no restart."""

    def __init__(self, load_creds):
        # load_creds is a 0-arg callable that returns an AMICredentials.
        self._load_creds = load_creds

    async def dial_supervisor(
        self,
        call_id: str,
        extension: str,
        spy_mode: str = "barge",
    ) -> dict:
        """Ring the supervisor's extension and drop them straight into the
        live clinic call via ChanSpy.

        spy_mode controls the audio policy:
          - "listen"  : silent monitor (no voice from supervisor into the call)
          - "whisper" : supervisor's voice goes ONLY to the caller-leg —
                        the AI (Gemini, via AudioSocket) doesn't hear it
          - "barge"   : 3-way — both the caller AND the AI hear the
                        supervisor; default for QA "jump-in" use case

        Strategy:
          1. Ask AMI for every active channel; find the one currently
             bridged with our AudioSocket leg.
          2. Originate a call to the supervisor; on answer, run
             `ChanSpy(<target>,<opts>)`.
          3. If no target channel found (no active call OR FreePBX hides
             them somehow), fall back to ring + beep so the operator at
             least knows the integration fired.
        """
        creds = self._load_creds()
        # Entry log — shows up on the Debug page so the operator can see
        # the click reached the backend, with which (non-secret) creds
        # and target extension. The early-exit returns below don't log
        # otherwise.
        logger.info(
            "dial_supervisor ENTRY call_id=%s ext=%r mode=%s creds_complete=%s "
            "host=%r port=%s user=%r secret_set=%s",
            call_id, extension, spy_mode, creds.is_complete(),
            creds.host, creds.port, creds.username, bool(creds.secret),
        )
        if not creds.is_complete():
            err = (
                "AMI credentials are incomplete (host / port / username / "
                "secret all required). Open Call Center → Configuration → "
                "PBX integration on THIS machine — escalation.json is "
                "per-machine (gitignored), so the values from the other "
                "machine don't sync."
            )
            logger.warning("dial_supervisor BAIL: %s", err)
            return {"ok": False, "error": err}
        if not extension:
            err = (
                "supervisor_extension not configured. Open Call Center → "
                "Configuration → Supervisor escalation triggers on THIS "
                "machine and set it."
            )
            logger.warning("dial_supervisor BAIL: %s", err)
            return {"ok": False, "error": err}

        # Normalise spy_mode → ChanSpy option string. Bad value falls
        # back to barge silently (safer than failing on a typo).
        #
        # ChanSpy options used:
        #   q  = quiet — don't play the "beep" before connecting
        #   s  = skip the channel-name announcement ("PJSIP/1002…")
        #   w  = whisper — supervisor's voice is heard ONLY by the spied
        #        channel (the caller leg). The bridged peer (AudioSocket
        #        leg → Gemini) does NOT hear the supervisor.
        #   B  = barge — supervisor's voice is heard by BOTH sides of
        #        the bridge (caller AND Gemini, via AudioSocket).
        #
        # NB: we deliberately do NOT pass the `o` flag here, even though
        # we target a specific channel. `o` ("only this channel, don't
        # follow callees through bridge") blocks ChanSpy from following
        # into the bridged Local channel — which is the path Gemini's
        # audio flows along on its way to the caller. Without `o`, we
        # get both directions; with it, we only hear the spied channel's
        # read side (caller's mic).
        spy_opts_by_mode = {
            "listen":  "qs",
            "whisper": "qsw",
            "barge":   "qsB",
        }
        spy_opts = spy_opts_by_mode.get(spy_mode, spy_opts_by_mode["barge"])
        effective_mode = spy_mode if spy_mode in spy_opts_by_mode else "barge"

        client = AMIClient(creds)

        # ----- 1. Pick the target channel -------------------------------
        channels = await client.core_show_channels()
        # FreePBX 16 / Asterisk 18+: Application name is "AudioSocket"
        # verbatim. Case-insensitive match for safety across versions.
        audiosocket_channels = [
            c for c in channels
            if (c.get("Application") or "").strip().lower() == "audiosocket"
        ]
        # Multi-concurrent: prefer the most recently started call.
        # Duration is "HH:MM:SS" on older Asterisks and seconds-as-string
        # on newer; handle both.
        def _dur_key(ch: dict) -> int:
            d = ch.get("Duration") or ch.get("BridgeDuration") or "999999"
            if ":" in d:
                try:
                    h, m, s = (int(x) for x in d.split(":"))
                    return h * 3600 + m * 60 + s
                except Exception:
                    return 999_999
            try: return int(d)
            except Exception: return 999_999
        audiosocket_channels.sort(key=_dur_key)

        # Pick the right target depending on the dialplan style:
        #
        # (A) Legacy — AudioSocket() runs DIRECTLY on the caller's PJSIP
        #     channel. ChanSpy on that channel may only hear the caller
        #     (AudioSocket's WriteFrame bypasses the audiohook). Best
        #     we can do without a dialplan change.
        #
        # (B) Recommended — AudioSocket() runs on a Local channel that
        #     is bridged with the caller's PJSIP channel via Dial().
        #     The caller's PJSIP channel IS in a normal bridge and
        #     ChanSpy on it captures both directions. We detect this
        #     case by finding an AudioSocket channel whose name starts
        #     with "Local/" then walking its bridge to find the PJSIP
        #     peer. Falls back to the AudioSocket channel itself if no
        #     bridge peer can be identified.
        def _bridge_id(ch: dict) -> str:
            return ch.get("BridgeId") or ch.get("BridgeID") or ""

        target_channel = None
        target_style = None
        target_log = []
        if audiosocket_channels:
            asc = audiosocket_channels[0]
            asc_name = asc.get("Channel") or ""
            target_log.append(f"AudioSocket channel: {asc_name} bridge={_bridge_id(asc) or '<none>'}")
            if asc_name.startswith("Local/"):
                # Local pair — find the ;1 sibling and walk its bridge.
                sibling_name = (
                    asc_name.replace(";2", ";1")
                    if ";2" in asc_name else asc_name.replace(";1", ";2")
                )
                sibling = next((c for c in channels if c.get("Channel") == sibling_name), None)
                if sibling:
                    target_log.append(f"Local sibling: {sibling_name} bridge={_bridge_id(sibling) or '<none>'}")
                bridge_id = _bridge_id(sibling) if sibling else _bridge_id(asc)
                if bridge_id:
                    peers = [
                        c for c in channels
                        if _bridge_id(c) == bridge_id
                           and not (c.get("Channel") or "").startswith("Local/")
                    ]
                    target_log.append(f"Bridge peers (non-Local) in {bridge_id}: " +
                                      ", ".join(p.get("Channel", "?") for p in peers))
                    if peers:
                        target_channel = peers[0].get("Channel")
                        target_style = "local-pair-bridge-peer"
            if target_channel is None:
                # No Local-pair peer found — spy on the AudioSocket channel
                # directly. This is the legacy path; works for caller-only
                # audio at minimum.
                target_channel = asc_name
                target_style = "direct-audiosocket"

        # WARNING (not INFO) so it shows in uvicorn console out of the
        # box — the demo_clinic.ami logger has no explicit handler so
        # INFO would propagate to root and get filtered. Demote back to
        # info once channel selection is stable.
        diag_line = (
            f"clinic dial_supervisor: total={len(channels)} "
            f"audiosocket={len(audiosocket_channels)} style={target_style} "
            f"target={target_channel} | "
            + (" | ".join(target_log) or "no candidates")
        )
        logger.warning(diag_line)

        # ----- 2. Originate ---------------------------------------------
        # Local/<ext>@from-internal hits the same dialplan path a normal
        # internal call uses on FreePBX, so DND / forwarding / find-me
        # all behave normally for the supervisor's extension.
        channel = f"Local/{extension}@from-internal/n"

        if target_channel:
            mode_label_by_mode = {
                "listen":  "monitoring",
                "whisper": "coaching",
                "barge":   "joining",
            }
            caller_id_verb = mode_label_by_mode[effective_mode]
            resp = await client.originate(
                channel=channel,
                application="ChanSpy",
                data=f"{target_channel},{spy_opts}",
                caller_id=f"\"Supervisor ({caller_id_verb} call)\" <{extension}>",
                timeout_ms=20_000,
            )
            mode = f"chanspy-{effective_mode}"
        else:
            # Nothing to spy on — at least ring the phone so the operator
            # sees the integration is alive. Useful while debugging.
            resp = await client.originate(
                channel=channel,
                application="Playback",
                data="beep",
                caller_id=f"\"Clinic call needs attention\" <{call_id[:8]}>",
                timeout_ms=20_000,
            )
            mode = "beep"

        success = (resp.get("Response") or "").lower() == "success"
        return {
            "ok":             success,
            "mode":           mode,
            "spy_mode":       effective_mode,
            "channel":        channel,
            "target_channel": target_channel,
            "target_style":   target_style,
            # Diagnostic info echoed back to the SPA so we can see what
            # the bridge-walk picked without grepping uvicorn logs.
            "diagnostic":     diag_line,
            "raw":            resp,
            "error":          None if success else (resp.get("Message") or "AMI Originate failed"),
        }
