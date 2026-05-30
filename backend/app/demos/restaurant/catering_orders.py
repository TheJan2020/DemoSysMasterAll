"""
Catering — event orders.

Catering doesn't share the Custom Meals `clients` DB — each order
carries the customer's contact details inline because most clients
order once. Repeat customers are detected by phone-number match in
the Overview / Insights pages.

Storage:
    data/demos/restaurant/catering_orders.json

Tracked by git (operator content); `.tmp` from atomic writes is
gitignored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.catering_orders")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "catering_orders.json"

_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

# Enumerations — drive the SPA's quick-pick chips. Free-text values are
# allowed via the API; these are the ones the UI suggests.
EVENT_TYPES = ["wedding", "corporate", "birthday", "graduation",
                "conference", "reception", "ramadan_iftar", "other"]
PACKAGE_TYPES = [
    "cocktail_reception",   # finger foods, 8-12 items per guest
    "buffet",               # 4-6 mains + 6-8 sides
    "plated_dinner",        # 3-course served
    "box_lunch",            # individual packed meals
    "tea_ceremony",         # Saudi mezze + qahwa
    "custom",               # operator-defined
]
ORDER_STATUSES = ["inquiry", "confirmed", "in_kitchen",
                   "out_for_delivery", "delivered", "completed",
                   "cancelled"]
PAYMENT_STATUSES = ["unpaid", "deposit_paid", "paid_in_full", "refunded"]

# Phone normalisation — strip everything that isn't digit/+ for the
# repeat-customer detector. Operator-edited spaces, dashes, parens etc.
# don't break matches.
_PHONE_TIDY = re.compile(r"[^\d+]+")


# ----------------------------------------------------------------------
# Seed — 12 orders spread across statuses + event types. Times are
# anchored to NOW so the Overview pipeline always looks meaningful.
# ----------------------------------------------------------------------

_NOW = int(time.time())


def _days_from_now(d: int, hour: int = 19, minute: int = 0) -> int:
    """Unix timestamp for today+d at the given local hour."""
    base = time.localtime(time.time() + d * 86400)
    return int(time.mktime((base.tm_year, base.tm_mon, base.tm_mday,
                             hour, minute, 0, 0, 0, base.tm_isdst)))


def _iso(ts: int) -> str:
    t = time.localtime(ts)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _hhmm(ts: int) -> str:
    t = time.localtime(ts)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


def _seed_orders() -> list[dict]:
    """Build the seed fresh so event dates are always anchored to today
    relative to whenever the operator clicks Reset Dummy Data."""

    def order(
        idx: int, client_name: str, client_name_ar: str,
        client_phone: str, client_email: str,
        event_offset_days: int, event_hour: int,
        event_type: str, package: str, venue: str, guests: int,
        items: list[dict],
        per_guest: float,
        status: str, payment: str,
        dietary: list[str], allergens: list[str],
        notes: str,
        created_days_ago: int,
    ) -> dict:
        event_ts = _days_from_now(event_offset_days, event_hour)
        subtotal = round(per_guest * guests, 2)
        items_total = round(sum(i["qty"] * i["unit_price"] for i in items), 2)
        # If line items present, use the line-items total; else
        # per-guest × headcount.
        gross = items_total if items_total > 0 else subtotal
        vat = round(gross * 0.15, 2)
        delivery_fee = 150.0 if event_offset_days >= 0 else 0.0
        total = round(gross + vat + delivery_fee, 2)
        # Deposit convention: 30% of total at confirmation.
        deposit_due = round(total * 0.30, 2)
        deposit_paid = (
            deposit_due if payment in ("deposit_paid", "paid_in_full") else 0.0
        )
        amount_paid = (
            total            if payment == "paid_in_full" else
            deposit_paid     if payment == "deposit_paid" else
            0.0
        )
        created = int(time.time() - created_days_ago * 86400)
        return {
            "id":              f"CAT-{idx:04d}",
            "client_name":     client_name,
            "client_name_ar":  client_name_ar,
            "client_phone":    client_phone,
            "client_email":    client_email,
            "client_company":  "",
            "event_date":      _iso(event_ts),
            "event_time":      _hhmm(event_ts),
            "event_type":      event_type,
            "package":         package,
            "venue":           venue,
            "guests":          guests,
            "items":           [{**i, "currency": DEFAULT_CURRENCY} for i in items],
            "per_guest_price": per_guest,
            "subtotal":        gross,
            "vat":             vat,
            "delivery_fee":    delivery_fee,
            "deposit_due":     deposit_due,
            "deposit_paid":    deposit_paid,
            "amount_paid":     amount_paid,
            "total":           total,
            "currency":        DEFAULT_CURRENCY,
            "status":          status,
            "payment_status":  payment,
            "dietary":         dietary,
            "allergens":       allergens,
            "notes":           notes,
            "created_at":      created,
        }

    return [
        order(1, "Khalid Al-Otaibi", "خالد العتيبي",
              "+966 50 123 4567", "khalid@example.com",
              7, 9, "corporate", "box_lunch",
              "Riyadh Tower, 18th floor", 25,
              [
                  {"name_en": "Chicken Shawarma Plate", "name_ar": "صحن شاورما دجاج", "qty": 25, "unit_price": 55.00},
                  {"name_en": "Hummus & Mutabbal",      "name_ar": "حمّص ومتبّل",      "qty": 25, "unit_price": 18.00},
                  {"name_en": "Mint Lemonade",          "name_ar": "ليمون نعناع",      "qty": 25, "unit_price": 14.00},
              ],
              per_guest=87.00,
              status="confirmed", payment="deposit_paid",
              dietary=[], allergens=["sesame"],
              notes="Boardroom — quiet drop-off through service lift.",
              created_days_ago=3),
        order(2, "Aisha Al-Harbi", "عائشة الحربي",
              "+966 55 987 6543", "aisha.h@example.com",
              21, 20, "wedding", "plated_dinner",
              "Al-Faisaliah Ballroom, Riyadh", 200,
              [
                  {"name_en": "3-course plated dinner",  "name_ar": "عشاء بثلاث أطباق",  "qty": 200, "unit_price": 245.00},
                  {"name_en": "Welcome cocktail station", "name_ar": "محطة استقبال",       "qty": 1,   "unit_price": 3500.00},
                  {"name_en": "Dessert table",            "name_ar": "طاولة حلويات",      "qty": 1,   "unit_price": 4500.00},
              ],
              per_guest=245.00,
              status="confirmed", payment="deposit_paid",
              dietary=[], allergens=["wheat"],
              notes="Bride is celiac — strict gluten-free on the head-table service.",
              created_days_ago=45),
        order(3, "Bandar Holdings", "شركة البندر القابضة",
              "+966 11 234 5678", "events@bandar.example",
              0, 13, "corporate", "buffet",
              "Bandar HQ — Riyadh", 80,
              [
                  {"name_en": "Lebanese buffet (full)",  "name_ar": "بوفيه لبناني كامل", "qty": 80, "unit_price": 145.00},
              ],
              per_guest=145.00,
              status="in_kitchen", payment="paid_in_full",
              dietary=[], allergens=[],
              notes="Setup at 11:30, lunch served 13:00-14:30. Buffet teardown 15:00.",
              created_days_ago=6),
        order(4, "Hilton Riyadh Conference", "مؤتمر هيلتون الرياض",
              "+966 11 555 9988", "fb@hilton-rh.example",
              0, 10, "conference", "box_lunch",
              "Hilton Riyadh — Sheraton Ballroom", 120,
              [
                  {"name_en": "Box lunch (gluten-free)", "name_ar": "صندوق غذاء (خالي من الجلوتين)", "qty": 30, "unit_price": 95.00},
                  {"name_en": "Box lunch (vegetarian)",  "name_ar": "صندوق غذاء نباتي",              "qty": 40, "unit_price": 85.00},
                  {"name_en": "Box lunch (standard)",    "name_ar": "صندوق غذاء عادي",                "qty": 50, "unit_price": 80.00},
              ],
              per_guest=85.00,
              status="out_for_delivery", payment="paid_in_full",
              dietary=[], allergens=["wheat"],
              notes="30 GF boxes need to be sealed and clearly labelled.",
              created_days_ago=5),
        order(5, "Layla Al-Qahtani", "ليلى القحطاني",
              "+966 56 333 7788", "layla.q@example.com",
              -1, 19, "birthday", "cocktail_reception",
              "Private villa — Hittin, Riyadh", 60,
              [
                  {"name_en": "Cocktail reception (12 items/guest)", "name_ar": "كوكتيل ريسبشن (12 صنف)",
                   "qty": 60, "unit_price": 135.00},
                  {"name_en": "Knafeh station (live)",  "name_ar": "محطة كنافة مباشرة", "qty": 1, "unit_price": 1800.00},
              ],
              per_guest=135.00,
              status="delivered", payment="paid_in_full",
              dietary=["vegetarian"], allergens=["peanut"],
              notes="One guest has severe peanut allergy — kitchen lead briefed.",
              created_days_ago=14),
        order(6, "King Abdulaziz School", "مدرسة الملك عبدالعزيز",
              "+966 11 880 0011", "events@kas.example",
              -14, 11, "graduation", "buffet",
              "School main hall — Riyadh", 350,
              [
                  {"name_en": "Graduation buffet",  "name_ar": "بوفيه التخرّج",  "qty": 350, "unit_price": 88.00},
                  {"name_en": "Cake (3 tier)",      "name_ar": "كيكة 3 طبقات",   "qty": 1,   "unit_price": 2400.00},
              ],
              per_guest=88.00,
              status="completed", payment="paid_in_full",
              dietary=[], allergens=[],
              notes="Tip submitted post-event — ask for testimonial.",
              created_days_ago=35),
        order(7, "Omar Al-Bandar", "عمر البندر",
              "+966 54 555 8899", "obandar@example.com",
              5, 13, "ramadan_iftar", "buffet",
              "Family majlis — Riyadh", 45,
              [
                  {"name_en": "Iftar buffet (premium)", "name_ar": "بوفيه إفطار فاخر", "qty": 45, "unit_price": 175.00},
              ],
              per_guest=175.00,
              status="confirmed", payment="deposit_paid",
              dietary=[], allergens=[],
              notes="Sunset 18:32 — table must be set by 18:00.",
              created_days_ago=10),
        order(8, "TechHub Riyadh", "تك هاب الرياض",
              "+966 11 444 7700", "ops@techhub.example",
              14, 18, "corporate", "cocktail_reception",
              "TechHub Diriyah office", 150,
              [],   # inquiry stage — no line items yet
              per_guest=125.00,
              status="inquiry", payment="unpaid",
              dietary=[], allergens=[],
              notes="Product launch — want a Live-Cooking station option.",
              created_days_ago=2),
        order(9, "Sara Al-Mutairi", "سارة المطيري",
              "+966 53 888 9911", "sara.m@example.com",
              28, 19, "wedding", "tea_ceremony",
              "Family villa — Diriyah, Riyadh", 80,
              [
                  {"name_en": "Saudi qahwa & dates", "name_ar": "قهوة وتمر",  "qty": 80, "unit_price": 32.00},
                  {"name_en": "Mezze platter",       "name_ar": "صحن مقبلات", "qty": 80, "unit_price": 65.00},
                  {"name_en": "Knafeh nabulsia",     "name_ar": "كنافة نابلسية", "qty": 80, "unit_price": 28.00},
              ],
              per_guest=125.00,
              status="confirmed", payment="deposit_paid",
              dietary=[], allergens=["tree-nut"],
              notes="Henna night — no music; modest service.",
              created_days_ago=22),
        order(10, "PIF Office", "صندوق الاستثمارات العامة",
              "+966 11 200 3344", "ext-fb@pif.example",
              3, 12, "corporate", "plated_dinner",
              "PIF HQ — King Abdullah Financial District", 40,
              [
                  {"name_en": "Executive plated lunch", "name_ar": "غداء تنفيذي بثلاث أطباق", "qty": 40, "unit_price": 280.00},
              ],
              per_guest=280.00,
              status="confirmed", payment="deposit_paid",
              dietary=["low-sodium"], allergens=[],
              notes="VIP — silver service, table linens in firm's brand colours.",
              created_days_ago=8),
        order(11, "Faisal Al-Dosari", "فيصل الدوسري",
              "+966 56 444 7700", "faisal.d@example.com",
              -6, 20, "birthday", "buffet",
              "Faisal's residence — Al-Murabba, Riyadh", 35,
              [
                  {"name_en": "Mixed Lebanese buffet", "name_ar": "بوفيه لبناني مشكّل", "qty": 35, "unit_price": 110.00},
              ],
              per_guest=110.00,
              status="cancelled", payment="refunded",
              dietary=[], allergens=[],
              notes="Cancelled 48h prior — refund processed minus 10% admin.",
              created_days_ago=21),
        order(12, "Noura Al-Saleh", "نورة الصالح",
              "+966 55 234 5678", "noura.s@example.com",
              42, 13, "reception", "plated_dinner",
              "Four Seasons Riyadh — Al-Faisaliah Suite", 90,
              [
                  {"name_en": "Bridal-shower plated brunch", "name_ar": "إفطار حفل عروس مع أطباق",
                   "qty": 90, "unit_price": 195.00},
              ],
              per_guest=195.00,
              status="inquiry", payment="unpaid",
              dietary=["vegetarian"], allergens=[],
              notes="Wants 2 vegan options + 1 dairy-free dessert.",
              created_days_ago=1),
    ]


def _default_payload() -> dict:
    return {"orders": _seed_orders()}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_orders() -> list[dict]:
    """Every order, newest event-date first."""
    with _LOCK:
        rows = _load_locked().get("orders") or []
    rows.sort(key=lambda r: r.get("event_date") or "", reverse=True)
    return rows


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("catering_orders.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    rows = d.get("orders")
    return {"orders": [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_orders() -> list[dict]:
    """Restore the 12-order seed with event dates anchored to NOW."""
    with _LOCK:
        _save_locked(_default_payload())
    return load_orders()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    """Raised when the order is malformed."""


def _next_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("CAT-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"CAT-{n + 1:04d}"


def _coerce_item(it: dict) -> dict:
    qty   = max(0, int(it.get("qty") or 0))
    price = round(max(0.0, float(it.get("unit_price") or 0)), 2)
    return {
        "name_en":    str(it.get("name_en") or "").strip()[:200],
        "name_ar":    str(it.get("name_ar") or "").strip()[:200],
        "qty":        qty,
        "unit_price": price,
        "currency":   (str(it.get("currency") or DEFAULT_CURRENCY).strip()
                        or DEFAULT_CURRENCY)[:8],
    }


def _coerce_order(o: dict, existing: list[dict]) -> dict:
    items = o.get("items") or []
    if not isinstance(items, list): items = []
    diet  = o.get("dietary") or []
    if not isinstance(diet, list): diet = []
    alg   = o.get("allergens") or []
    if not isinstance(alg, list): alg = []

    status = str(o.get("status") or "inquiry").strip().lower()
    if status not in ORDER_STATUSES: status = "inquiry"
    payment = str(o.get("payment_status") or "unpaid").strip().lower()
    if payment not in PAYMENT_STATUSES: payment = "unpaid"

    coerced_items = [_coerce_item(i) for i in items if isinstance(i, dict)]
    items_total = round(sum(i["qty"] * i["unit_price"] for i in coerced_items), 2)
    guests = max(0, int(o.get("guests") or 0))
    per_guest = round(max(0.0, float(o.get("per_guest_price") or 0)), 2)

    # Subtotal: prefer line-items if present, else per-guest × guests.
    subtotal = items_total if items_total > 0 else round(per_guest * guests, 2)
    vat = round(subtotal * 0.15, 2)
    delivery_fee = round(max(0.0, float(o.get("delivery_fee") or 0)), 2)
    total = round(subtotal + vat + delivery_fee, 2)
    deposit_due = round(total * 0.30, 2)
    deposit_paid = round(max(0.0, float(o.get("deposit_paid") or 0)), 2)
    amount_paid = (
        total           if payment == "paid_in_full" else
        deposit_paid    if payment == "deposit_paid" else
        round(max(0.0, float(o.get("amount_paid") or 0)), 2)
    )

    return {
        "id":              str(o.get("id") or _next_id(existing)).strip(),
        "client_name":     str(o.get("client_name") or "").strip()[:200],
        "client_name_ar":  str(o.get("client_name_ar") or "").strip()[:200],
        "client_phone":    _PHONE_TIDY.sub("", str(o.get("client_phone") or "")).strip()[:32],
        "client_email":    str(o.get("client_email") or "").strip()[:200],
        "client_company":  str(o.get("client_company") or "").strip()[:200],
        "event_date":      str(o.get("event_date") or "").strip()[:10],
        "event_time":      str(o.get("event_time") or "").strip()[:5],
        "event_type":      str(o.get("event_type") or "other").strip().lower()[:32],
        "package":         str(o.get("package") or "custom").strip().lower()[:32],
        "venue":           str(o.get("venue") or "").strip()[:300],
        "guests":          guests,
        "items":           coerced_items,
        "per_guest_price": per_guest,
        "subtotal":        subtotal,
        "vat":             vat,
        "delivery_fee":    delivery_fee,
        "deposit_due":     deposit_due,
        "deposit_paid":    deposit_paid,
        "amount_paid":     amount_paid,
        "total":           total,
        "currency":        (str(o.get("currency") or DEFAULT_CURRENCY).strip()
                             or DEFAULT_CURRENCY)[:8],
        "status":          status,
        "payment_status":  payment,
        # "halal" is stripped — every meal we serve is halal by default
        # (Saudi context), so tagging individual orders as halal is noise.
        "dietary":         [d for d in (str(x).strip().lower()[:32] for x in diet)
                              if d and d != "halal"],
        "allergens":       [str(a).strip().lower()[:32] for a in alg if str(a).strip()],
        "notes":           str(o.get("notes") or "").strip()[:1000],
        "created_at":      int(o.get("created_at") or time.time()),
    }


def _validate(patch: dict) -> None:
    if not (patch.get("client_name") or patch.get("client_name_ar") or "").strip():
        raise _Refused("Client name (EN or AR) is required.")
    if not (patch.get("event_date") or "").strip():
        raise _Refused("event_date is required (YYYY-MM-DD).")


def add_order(patch: dict) -> dict:
    _validate(patch)
    with _LOCK:
        payload = _load_locked()
        o = _coerce_order(patch, payload["orders"])
        payload["orders"].append(o)
        _save_locked(payload)
    return o


def update_order(order_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, o in enumerate(payload["orders"]):
            if o.get("id") == order_id:
                merged = {**o, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = order_id
                if not (merged.get("client_name") or merged.get("client_name_ar") or "").strip():
                    raise _Refused("Client name (EN or AR) is required.")
                payload["orders"][i] = _coerce_order(merged, payload["orders"])
                _save_locked(payload)
                return payload["orders"][i]
    return None


def delete_order(order_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["orders"])
        payload["orders"] = [o for o in payload["orders"] if o.get("id") != order_id]
        removed = len(payload["orders"]) < before
        if removed:
            _save_locked(payload)
        return removed
