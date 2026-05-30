"""
Background runner for Kitchen → AI-Rules.

Every TICK_INTERVAL_S seconds the engine walks all active rules and
decides — based on trigger_mode + last-fire time + Frigate motion
state — whether each rule should fire NOW. When a rule fires, the
engine:

    1. Pulls a fresh JPEG from Frigate (/api/<camera>/latest.jpg).
    2. Calls Gemini with the rule's prompt and a structured
       verdict / confidence / explanation schema.
    3. Saves the snapshot + appends an iteration to the ledger.
    4. If the verdict is True AND the rule has ha_event_name set,
       POSTs to {HA_URL}/api/events/<event_name> with a small JSON
       body so Home Assistant automations can react.

Trigger modes:
    periodic         fire every scan_interval_s
    motion_periodic  fire every scan_interval_s, but ONLY while
                      Frigate currently reports motion on this camera
    motion_once      fire ONCE per motion event — i.e. when motion
                      transitions inactive → active

Lifecycle:
    ensure_running() is idempotent. Call it from FastAPI lifespan
    + from any endpoint that mutates rules. The engine self-pauses
    when Frigate or Gemini are unconfigured.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from ...core.state import state
from ...services.mqtt import mqtt_service
from . import kitchen_rules as rules_mod
from . import kitchen_vision as kvision

logger = logging.getLogger("demo_restaurant.kitchen_rules_engine")

TICK_INTERVAL_S = 2.0
HTTP_TIMEOUT_S  = 12.0

_TASK: Optional[asyncio.Task] = None


# ----------------------------------------------------------------------
# Pub/sub for iteration events — used by the SPA's AI-Rules page to
# blink rows in real time when the engine fires a rule. Identical
# pattern to the MQTT service: each WS subscriber gets its own bounded
# asyncio.Queue and we drop oldest on overflow so a slow client can't
# stall the engine loop.
# ----------------------------------------------------------------------

_SUBSCRIBERS: list[asyncio.Queue] = []
_SUB_QUEUE_MAX = 64


def add_subscriber() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_SUB_QUEUE_MAX)
    _SUBSCRIBERS.append(q)
    return q


def remove_subscriber(q: asyncio.Queue) -> None:
    try:
        _SUBSCRIBERS.remove(q)
    except ValueError:
        pass


def _broadcast(payload: dict) -> None:
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop oldest — slow subscriber shouldn't stall the engine.
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except Exception:
                pass

# Per-rule cursors. Reset on server restart (in-memory only).
_last_fire: dict[str, float] = {}        # rule_id -> unix ts of last fire
_prev_motion: dict[str, bool] = {}       # rule_id -> motion state last tick


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------

def ensure_running() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _TASK = loop.create_task(_run_forever(), name="kitchen_rules_engine")
    logger.info("kitchen AI-rules engine started")


def is_running() -> bool:
    return _TASK is not None and not _TASK.done()


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

async def _run_forever() -> None:
    await asyncio.sleep(2.0)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("kitchen rules engine tick failed")
        await asyncio.sleep(TICK_INTERVAL_S)


async def _tick() -> None:
    rules = [r for r in rules_mod.list_rules() if r.get("active")]
    if not rules:
        return
    # Reach into the MQTT service for the current motion snapshot — that
    # singleton already subscribes to frigate/+/motion and keeps an
    # up-to-date per-camera state.
    motion = mqtt_service.snapshot() if mqtt_service else {}
    now = time.time()
    # Pick the rules whose `_should_fire` window is open right now. We
    # run them IN PARALLEL — earlier this was a sequential `for` loop
    # and a single slow vision-provider call (OpenRouter cold start,
    # rate-limit pause, etc.) would freeze every other rule until it
    # returned. With gather() each rule gets its own coroutine and a
    # 30-second hang on one doesn't park the rest of the engine.
    fireable: list[tuple[dict, str]] = []
    for r in rules:
        should_fire, reason = _should_fire(r, motion, now)
        if should_fire:
            fireable.append((r, reason))
            # Stamp the fire time NOW so concurrent ticks (if the
            # scan_interval is shorter than the analyse duration) don't
            # re-fire the same rule before this one returns.
            _last_fire[r["id"]] = time.time()
    if not fireable:
        return

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        async def _wrap(r: dict, reason: str) -> None:
            try:
                await _execute_rule(r, reason, client)
            except Exception:
                logger.exception("rule %s execution failed", r.get("id"))
        await asyncio.gather(
            *(_wrap(r, reason) for r, reason in fireable),
            return_exceptions=False,
        )


def _should_fire(rule: dict, motion_state: dict, now: float) -> tuple[bool, str]:
    """Return (fire?, reason) per trigger_mode."""
    mode      = rule.get("trigger_mode") or "periodic"
    interval  = max(1, int(rule.get("scan_interval_s") or 30))
    rule_id   = rule["id"]
    camera    = rule.get("camera") or ""
    last      = _last_fire.get(rule_id, 0.0)
    cam_state = (motion_state or {}).get(camera) or {}
    is_motion = bool(cam_state.get("motion"))
    prev      = _prev_motion.get(rule_id, False)
    # Always remember the latest motion sample for edge detection.
    _prev_motion[rule_id] = is_motion

    if mode == "periodic":
        if (now - last) >= interval:
            return True, "periodic"
        return False, ""
    if mode == "motion_periodic":
        if is_motion and (now - last) >= interval:
            return True, "motion+periodic"
        return False, ""
    if mode == "motion_once":
        # Rising edge: motion just turned on.
        if is_motion and not prev:
            return True, "motion"
        return False, ""
    return False, ""


# ----------------------------------------------------------------------
# Rule execution
# ----------------------------------------------------------------------

_VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict":     {"type": "BOOLEAN"},
        "confidence":  {"type": "NUMBER"},
        "explanation": {"type": "STRING"},
    },
    "required": ["verdict", "confidence", "explanation"],
}


def _system_prompt(rule: dict) -> str:
    return (
        "You are a kitchen monitoring AI evaluating a single rule on a "
        "still frame from a kitchen camera. Be conservative — only "
        "answer verdict=true if you can clearly see the condition met.\n\n"
        f"RULE: {rule.get('prompt', '').strip()}\n\n"
        "Return JSON with this exact shape:\n"
        '  {"verdict": <bool>, "confidence": <0..1>, "explanation": <one '
        "short sentence>}\n"
        "Confidence reflects how certain you are about the verdict, not "
        "how strong the activity is."
    )


async def _execute_rule(rule: dict, trigger_reason: str,
                          client: httpx.AsyncClient) -> None:
    rule_id = rule["id"]
    iter_id = f"ITR-{rule_id}-{int(time.time() * 1000)}"
    started = time.time()

    # 1) Frigate snapshot.
    img_bytes = b""
    if state.frigate_url and rule.get("camera"):
        url = f"{state.frigate_url}/api/{rule['camera']}/latest.jpg"
        try:
            r = await client.get(url, params={"h": 720})
            if r.status_code == 200:
                img_bytes = r.content
        except httpx.HTTPError as e:
            logger.warning("rule %s snapshot fetch failed: %s", rule_id, e)
    if not img_bytes:
        rules_mod.append_iteration({
            "id": iter_id, "rule_id": rule_id, "ts": started,
            "trigger_reason": trigger_reason,
            "provider": rule.get("provider") or "gemini",
            "model": rule.get("model") or "", "verdict": False,
            "confidence": 0.0,
            "explanation": "Snapshot fetch failed.",
            "ha_fired": False,
            "latency_ms": int((time.time() - started) * 1000),
            "error": "frigate_snapshot_failed",
        })
        return

    # Persist the snapshot up front so the UI can show it even if Gemini
    # later errors out.
    try:
        rules_mod.image_path(iter_id).write_bytes(img_bytes)
    except Exception:
        logger.exception("could not save snapshot for %s", iter_id)

    # 2) Vision call — provider-agnostic via kitchen_vision. Routes to
    # Gemini or DeepSeek based on the rule's provider field.
    provider = (rule.get("provider") or "gemini").strip().lower()
    out = await kvision.analyze(
        image_bytes=img_bytes,
        system_prompt=_system_prompt(rule),
        user_prompt="Evaluate the rule and reply now with the JSON verdict.",
        provider=provider,
        model=rule.get("model"),
        response_schema=_VERDICT_SCHEMA if provider == "gemini" else None,
        expect_json=True,
        temperature=0.1,
    )
    verdict, confidence, explanation = False, 0.0, ""
    error: Optional[str] = None
    if not out.get("ok"):
        error = out.get("error") or "vision_failed"
        explanation = error
    else:
        text = (out.get("text") or "").strip()
        try:
            parsed = json.loads(text) if text else {}
        except Exception:
            parsed = {}
        verdict     = bool(parsed.get("verdict"))
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        explanation = str(parsed.get("explanation") or text or "")[:1000]

    # 3) Persist iteration.
    iter_row = rules_mod.append_iteration({
        "id": iter_id, "rule_id": rule_id, "ts": started,
        "trigger_reason": trigger_reason,
        "provider": provider,
        "model": out.get("model") or rule.get("model") or "",
        "verdict": verdict,
        "confidence": confidence,
        "explanation": explanation,
        "ha_fired": False,
        "latency_ms": out.get("latency_ms")
                      or int((time.time() - started) * 1000),
        "error": error,
    })

    # 4) HA script — only on a positive verdict + non-empty script name.
    ha_script = (rule.get("ha_script") or rule.get("ha_event_name") or "").strip()
    ha_fired = False
    ha_err: Optional[str] = None
    if verdict and ha_script and state.homeassistant_url and state.homeassistant_token:
        ha_fired, ha_err = await _fire_ha_script(rule, iter_row)
        # Patch the iteration we just wrote with the HA outcome.
        with rules_mod._LOCK:
            st = rules_mod._load_locked()
            for i, it in enumerate(st["iterations"]):
                if it.get("id") == iter_id:
                    st["iterations"][i]["ha_fired"] = ha_fired
                    if ha_err:
                        st["iterations"][i]["ha_error"] = ha_err
                    rules_mod._save_locked(st)
                    break

    # 5) Broadcast the iteration to any WS subscribers (the AI-Rules
    #    SPA listens so it can blink rule rows on fire).
    _broadcast({
        "type":           "iteration",
        "rule_id":        rule_id,
        "camera":         rule.get("camera"),
        "iteration_id":   iter_id,
        "ts":             started,
        "verdict":        verdict,
        "confidence":     confidence,
        "trigger_reason": trigger_reason,
        "provider":       provider,
        "model":          out.get("model") or rule.get("model") or "",
        "ha_fired":       ha_fired,
        "ha_error":       ha_err,
        "error":          error,
    })


async def call_ha_script(script_name: str,
                           variables: dict,
                           source: str = "ai-rules") -> tuple[bool, Optional[str]]:
    """Single source of truth for firing a Home Assistant script.

    Used by BOTH the Test button (which posts to /kitchen/ai-rules/ha/test-script)
    AND the engine's per-iteration HA fire. If the Test button works,
    the engine MUST behave identically because they share this function.

    Posts to {ha_url}/api/services/script/turn_on with
    `{entity_id, variables}` — the same shape HA's Lovelace UI uses.
    Returns (ok, error_or_None). Always logs the full request + response
    to the backend log so the operator can compare engine fires vs test
    fires side-by-side."""
    base  = (state.homeassistant_url or "").rstrip("/")
    token = state.homeassistant_token or ""
    if not (base and token):
        return False, "ha_unconfigured"
    s = (script_name or "").strip()
    if s.startswith("script."):
        s = s[len("script."):]
    if not s:
        return False, "no_script"
    url = f"{base}/api/services/script/turn_on"
    body = {"entity_id": f"script.{s}", "variables": dict(variables or {})}
    try:
        # Fresh client per call — same pattern as the Test endpoint, so
        # connection state can't possibly diverge between callers.
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type":   "application/json"},
                json=body,
            )
        logger.info(
            "HA script.turn_on (source=%s) — url=%s body=%s status=%s response=%s",
            source, url, json.dumps(body)[:600], r.status_code,
            (r.text or "")[:400],
        )
        if 200 <= r.status_code < 300:
            return True, None
        return False, _format_ha_error(r)
    except httpx.HTTPError as e:
        logger.warning("HA script.turn_on (source=%s) HTTP error: %s", source, e)
        return False, f"{type(e).__name__}: {e}"


async def _fire_ha_script(rule: dict, iter_row: dict) -> tuple[bool, Optional[str]]:
    """Engine-side wrapper around `call_ha_script` — assembles the
    variables dict from rule + iteration context, then delegates.
    Uses its own httpx client via `call_ha_script` so connection state
    can't diverge from the Test button's call."""
    script = (rule.get("ha_script") or rule.get("ha_event_name") or "").strip()
    if not script:
        return False, "no_script"
    explanation = (iter_row.get("explanation") or "")[:500]
    variables = {
        "source":       "ai-rules-engine",
        "fired_at":     int(time.time()),
        "rule_id":      str(rule.get("id") or ""),
        "rule_name":    str(rule.get("name") or ""),
        "camera":       str(rule.get("camera") or ""),
        "verdict":      bool(iter_row.get("verdict", True)),
        "confidence":   float(iter_row.get("confidence") or 0.0),
        "explanation":  explanation,
        "iteration_id": str(iter_row.get("id") or ""),
        "ts":           int(float(iter_row.get("ts") or 0)),
    }
    return await call_ha_script(script, variables, source="engine")


def _format_ha_error(r: "httpx.Response") -> str:
    """Squeeze HA's response into a short human line. HA's REST API
    returns either a JSON envelope (`{"message": "..."}`) on validation
    failure or aiohttp's raw text body on framework-level rejection."""
    try:
        j = r.json()
        msg = (j.get("message") if isinstance(j, dict) else None) or r.text
    except Exception:
        msg = r.text
    return f"ha_status_{r.status_code}: {(msg or '').strip()[:200]}"
