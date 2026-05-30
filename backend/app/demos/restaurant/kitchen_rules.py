"""
Kitchen AI-Rules — persisted rules + iteration ledger.

Each rule pairs a Frigate camera with a natural-language condition the
operator wants the AI to evaluate (e.g. "is someone preparing sandwiches
at the sandwich station?"). The background engine fires the rule based
on its trigger_mode:

    periodic         every scan_interval_s
    motion_periodic  every scan_interval_s, but only while Frigate
                      currently reports motion on the rule's camera
    motion_once      once per motion event (fires once when motion
                      transitions inactive → active)

Every run produces an iteration row. Iterations whose verdict is True
are also conceptually "triggers" — there's no separate triggers store,
the UI just filters iterations by verdict.

The operator can mark each iteration as "correct" or "incorrect" so we
can compute per-rule accuracy and surface it on the main row.

Storage:
    data/demos/restaurant/kitchen_rules.json
    data/demos/restaurant/kitchen_rules/<iter_id>.jpg

Both gitignored — iteration snapshots are kitchen footage (PII).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.kitchen_rules")

_DATA_DIR    = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH        = _DATA_DIR / "kitchen_rules.json"
_IMAGES_DIR  = _DATA_DIR / "kitchen_rules"
_LOCK = threading.Lock()

TRIGGER_MODES = ["periodic", "motion_periodic", "motion_once"]

SCAN_INTERVAL_MIN = 3
SCAN_INTERVAL_MAX = 600
DEFAULT_SCAN_INTERVAL_S = 30

# Cap iterations per rule — when we cross this, the oldest are FIFO-
# evicted (both the JSON row and the JPEG on disk).
ITERATION_CAP_PER_RULE = 200


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def _default_state() -> dict:
    return {"rules": [], "iterations": []}


def load_state() -> dict:
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_state()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("kitchen_rules.json corrupt — starting fresh")
        return _default_state()
    if not isinstance(d, dict):
        return _default_state()
    rules = d.get("rules") if isinstance(d.get("rules"), list) else []
    iters = d.get("iterations") if isinstance(d.get("iterations"), list) else []
    return {
        "rules":      [r for r in rules if isinstance(r, dict)],
        "iterations": [r for r in iters if isinstance(r, dict)],
    }


def _save_locked(state: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return state


# ----------------------------------------------------------------------
# Image paths
# ----------------------------------------------------------------------

def image_dir() -> Path:
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return _IMAGES_DIR


def image_path(iter_id: str) -> Path:
    safe = "".join(c for c in (iter_id or "") if c.isalnum() or c in "-_.")
    return image_dir() / f"{safe}.jpg"


def _delete_image(iter_id: str) -> None:
    if not iter_id: return
    try:
        p = image_path(iter_id)
        if p.exists(): p.unlink()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Rules CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_rule_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("RUL-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"RUL-{n + 1:03d}"


def _coerce_rule(r: dict, existing: list[dict]) -> dict:
    mode = str(r.get("trigger_mode") or "periodic").strip().lower()
    if mode not in TRIGGER_MODES: mode = "periodic"
    provider = str(r.get("provider") or "gemini").strip().lower()
    if provider not in ("gemini", "deepseek", "openrouter"): provider = "gemini"
    # Auto-fix `provider` for rules created before OpenRouter was wired
    # into the form (their model name made it through but the encoded
    # provider got downcast to "gemini"). OpenRouter model ids carry a
    # vendor prefix (e.g. "qwen/qwen3-vl-8b-instruct"); Gemini ids never
    # contain a slash, so the slash is a safe signal.
    model_name = str(r.get("model") or "").strip()
    if "/" in model_name and provider != "openrouter":
        provider = "openrouter"
    try:
        interval = int(r.get("scan_interval_s") or DEFAULT_SCAN_INTERVAL_S)
    except Exception:
        interval = DEFAULT_SCAN_INTERVAL_S
    interval = max(SCAN_INTERVAL_MIN, min(SCAN_INTERVAL_MAX, interval))
    # `ha_script` is the new HA target — when a verdict turns True the
    # engine calls POST /api/services/script/<ha_script>. The older
    # `ha_event_name` field (fire-an-event flow) is still read here as a
    # one-time migration so existing rules keep working.
    ha_script = str(r.get("ha_script")
                     or r.get("ha_event_name") or "").strip()[:120]
    return {
        "id":              str(r.get("id") or _next_rule_id(existing)).strip()[:32],
        "name":            str(r.get("name") or "").strip()[:200] or "Untitled rule",
        "camera":          str(r.get("camera") or "").strip()[:64],
        "prompt":          str(r.get("prompt") or "").strip()[:2000],
        "provider":        provider,
        "model":           str(r.get("model") or "").strip()[:64],
        "trigger_mode":    mode,
        "scan_interval_s": interval,
        "ha_script":       ha_script,
        "active":          bool(r.get("active", True)),
        "created_at":      int(r.get("created_at") or time.time()),
    }


def list_rules() -> list[dict]:
    rules = load_state().get("rules") or []
    rules.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return rules


def get_rule(rule_id: str) -> Optional[dict]:
    for r in list_rules():
        if r.get("id") == rule_id:
            return r
    return None


def add_rule(patch: dict) -> dict:
    if not (patch.get("name") or "").strip():
        raise _Refused("Rule needs a name.")
    if not (patch.get("camera") or "").strip():
        raise _Refused("Rule needs a camera.")
    if not (patch.get("prompt") or "").strip():
        raise _Refused("Rule needs a prompt.")
    with _LOCK:
        state = _load_locked()
        rule = _coerce_rule(patch, state["rules"])
        state["rules"].append(rule)
        _save_locked(state)
    return rule


def update_rule(rule_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        state = _load_locked()
        for i, r in enumerate(state["rules"]):
            if r.get("id") == rule_id:
                merged = {**r, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = rule_id
                if not (merged.get("name") or "").strip():
                    raise _Refused("Rule needs a name.")
                if not (merged.get("camera") or "").strip():
                    raise _Refused("Rule needs a camera.")
                if not (merged.get("prompt") or "").strip():
                    raise _Refused("Rule needs a prompt.")
                state["rules"][i] = _coerce_rule(merged, state["rules"])
                _save_locked(state)
                return state["rules"][i]
    return None


def delete_rule(rule_id: str) -> bool:
    with _LOCK:
        state = _load_locked()
        before = len(state["rules"])
        state["rules"] = [r for r in state["rules"] if r.get("id") != rule_id]
        # Cascade — drop every iteration belonging to this rule, including
        # the saved JPEG.
        drop = [it for it in state["iterations"] if it.get("rule_id") == rule_id]
        for it in drop:
            _delete_image(it.get("id") or "")
        state["iterations"] = [it for it in state["iterations"]
                                if it.get("rule_id") != rule_id]
        removed = len(state["rules"]) < before
        if removed:
            _save_locked(state)
        return removed


# ----------------------------------------------------------------------
# Iterations
# ----------------------------------------------------------------------

def _coerce_iteration(it: dict) -> dict:
    score = it.get("score")
    if score not in (None, "correct", "incorrect"):
        score = None
    provider = str(it.get("provider") or "gemini").strip().lower()
    if provider not in ("gemini", "deepseek", "openrouter"): provider = "gemini"
    # Same migration as in _coerce_rule — fix stale iterations whose
    # provider got downcast before OpenRouter was wired.
    model_name = str(it.get("model") or "").strip()
    if "/" in model_name and provider != "openrouter":
        provider = "openrouter"
    return {
        "id":             str(it.get("id") or "").strip(),
        "rule_id":        str(it.get("rule_id") or "").strip(),
        "ts":             float(it.get("ts") or time.time()),
        "trigger_reason": str(it.get("trigger_reason") or "").strip()[:32],
        "provider":       provider,
        "model":          str(it.get("model") or "").strip()[:64],
        "verdict":        bool(it.get("verdict", False)),
        "confidence":     round(float(it.get("confidence") or 0), 3),
        "explanation":    str(it.get("explanation") or "").strip()[:2000],
        "score":          score,
        "ha_fired":       bool(it.get("ha_fired", False)),
        "ha_error":       str(it.get("ha_error") or "").strip()[:500] or None,
        "latency_ms":     int(it.get("latency_ms") or 0),
        "error":          str(it.get("error") or "").strip()[:500] or None,
    }


def append_iteration(it: dict) -> dict:
    row = _coerce_iteration(it)
    if not row["id"] or not row["rule_id"]:
        raise ValueError("iteration needs id + rule_id")
    with _LOCK:
        state = _load_locked()
        state["iterations"].append(row)
        # FIFO eviction per rule.
        for_rule = [r for r in state["iterations"] if r.get("rule_id") == row["rule_id"]]
        if len(for_rule) > ITERATION_CAP_PER_RULE:
            keep_ids = {r["id"] for r in sorted(for_rule, key=lambda r: r.get("ts") or 0,
                                                  reverse=True)[:ITERATION_CAP_PER_RULE]}
            evict = [r for r in for_rule if r["id"] not in keep_ids]
            for ev in evict:
                _delete_image(ev.get("id") or "")
            state["iterations"] = [
                r for r in state["iterations"]
                if r.get("rule_id") != row["rule_id"] or r["id"] in keep_ids
            ]
        _save_locked(state)
        return row


def list_iterations(rule_id: Optional[str] = None,
                      limit: int = 50, offset: int = 0,
                      only_triggers: bool = False,
                      from_ts: Optional[float] = None,
                      to_ts: Optional[float] = None) -> dict:
    if limit < 1: limit = 1
    if limit > 200: limit = 200
    if offset < 0: offset = 0
    rows = load_state().get("iterations") or []
    if rule_id:
        rows = [r for r in rows if r.get("rule_id") == rule_id]
    if only_triggers:
        rows = [r for r in rows if r.get("verdict")]
    if from_ts is not None:
        rows = [r for r in rows if float(r.get("ts") or 0) >= float(from_ts)]
    if to_ts is not None:
        rows = [r for r in rows if float(r.get("ts") or 0) <= float(to_ts)]
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {
        "total":      len(rows),
        "limit":      limit,
        "offset":     offset,
        "iterations": rows[offset: offset + limit],
    }


def score_iteration(iter_id: str, score: Optional[str]) -> Optional[dict]:
    if score not in (None, "correct", "incorrect"):
        raise _Refused("score must be 'correct', 'incorrect', or null.")
    with _LOCK:
        state = _load_locked()
        for i, it in enumerate(state["iterations"]):
            if it.get("id") == iter_id:
                it["score"] = score
                state["iterations"][i] = it
                _save_locked(state)
                return it
    return None


def delete_iteration(iter_id: str) -> bool:
    with _LOCK:
        state = _load_locked()
        before = len(state["iterations"])
        state["iterations"] = [it for it in state["iterations"]
                                if it.get("id") != iter_id]
        removed = len(state["iterations"]) < before
        if removed:
            _delete_image(iter_id)
            _save_locked(state)
        return removed


# ----------------------------------------------------------------------
# Stats — what the SPA's main row shows next to each rule.
# ----------------------------------------------------------------------

def stats_for_rule(rule_id: str) -> dict:
    iters = [r for r in (load_state().get("iterations") or [])
             if r.get("rule_id") == rule_id]
    triggers = [r for r in iters if r.get("verdict")]
    correct   = sum(1 for r in iters if r.get("score") == "correct")
    incorrect = sum(1 for r in iters if r.get("score") == "incorrect")
    scored = correct + incorrect
    accuracy = (correct / scored) if scored else None
    latest = max(iters, key=lambda r: r.get("ts") or 0) if iters else None
    return {
        "iterations":  len(iters),
        "triggers":    len(triggers),
        "correct":     correct,
        "incorrect":   incorrect,
        "scored":      scored,
        "accuracy":    accuracy,        # 0..1 or None
        "latest_ts":   latest.get("ts") if latest else None,
        "latest_verdict": latest.get("verdict") if latest else None,
    }
