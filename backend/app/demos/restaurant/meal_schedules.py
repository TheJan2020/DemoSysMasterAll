"""
Custom Meals — schedule generator + persistent schedule rows.

Schedules are derived from active subscriptions. We persist them so the
operator can hand-edit (swap a meal, mark delivered, skip a day)
without re-running the generator and losing the edits.

Storage:
    data/demos/restaurant/meal_schedules.json

Tracked by git (operator content); `.tmp` is gitignored.

Allergy + diet enforcement
- The generator picks meals via `meals_catalog.meals_suitable_for(...)`
  which already enforces: (a) the plan's required diet tags must be a
  subset of the meal's diet tags, (b) the meal's allergens must NOT
  intersect the client's allergies (parsed from `clients.allergies`
  text + `subscription.extra_allergens`).
- The generator also rejects any meal whose id appears in the
  subscription's `meal_overrides` list (client-side block-list).
"""
from __future__ import annotations

import json
import logging
import re
import random
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

from . import clients as clients_mod
from . import meals_catalog as meals_mod
from . import subscriptions as subs_mod

logger = logging.getLogger("demo_restaurant.meal_schedules")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "meal_schedules.json"

_LOCK = threading.Lock()

SCHEDULE_STATUSES = ["pending", "delivered", "skipped"]


# ----------------------------------------------------------------------
# Allergy parsing from the client's free-text `allergies` field. We map
# common keywords to allergen codes used by the meals catalog. Anything
# the operator typed verbatim that matches a known code is kept too.
# ----------------------------------------------------------------------

_ALLERGY_KEYWORDS = [
    (r"peanut",     "peanut"),
    (r"tree[- ]?nut|walnut|cashew|almond|pistachio|hazelnut|pecan",
                    "tree-nut"),
    (r"dairy|lactose|milk|cheese|yog?h?urt",
                    "dairy"),
    (r"egg",        "egg"),
    (r"wheat|gluten|celiac|coelia[ck]",
                    "wheat"),
    (r"soy|tofu",   "soy"),
    (r"fish",       "fish"),
    (r"shellfish|shrimp|crab|lobster|prawn",
                    "shellfish"),
    (r"sesame|tahini",
                    "sesame"),
    (r"sulph?ite",  "sulphites"),
]


def parse_allergies(text: str) -> list[str]:
    """Turn free-text allergy notes into a list of allergen codes the
    meals catalog can compare against. Idempotent + deduped."""
    if not text:
        return []
    blob = text.lower()
    out: list[str] = []
    for pattern, code in _ALLERGY_KEYWORDS:
        if re.search(pattern, blob) and code not in out:
            out.append(code)
    return out


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_schedules() -> list[dict]:
    """Return every persisted schedule row, sorted (date, slot, sub_id)."""
    with _LOCK:
        rows = _load_locked().get("schedules") or []
    rows.sort(key=lambda r: (r.get("date") or "",
                              _slot_order(r.get("meal_slot") or ""),
                              r.get("subscription_id") or ""))
    return rows


_SLOT_RANK = {"breakfast": 0, "lunch": 1, "snack": 2, "dinner": 3}


def _slot_order(slot: str) -> int:
    return _SLOT_RANK.get((slot or "").lower(), 99)


