"""
Custom Meals — subscription plans (catalog) + active subscriptions
(per-client instances).

Storage:
    data/demos/restaurant/subscriptions.json

Tracked by git (operator content); `.tmp` from atomic writes is
gitignored.

Design
- A `SubscriptionPlan` is an offering on the menu. 6 standard plans
  ship with the seed (Starter, Balanced Daily, Family, Keto Custom,
  Weight Management, Plant-Based). Each plan declares:
    · `meals_per_week`: scheduling unit
    · `meal_slots`: which slots (breakfast/lunch/dinner/snack) the plan
      fills each delivery day
    · `required_diet_tags`: the agent + the schedule picker will only
      choose meals that contain every tag in this list
    · `excluded_allergens`: any allergen-bearing meal is rejected
      regardless of client's per-client allergies
    · `calorie_target`: optional daily target (Weight Mgmt sets this)
    · `pricing`: a weekly + monthly + quarterly price ladder
- A `Subscription` instance points at a client + plan, locks in a
  billing cycle + price, and tracks status. Schedules read from
  subscriptions (not plans directly) so that an operator could
  customise a single client's plan without forking the catalog.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.subscriptions")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "subscriptions.json"

_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

BILLING_CYCLES        = ["weekly", "monthly", "quarterly"]
DELIVERY_WINDOWS      = ["morning", "afternoon", "evening", "anytime"]
SUBSCRIPTION_STATUSES = ["active", "paused", "cancelled", "expired"]
PAYMENT_STATUSES      = ["paid", "due", "overdue", "refunded"]


# ----------------------------------------------------------------------
# Plans seed — 6 standard offerings, calibrated for a Saudi
# Lebanese-restaurant operation. Prices are illustrative; meal_slots
# decides how many meals/day each plan covers.
# ----------------------------------------------------------------------

DEFAULT_PLANS: list[dict] = [
    {
        "id": "PLN-STARTER",
        "code": "STARTER",
        "name_en": "Lebanese Starter",
        "name_ar": "الباقة التمهيدية",
        "description_en": "One healthy lunch every day, 7 days a week. Best for trying out the service.",
        "description_ar": "غداء صحي واحد يومياً لمدة 7 أيام في الأسبوع.",
        "meals_per_week": 7,
        "meal_slots": ["lunch"],
        "required_diet_tags": [],
        "excluded_allergens": [],
        "calorie_target": 0,        # no enforced cap
        "price_weekly":  220.00,
        "price_monthly": 800.00,
        "price_quarterly": 2280.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 1,
    },
    {
        "id": "PLN-BALANCED",
        "code": "BALANCED",
        "name_en": "Balanced Daily",
        "name_ar": "اليومي المتوازن",
        "description_en": "Lunch + dinner, 7 days a week (14 meals). Balanced macros.",
        "description_ar": "غداء وعشاء يومياً (14 وجبة أسبوعياً).",
        "meals_per_week": 14,
        "meal_slots": ["lunch", "dinner"],
        "required_diet_tags": [],
        "excluded_allergens": [],
        "calorie_target": 1800,
        "price_weekly":  430.00,
        "price_monthly": 1600.00,
        "price_quarterly": 4560.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 2,
    },
    {
        "id": "PLN-FAMILY",
        "code": "FAMILY",
        "name_en": "Family Plan",
        "name_ar": "باقة العائلة",
        "description_en": "Breakfast + lunch + dinner (21 meals/week). For households of 2-5.",
        "description_ar": "فطور وغداء وعشاء (21 وجبة أسبوعياً) للعائلات.",
        "meals_per_week": 21,
        "meal_slots": ["breakfast", "lunch", "dinner"],
        "required_diet_tags": [],
        "excluded_allergens": [],
        "calorie_target": 0,
        "price_weekly":  680.00,
        "price_monthly": 2500.00,
        "price_quarterly": 7100.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 3,
    },
    {
        "id": "PLN-KETO",
        "code": "KETO",
        "name_en": "Keto Custom",
        "name_ar": "كيتو مخصّص",
        "description_en": "Lunch + dinner, strictly low-carb. 14 meals/week, < 30g net carbs/day.",
        "description_ar": "غداء وعشاء بنظام الكيتو (14 وجبة أسبوعياً).",
        "meals_per_week": 14,
        "meal_slots": ["lunch", "dinner"],
        "required_diet_tags": ["low-carb"],
        "excluded_allergens": [],
        "calorie_target": 1700,
        "price_weekly":  520.00,
        "price_monthly": 1950.00,
        "price_quarterly": 5550.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 4,
    },
    {
        "id": "PLN-WEIGHT",
        "code": "WEIGHT",
        "name_en": "Weight Management",
        "name_ar": "إدارة الوزن",
        "description_en": "Lunch + dinner + 1 snack, calorie-controlled at 1500/day.",
        "description_ar": "غداء وعشاء ووجبة خفيفة (1500 سعرة حرارية يومياً).",
        "meals_per_week": 21,
        "meal_slots": ["lunch", "dinner", "snack"],
        "required_diet_tags": ["low-sodium"],
        "excluded_allergens": [],
        "calorie_target": 1500,
        "price_weekly":  490.00,
        "price_monthly": 1820.00,
        "price_quarterly": 5180.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 5,
    },
    {
        "id": "PLN-PLANT",
        "code": "PLANT",
        "name_en": "Plant-Based",
        "name_ar": "نباتي",
        "description_en": "Lunch + dinner, all plant-based. Optional dairy via 'vegetarian' upgrade.",
        "description_ar": "غداء وعشاء نباتي بالكامل.",
        "meals_per_week": 14,
        "meal_slots": ["lunch", "dinner"],
        "required_diet_tags": ["vegetarian"],
        "excluded_allergens": [],
        "calorie_target": 1750,
        "price_weekly":  400.00,
        "price_monthly": 1480.00,
        "price_quarterly": 4220.00,
        "currency": DEFAULT_CURRENCY,
        "active": True,
        "sort_order": 6,
    },
]


# ----------------------------------------------------------------------
# Subscriptions seed — 6 client subscriptions covering the spread of
# plans. Client ids match the seeded clients.json so the page joins
# cleanly out of the box.
# ----------------------------------------------------------------------

_TODAY = time.localtime()


def _today_iso() -> str:
    return f"{_TODAY.tm_year:04d}-{_TODAY.tm_mon:02d}-{_TODAY.tm_mday:02d}"


def _days_from_today(d: int) -> str:
    t = time.localtime(time.time() + d * 86400)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _days_ago(d: int) -> int:
    return int(time.time() - d * 86400)


DEFAULT_SUBSCRIPTIONS: list[dict] = [
    # Khalid — high-protein → Balanced
    {"id": "SUB-001", "client_id": "CLT-001", "plan_id": "PLN-BALANCED",
     "start_date": _days_from_today(-20), "end_date": _days_from_today(40),
     "billing_cycle": "monthly", "price": 1600.00, "currency": DEFAULT_CURRENCY,
     "status": "active", "payment_status": "paid",
     "delivery_address": "Al-Olaya, Riyadh", "delivery_window": "evening",
     "meal_overrides": [], "extra_allergens": [],
     "notes": "Wants extra grilled chicken — log extras as add-ons.",
     "created_at": _days_ago(22)},
    # Aisha — celiac (gluten-free) → on Family + we filter wheat-bearing meals via her client allergies.
    {"id": "SUB-002", "client_id": "CLT-002", "plan_id": "PLN-FAMILY",
     "start_date": _days_from_today(-10), "end_date": _days_from_today(50),
     "billing_cycle": "monthly", "price": 2500.00, "currency": DEFAULT_CURRENCY,
     "status": "active", "payment_status": "paid",
     "delivery_address": "Al-Yasmin, Riyadh", "delivery_window": "morning",
     "meal_overrides": [], "extra_allergens": ["wheat"],
     "notes": "Two kids on the plan — half portions on weekends.",
     "created_at": _days_ago(12)},
    # Mohammed — low-sodium + diabetic → Weight Management
    {"id": "SUB-003", "client_id": "CLT-003", "plan_id": "PLN-WEIGHT",
     "start_date": _days_from_today(-30), "end_date": _days_from_today(60),
     "billing_cycle": "quarterly", "price": 5180.00, "currency": DEFAULT_CURRENCY,
     "status": "active", "payment_status": "paid",
     "delivery_address": "Al-Nakheel, Riyadh", "delivery_window": "afternoon",
     "meal_overrides": [], "extra_allergens": [],
     "notes": "Cardiologist note on file — strict 1500 cal cap.",
     "created_at": _days_ago(32)},
    # Layla — vegetarian + peanut allergy → Plant-Based
    {"id": "SUB-004", "client_id": "CLT-004", "plan_id": "PLN-PLANT",
     "start_date": _days_from_today(-5), "end_date": _days_from_today(25),
     "billing_cycle": "monthly", "price": 1480.00, "currency": DEFAULT_CURRENCY,
     "status": "active", "payment_status": "paid",
     "delivery_address": "Al-Sahafah, Riyadh", "delivery_window": "evening",
     "meal_overrides": [], "extra_allergens": [],
     "notes": "Eats fish occasionally — flag as exception when added.",
     "created_at": _days_ago(7)},
    # Omar — keto + high-protein → Keto Custom
    {"id": "SUB-005", "client_id": "CLT-005", "plan_id": "PLN-KETO",
     "start_date": _days_from_today(-3), "end_date": _days_from_today(25),
     "billing_cycle": "monthly", "price": 1950.00, "currency": DEFAULT_CURRENCY,
     "status": "active", "payment_status": "paid",
     "delivery_address": "Hittin, Riyadh", "delivery_window": "morning",
     "meal_overrides": [], "extra_allergens": [],
     "notes": "Trains 6x/week — request 200g protein/day.",
     "created_at": _days_ago(4)},
    # Hanan — vegan + nuts allergy → Plant-Based (paused)
    {"id": "SUB-006", "client_id": "CLT-008", "plan_id": "PLN-PLANT",
     "start_date": _days_from_today(-15), "end_date": _days_from_today(45),
     "billing_cycle": "monthly", "price": 1480.00, "currency": DEFAULT_CURRENCY,
     "status": "paused", "payment_status": "paid",
     "delivery_address": "Al-Malqa, Riyadh", "delivery_window": "anytime",
     "meal_overrides": [], "extra_allergens": [],
     "notes": "Paused for travel — resume in 7 days.",
     "created_at": _days_ago(17)},
]


def _default_payload() -> dict:
    return {
        "plans":         deepcopy(DEFAULT_PLANS),
        "subscriptions": deepcopy(DEFAULT_SUBSCRIPTIONS),
    }


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_data() -> dict:
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("subscriptions.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    plans = d.get("plans") if isinstance(d.get("plans"), list) else []
    subs  = d.get("subscriptions") if isinstance(d.get("subscriptions"), list) else []
    return {
        "plans":         [p for p in plans if isinstance(p, dict)] or deepcopy(DEFAULT_PLANS),
        "subscriptions": [s for s in subs  if isinstance(s, dict)],
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_subscriptions() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_data()


# ----------------------------------------------------------------------
# Plan CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    """Raised when the request is malformed."""


def _next_id(rows: list[dict], prefix: str) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith(prefix + "-"):
            tail = rid[len(prefix) + 1:]
            try: n = max(n, int(tail))
            except Exception: pass
    return f"{prefix}-{n + 1:03d}"


def _coerce_plan(p: dict, existing: list[dict]) -> dict:
    slots = p.get("meal_slots") or []
    if not isinstance(slots, list): slots = []
    return {
        "id":                 str(p.get("id") or _next_id(existing, "PLN")).strip(),
        "code":               str(p.get("code") or "").strip().upper()[:32],
        "name_en":            str(p.get("name_en") or "").strip()[:200],
        "name_ar":            str(p.get("name_ar") or "").strip()[:200],
        "description_en":     str(p.get("description_en") or "").strip()[:600],
        "description_ar":     str(p.get("description_ar") or "").strip()[:600],
        "meals_per_week":     max(1, int(p.get("meals_per_week") or 7)),
        "meal_slots":         [str(s).strip().lower()[:16] for s in slots if str(s).strip()],
        "required_diet_tags": sorted({str(t).strip().lower()[:32]
                                       for t in (p.get("required_diet_tags") or [])
                                       if str(t).strip()}),
        "excluded_allergens": sorted({str(a).strip().lower()[:32]
                                       for a in (p.get("excluded_allergens") or [])
                                       if str(a).strip()}),
        "calorie_target":     max(0, int(p.get("calorie_target") or 0)),
        "price_weekly":       round(max(0.0, float(p.get("price_weekly") or 0)), 2),
        "price_monthly":      round(max(0.0, float(p.get("price_monthly") or 0)), 2),
        "price_quarterly":    round(max(0.0, float(p.get("price_quarterly") or 0)), 2),
        "currency":           (str(p.get("currency") or DEFAULT_CURRENCY).strip()
                                or DEFAULT_CURRENCY)[:8],
        "active":             bool(p.get("active", True)),
        "sort_order":         int(p.get("sort_order") or 99),
    }


def add_plan(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Plan needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        plan = _coerce_plan(patch, payload["plans"])
        payload["plans"].append(plan)
        _save_locked(payload)
        return plan


def update_plan(plan_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, p in enumerate(payload["plans"]):
            if p.get("id") == plan_id:
                merged = {**p, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = plan_id
                if not (merged.get("name_en") or merged.get("name_ar") or "").strip():
                    raise _Refused("Plan needs at least one name (EN or AR).")
                payload["plans"][i] = _coerce_plan(merged, payload["plans"])
                _save_locked(payload)
                return payload["plans"][i]
    return None


def delete_plan(plan_id: str) -> dict:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["plans"])
        payload["plans"] = [p for p in payload["plans"] if p.get("id") != plan_id]
        if len(payload["plans"]) == before:
            return {"removed": False, "subscriptions_orphaned": 0}
        # Count subscriptions that still point at this plan.
        n_refs = sum(1 for s in payload["subscriptions"] if s.get("plan_id") == plan_id)
        _save_locked(payload)
        return {"removed": True, "subscriptions_orphaned": n_refs}


# ----------------------------------------------------------------------
# Subscription CRUD
# ----------------------------------------------------------------------

def _coerce_subscription(s: dict, existing: list[dict]) -> dict:
    cycle = str(s.get("billing_cycle") or "monthly").strip().lower()
    if cycle not in BILLING_CYCLES: cycle = "monthly"
    win = str(s.get("delivery_window") or "anytime").strip().lower()
    if win not in DELIVERY_WINDOWS: win = "anytime"
    status = str(s.get("status") or "active").strip().lower()
    if status not in SUBSCRIPTION_STATUSES: status = "active"
    pay = str(s.get("payment_status") or "due").strip().lower()
    if pay not in PAYMENT_STATUSES: pay = "due"
    return {
        "id":               str(s.get("id") or _next_id(existing, "SUB")).strip(),
        "client_id":        str(s.get("client_id") or "").strip(),
        "plan_id":          str(s.get("plan_id") or "").strip(),
        "start_date":       str(s.get("start_date") or "").strip()[:10],
        "end_date":         str(s.get("end_date") or "").strip()[:10],
        "billing_cycle":    cycle,
        "price":            round(max(0.0, float(s.get("price") or 0)), 2),
        "currency":         (str(s.get("currency") or DEFAULT_CURRENCY).strip()
                              or DEFAULT_CURRENCY)[:8],
        "status":           status,
        "payment_status":   pay,
        "delivery_address": str(s.get("delivery_address") or "").strip()[:300],
        "delivery_window":  win,
        "meal_overrides":   [str(x).strip() for x in (s.get("meal_overrides") or []) if str(x).strip()],
        "extra_allergens":  [str(a).strip().lower() for a in (s.get("extra_allergens") or [])
                              if str(a).strip()],
        "notes":            str(s.get("notes") or "").strip()[:1000],
        "created_at":       int(s.get("created_at") or time.time()),
    }


def _validate_sub(patch: dict) -> None:
    if not (patch.get("client_id") or "").strip():
        raise _Refused("client_id is required.")
    if not (patch.get("plan_id") or "").strip():
        raise _Refused("plan_id is required.")
    if not (patch.get("start_date") or "").strip():
        raise _Refused("start_date is required (YYYY-MM-DD).")


def add_subscription(patch: dict) -> dict:
    _validate_sub(patch)
    with _LOCK:
        payload = _load_locked()
        sub = _coerce_subscription(patch, payload["subscriptions"])
        payload["subscriptions"].append(sub)
        _save_locked(payload)
        return sub


def update_subscription(sub_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, s in enumerate(payload["subscriptions"]):
            if s.get("id") == sub_id:
                merged = {**s, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = sub_id
                payload["subscriptions"][i] = _coerce_subscription(merged, payload["subscriptions"])
                _save_locked(payload)
                return payload["subscriptions"][i]
    return None


def delete_subscription(sub_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["subscriptions"])
        payload["subscriptions"] = [
            s for s in payload["subscriptions"] if s.get("id") != sub_id
        ]
        removed = len(payload["subscriptions"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# Read helpers
# ----------------------------------------------------------------------

def active_subscriptions() -> list[dict]:
    return [s for s in load_data()["subscriptions"] if s.get("status") == "active"]


def plan_by_id(plan_id: str) -> Optional[dict]:
    for p in load_data()["plans"]:
        if p.get("id") == plan_id:
            return p
    return None