def _load_locked() -> dict:
    if not _PATH.exists():
        return {"schedules": []}
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("meal_schedules.json corrupt — starting empty")
        return {"schedules": []}
    if not isinstance(d, dict):
        return {"schedules": []}
    rows = d.get("schedules")
    return {"schedules": [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_schedules() -> list[dict]:
    """Regenerate schedules from scratch for the next 7 days for every
    active subscription. Overwrites any operator edits."""
    new_rows = generate_for_active(days=7)
    with _LOCK:
        _save_locked({"schedules": new_rows})
    return load_schedules()


# ----------------------------------------------------------------------
# Generator — the core "what to feed who, on which day" decision.
# ----------------------------------------------------------------------

def _today_iso() -> str:
    t = time.localtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _iso_plus(days: int) -> str:
    t = time.localtime(time.time() + days * 86400)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _candidates_for(sub: dict, plan: dict, client: dict,
                     slot: str) -> list[dict]:
    """Every meal a given client could be served for this slot, after
    applying client allergies + plan diet tags + subscription's
    extra_allergens + meal_overrides."""
    client_allergens  = parse_allergies(client.get("allergies") or "")
    extra             = [a.strip().lower() for a in (sub.get("extra_allergens") or []) if a]
    forbidden         = list({*client_allergens, *extra, *(plan.get("excluded_allergens") or [])})
    required_tags     = list(plan.get("required_diet_tags") or [])
    candidates = meals_mod.meals_suitable_for(forbidden, required_tags, category=slot)
    block = set(sub.get("meal_overrides") or [])
    return [m for m in candidates if m.get("id") not in block]


def generate_for_active(days: int = 7) -> list[dict]:
    """Build schedule rows for the next `days` days, covering every
    active subscription. Tries to vary meals across the week (no same
    meal two days in a row when alternatives exist), but falls back
    gracefully if a slot has only one suitable meal."""
    rng = random.Random(42)   # deterministic so reset produces
                              # the same schedule until subs change.
    sub_data = subs_mod.load_data()
    plans_by_id = {p["id"]: p for p in sub_data["plans"]}

    client_by_id: dict = {}
    for c in clients_mod.load_clients():
        client_by_id[c["id"]] = c

    out: list[dict] = []
    next_id = 1
    for sub in sub_data["subscriptions"]:
        if sub.get("status") != "active":
            continue
        plan = plans_by_id.get(sub.get("plan_id"))
        client = client_by_id.get(sub.get("client_id"))
        if not plan or not client:
            continue

        # Per-slot rotation so we don't repeat meals back-to-back.
        rotations: dict[str, list[dict]] = {}
        last_chosen: dict[str, Optional[str]] = {}
        for slot in plan.get("meal_slots") or []:
            cands = _candidates_for(sub, plan, client, slot)
            rng.shuffle(cands)
            rotations[slot] = cands
            last_chosen[slot] = None

        for day_offset in range(days):
            date = _iso_plus(day_offset)
            for slot in plan.get("meal_slots") or []:
                cands = rotations.get(slot) or []
                pick: Optional[dict] = None
                if cands:
                    # Prefer one that isn't the last-served meal.
                    for i, m in enumerate(cands):
                        if m["id"] != last_chosen.get(slot):
                            pick = m
                            rotations[slot] = cands[i + 1:] + cands[: i + 1]
                            break
                    if pick is None:
                        pick = cands[0]
                last_chosen[slot] = pick["id"] if pick else None
                out.append({
                    "id":              f"SCH-{next_id:06d}",
                    "subscription_id": sub["id"],
                    "client_id":       sub["client_id"],
                    "plan_id":         sub["plan_id"],
                    "date":            date,
                    "meal_slot":       slot,
                    "meal_id":         pick["id"] if pick else "",
                    "meal_name_en":    pick.get("name_en") if pick else "",
                    "meal_name_ar":    pick.get("name_ar") if pick else "",
                    "status":          "pending",
                    "notes":           ("" if pick else
                                         "No suitable meal found "
                                         "(check allergies / diet tags / overrides)"),
                })
                next_id += 1
    return out


# ----------------------------------------------------------------------
# Row CRUD — for hand-edits the operator makes on the Schedules page.
# ----------------------------------------------------------------------

class _Refused(ValueError):
    """Raised when a schedule row is malformed."""


def _coerce_row(r: dict, existing: list[dict]) -> dict:
    status = (str(r.get("status") or "pending").strip().lower())
    if status not in SCHEDULE_STATUSES: status = "pending"
    slot = (str(r.get("meal_slot") or "lunch").strip().lower())
    return {
        "id":              str(r.get("id") or _next_id(existing)).strip(),
        "subscription_id": str(r.get("subscription_id") or "").strip(),
        "client_id":       str(r.get("client_id") or "").strip(),
        "plan_id":         str(r.get("plan_id") or "").strip(),
        "date":            str(r.get("date") or "").strip()[:10],
        "meal_slot":       slot,
        "meal_id":         str(r.get("meal_id") or "").strip(),
        "meal_name_en":    str(r.get("meal_name_en") or "").strip()[:200],
        "meal_name_ar":    str(r.get("meal_name_ar") or "").strip()[:200],
        "status":          status,
        "notes":           str(r.get("notes") or "").strip()[:500],
    }


def _next_id(existing: list[dict]) -> str:
    n = 0
    for r in existing:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("SCH-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"SCH-{n + 1:06d}"


def update_row(row_id: str, patch: dict) -> Optional[dict]:
    """Hand-edit a schedule row (swap meal, mark delivered, etc.)."""
    with _LOCK:
        payload = _load_locked()
        for i, r in enumerate(payload["schedules"]):
            if r.get("id") == row_id:
                merged = {**r, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = row_id
                # If meal_id changed, refresh the denormalised name fields.
                if patch.get("meal_id") and patch["meal_id"] != r.get("meal_id"):
                    cat = meals_mod.load_catalog()
                    for m in cat["meals"]:
                        if m["id"] == patch["meal_id"]:
                            merged["meal_name_en"] = m.get("name_en") or ""
                            merged["meal_name_ar"] = m.get("name_ar") or ""
                            break
                payload["schedules"][i] = _coerce_row(merged, payload["schedules"])
                _save_locked(payload)
                return payload["schedules"][i]
    return None


def delete_row(row_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["schedules"])
        payload["schedules"] = [r for r in payload["schedules"] if r.get("id") != row_id]
        removed = len(payload["schedules"]) < before
        if removed:
            _save_locked(payload)
        return removed
