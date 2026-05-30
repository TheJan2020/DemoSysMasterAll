"""
Closed-invoice ledger for the restaurant Overview page.

Storage:
    data/demos/restaurant/invoices.json

Tracked by git (operator-edited content); the `.tmp` from atomic writes
is gitignored.

Schema:
    {
      "invoices": [
        {
          "id":             "INV-0001",
          "table_number":   "T03",
          "closed_at":      <unix-seconds>,
          "server_name":    "Omar",
          "guests":         4,
          "items": [
            { "name_en", "name_ar", "category_id",
              "qty": int, "price": float, "currency": "SAR" }
          ],
          "subtotal":       float,
          "tax":            float,   // 15% Saudi VAT
          "tip":            float,
          "total":          float,
          "currency":       "SAR",
          "payment_method": "card" | "cash" | "mada"
        }
      ]
    }

Reset Dummy Data rebuilds the seed with `closed_at` timestamps anchored
to NOW so "today" always means today regardless of when the operator
last reset.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.invoices")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_INVOICES_PATH = _DATA_DIR / "invoices.json"

_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"
VAT_RATE = 0.15   # Saudi VAT


# ----------------------------------------------------------------------
# Seed: 14 closed invoices spread across today, with a healthy mix of
# items across every F&B Menu category so the per-category chart on the
# Overview page actually looks meaningful.
# ----------------------------------------------------------------------

def _today_at(hour: int, minute: int = 0) -> int:
    """Return a unix timestamp for today at the given local hour:minute.
    If that time is in the future relative to now (e.g. seeding at 11:00
    with a target of 14:00), roll back to yesterday so all invoices
    truly belong to a closed business day."""
    now = time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                          hour, minute, 0,
                          0, 0, now.tm_isdst))
    nowts = time.time()
    if target > nowts:
        target -= 86400  # yesterday's same hour
    return int(target)


def _seed_invoices() -> list[dict]:
    """Build the closed-invoice seed fresh from NOW. Times use real
    service hours (12:00 → 23:00) so the Revenue table looks credible."""

    def inv(idx: int, table: str, hour: int, minute: int, server: str,
            guests: int, payment: str, items: list[dict]) -> dict:
        subtotal = round(sum(i["qty"] * i["price"] for i in items), 2)
        tax      = round(subtotal * VAT_RATE, 2)
        tip      = round(subtotal * 0.05, 2) if payment != "cash" else 0.0
        total    = round(subtotal + tax + tip, 2)
        return {
            "id":             f"INV-{idx:04d}",
            "table_number":   table,
            "closed_at":      _today_at(hour, minute),
            "server_name":    server,
            "guests":         guests,
            "items":          [{**i, "currency": DEFAULT_CURRENCY} for i in items],
            "subtotal":       subtotal,
            "tax":            tax,
            "tip":            tip,
            "total":          total,
            "currency":       DEFAULT_CURRENCY,
            "payment_method": payment,
        }

    # Item dicts reference categories by the F&B Menu's CAT-* ids. The
    # Overview page uses these to bucket items per category for the
    # bar chart.
    return [
        inv(1, "T02", 12, 35, "Layla", 2, "card", [
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 1, "price": 18.00},
            {"name_en": "Fattoush",        "name_ar": "فتّوش",        "category_id": "CAT-SALAD", "qty": 1, "price": 24.00},
            {"name_en": "Shish Tawook",    "name_ar": "شيش طاووق",  "category_id": "CAT-GRILL", "qty": 2, "price": 65.00},
            {"name_en": "Mint Lemonade",   "name_ar": "ليمون نعناع",  "category_id": "CAT-BEV",   "qty": 2, "price": 18.00},
        ]),
        inv(2, "T01", 13, 5, "Omar", 4, "mada", [
            {"name_en": "Mutabbal",        "name_ar": "متبّل",        "category_id": "CAT-COLD",  "qty": 1, "price": 22.00},
            {"name_en": "Tabbouleh",       "name_ar": "تبّولة",        "category_id": "CAT-SALAD", "qty": 1, "price": 25.00},
            {"name_en": "Kibbeh (4 pcs)",  "name_ar": "كبّة (4 قطع)",  "category_id": "CAT-HOT",   "qty": 1, "price": 28.00},
            {"name_en": "Mixed Grill",     "name_ar": "مشاوي مشكّلة",   "category_id": "CAT-GRILL", "qty": 1, "price": 110.00},
            {"name_en": "Ayran",           "name_ar": "عيران",        "category_id": "CAT-BEV",   "qty": 4, "price": 12.00},
            {"name_en": "Baklava (6 pcs)", "name_ar": "بقلاوة",        "category_id": "CAT-DESS",  "qty": 1, "price": 28.00},
        ]),
        inv(3, "T11", 13, 25, "Hadi", 2, "cash", [
            {"name_en": "Falafel (6 pcs)", "name_ar": "فلافل (6 قطع)", "category_id": "CAT-HOT",   "qty": 1, "price": 22.00},
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 1, "price": 18.00},
            {"name_en": "Mint Tea",        "name_ar": "شاي بالنعناع",  "category_id": "CAT-BEV",   "qty": 2, "price": 12.00},
        ]),
        inv(4, "T06", 13, 50, "Layla", 6, "card", [
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 2, "price": 18.00},
            {"name_en": "Muhammara",       "name_ar": "محمّرة",        "category_id": "CAT-COLD",  "qty": 1, "price": 24.00},
            {"name_en": "Sambousek (cheese)", "name_ar": "سمبوسك بالجبن", "category_id": "CAT-HOT", "qty": 1, "price": 24.00},
            {"name_en": "Lamb Kafta",      "name_ar": "كفتة لحم",     "category_id": "CAT-GRILL", "qty": 2, "price": 72.00},
            {"name_en": "Shish Tawook",    "name_ar": "شيش طاووق",  "category_id": "CAT-GRILL", "qty": 1, "price": 65.00},
            {"name_en": "Knafeh Nabulsia", "name_ar": "كنافة نابلسية",  "category_id": "CAT-DESS",  "qty": 2, "price": 32.00},
            {"name_en": "Jallab",          "name_ar": "جلاب",         "category_id": "CAT-BEV",   "qty": 4, "price": 16.00},
        ]),
        inv(5, "T04", 14, 10, "Omar", 3, "mada", [
            {"name_en": "Labneh",          "name_ar": "لبنة",         "category_id": "CAT-COLD",  "qty": 1, "price": 20.00},
            {"name_en": "Arayes",          "name_ar": "عرايس",        "category_id": "CAT-HOT",   "qty": 1, "price": 32.00},
            {"name_en": "Lamb Chops (4 pcs)", "name_ar": "ريش الغنم", "category_id": "CAT-GRILL", "qty": 1, "price": 95.00},
            {"name_en": "Arabic Coffee",   "name_ar": "قهوة عربية",   "category_id": "CAT-BEV",   "qty": 3, "price": 14.00},
        ]),
        inv(6, "T14", 14, 40, "Hadi", 2, "card", [
            {"name_en": "Rocket & Halloumi", "name_ar": "جرجير وحلوم", "category_id": "CAT-SALAD", "qty": 1, "price": 32.00},
            {"name_en": "Chicken Shawarma Plate", "name_ar": "صحن شاورما دجاج", "category_id": "CAT-GRILL", "qty": 1, "price": 55.00},
            {"name_en": "Mint Lemonade",   "name_ar": "ليمون نعناع",  "category_id": "CAT-BEV",   "qty": 2, "price": 18.00},
        ]),
        inv(7, "T05", 15, 0, "Layla", 2, "card", [
            {"name_en": "Tabbouleh",       "name_ar": "تبّولة",        "category_id": "CAT-SALAD", "qty": 1, "price": 25.00},
            {"name_en": "Falafel (6 pcs)", "name_ar": "فلافل (6 قطع)", "category_id": "CAT-HOT",   "qty": 1, "price": 22.00},
            {"name_en": "Mhalabia",        "name_ar": "مهلبية",        "category_id": "CAT-DESS",  "qty": 2, "price": 22.00},
            {"name_en": "Ayran",           "name_ar": "عيران",        "category_id": "CAT-BEV",   "qty": 2, "price": 12.00},
        ]),
        inv(8, "T08", 15, 30, "Omar", 2, "cash", [
            {"name_en": "Warak Enab (cold)", "name_ar": "ورق عنب بزيت", "category_id": "CAT-COLD",  "qty": 1, "price": 26.00},
            {"name_en": "Sayadieh",        "name_ar": "صيادية",        "category_id": "CAT-MAIN",  "qty": 1, "price": 85.00},
            {"name_en": "Arabic Coffee",   "name_ar": "قهوة عربية",   "category_id": "CAT-BEV",   "qty": 2, "price": 14.00},
        ]),
        inv(9, "T18", 16, 5, "Hadi", 8, "card", [
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 2, "price": 18.00},
            {"name_en": "Mutabbal",        "name_ar": "متبّل",        "category_id": "CAT-COLD",  "qty": 1, "price": 22.00},
            {"name_en": "Fatayer (spinach)", "name_ar": "فطاير سبانخ", "category_id": "CAT-HOT",   "qty": 2, "price": 22.00},
            {"name_en": "Beetroot Salad",  "name_ar": "سلطة الشمندر",  "category_id": "CAT-SALAD", "qty": 1, "price": 28.00},
            {"name_en": "Ouzi",            "name_ar": "أوزي",          "category_id": "CAT-MAIN",  "qty": 1, "price": 95.00},
            {"name_en": "Mansaf",          "name_ar": "منسف",         "category_id": "CAT-MAIN",  "qty": 1, "price": 88.00},
            {"name_en": "Mixed Grill",     "name_ar": "مشاوي مشكّلة",   "category_id": "CAT-GRILL", "qty": 1, "price": 110.00},
            {"name_en": "Baklava (6 pcs)", "name_ar": "بقلاوة",         "category_id": "CAT-DESS",  "qty": 2, "price": 28.00},
            {"name_en": "Mint Tea",        "name_ar": "شاي بالنعناع",  "category_id": "CAT-BEV",   "qty": 6, "price": 12.00},
        ]),
        inv(10, "T15", 16, 45, "Layla", 4, "mada", [
            {"name_en": "Muhammara",       "name_ar": "محمّرة",        "category_id": "CAT-COLD",  "qty": 1, "price": 24.00},
            {"name_en": "Kibbeh (4 pcs)",  "name_ar": "كبّة (4 قطع)",  "category_id": "CAT-HOT",   "qty": 1, "price": 28.00},
            {"name_en": "Shish Tawook",    "name_ar": "شيش طاووق",  "category_id": "CAT-GRILL", "qty": 2, "price": 65.00},
            {"name_en": "Atayef Bil Jibneh", "name_ar": "قطايف بالجبنة", "category_id": "CAT-DESS", "qty": 1, "price": 26.00},
            {"name_en": "Jallab",          "name_ar": "جلاب",         "category_id": "CAT-BEV",   "qty": 4, "price": 16.00},
        ]),
        inv(11, "T17", 17, 20, "Hadi", 3, "card", [
            {"name_en": "Tabbouleh",       "name_ar": "تبّولة",        "category_id": "CAT-SALAD", "qty": 1, "price": 25.00},
            {"name_en": "Lamb Chops (4 pcs)", "name_ar": "ريش الغنم", "category_id": "CAT-GRILL", "qty": 1, "price": 95.00},
            {"name_en": "Mhalabia",        "name_ar": "مهلبية",        "category_id": "CAT-DESS",  "qty": 1, "price": 22.00},
            {"name_en": "Mint Lemonade",   "name_ar": "ليمون نعناع",  "category_id": "CAT-BEV",   "qty": 3, "price": 18.00},
        ]),
        inv(12, "T10", 18, 0, "Hadi", 2, "card", [
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 1, "price": 18.00},
            {"name_en": "Chicken Shawarma Plate", "name_ar": "صحن شاورما دجاج", "category_id": "CAT-GRILL", "qty": 1, "price": 55.00},
            {"name_en": "Knafeh Nabulsia", "name_ar": "كنافة نابلسية",  "category_id": "CAT-DESS",  "qty": 1, "price": 32.00},
            {"name_en": "Arabic Coffee",   "name_ar": "قهوة عربية",   "category_id": "CAT-BEV",   "qty": 2, "price": 14.00},
        ]),
        inv(13, "T16", 18, 30, "Omar", 2, "mada", [
            {"name_en": "Labneh",          "name_ar": "لبنة",         "category_id": "CAT-COLD",  "qty": 1, "price": 20.00},
            {"name_en": "Sambousek (cheese)", "name_ar": "سمبوسك بالجبن", "category_id": "CAT-HOT", "qty": 1, "price": 24.00},
            {"name_en": "Mahshi Mixed",    "name_ar": "محشي مشكّل",     "category_id": "CAT-MAIN",  "qty": 1, "price": 68.00},
            {"name_en": "Mint Tea",        "name_ar": "شاي بالنعناع",  "category_id": "CAT-BEV",   "qty": 2, "price": 12.00},
        ]),
        inv(14, "T13", 19, 0, "Layla", 4, "card", [
            {"name_en": "Hummus",          "name_ar": "حمّص",        "category_id": "CAT-COLD",  "qty": 1, "price": 18.00},
            {"name_en": "Mutabbal",        "name_ar": "متبّل",        "category_id": "CAT-COLD",  "qty": 1, "price": 22.00},
            {"name_en": "Fattoush",        "name_ar": "فتّوش",        "category_id": "CAT-SALAD", "qty": 1, "price": 24.00},
            {"name_en": "Arayes",          "name_ar": "عرايس",        "category_id": "CAT-HOT",   "qty": 1, "price": 32.00},
            {"name_en": "Lamb Kafta",      "name_ar": "كفتة لحم",     "category_id": "CAT-GRILL", "qty": 1, "price": 72.00},
            {"name_en": "Shish Tawook",    "name_ar": "شيش طاووق",  "category_id": "CAT-GRILL", "qty": 1, "price": 65.00},
            {"name_en": "Baklava (6 pcs)", "name_ar": "بقلاوة",         "category_id": "CAT-DESS",  "qty": 1, "price": 28.00},
            {"name_en": "Mint Lemonade",   "name_ar": "ليمون نعناع",  "category_id": "CAT-BEV",   "qty": 4, "price": 18.00},
        ]),
    ]


def _default_payload() -> dict:
    return {"invoices": _seed_invoices()}


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def load_invoices() -> list[dict]:
    """Return the full list of closed invoices sorted newest-first."""
    with _LOCK:
        rows = _load_locked().get("invoices") or []
    rows.sort(key=lambda r: r.get("closed_at") or 0, reverse=True)
    return rows


def _load_locked() -> dict:
    if not _INVOICES_PATH.exists():
        return _default_payload()
    try:
        data = json.loads(_INVOICES_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("invoices.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(data, dict):
        return _default_payload()
    inv = data.get("invoices")
    if not isinstance(inv, list):
        return _default_payload()
    return {"invoices": [r for r in inv if isinstance(r, dict)]}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INVOICES_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(_INVOICES_PATH)
    return payload


def reset_invoices() -> list[dict]:
    """Restore the Lebanese-restaurant invoice seed with NOW-anchored
    `closed_at` timestamps so the Revenue table always reflects today."""
    with _LOCK:
        _save_locked(_default_payload())
    return load_invoices()


def invoices_for_today() -> list[dict]:
    """Filter to invoices closed since local midnight."""
    now = time.localtime()
    midnight = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                 0, 0, 0, 0, 0, now.tm_isdst)))
    return [r for r in load_invoices() if (r.get("closed_at") or 0) >= midnight]


# ----------------------------------------------------------------------
# Create invoice — used by the Orders/POS page when an operator closes
# an open table tab.
# ----------------------------------------------------------------------

def _next_invoice_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("INV-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"INV-{n + 1:04d}"


def add_invoice(patch: dict) -> dict:
    """Append a new closed invoice. Subtotal/tax/total are recomputed
    from `items` so the caller doesn't have to (just pass the qty +
    price + optional tip_pct + payment_method)."""
    items = patch.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("Invoice needs at least one item.")
    norm_items: list[dict] = []
    subtotal = 0.0
    for it in items:
        if not isinstance(it, dict):
            continue
        qty = max(0, int(it.get("qty") or 0))
        price = round(max(0.0, float(it.get("price") or 0)), 2)
        if qty <= 0:
            continue
        line = {
            "name_en":     str(it.get("name_en") or "").strip()[:200],
            "name_ar":     str(it.get("name_ar") or "").strip()[:200],
            "category_id": str(it.get("category_id") or "").strip()[:32],
            "qty":         qty,
            "price":       price,
            "currency":    DEFAULT_CURRENCY,
        }
        norm_items.append(line)
        subtotal += qty * price
    if not norm_items:
        raise ValueError("Invoice has no priced items.")

    payment = str(patch.get("payment_method") or "card").strip().lower()
    if payment not in ("card", "cash", "mada"):
        payment = "card"

    tip_pct = float(patch.get("tip_pct") or 0)
    # Operators typically don't add tip on cash drawer rings; mirror seed.
    if payment == "cash":
        tip_pct = 0.0
    subtotal = round(subtotal, 2)
    tax      = round(subtotal * VAT_RATE, 2)
    tip      = round(subtotal * (tip_pct / 100.0), 2)
    total    = round(subtotal + tax + tip, 2)

    with _LOCK:
        payload = _load_locked()
        inv = {
            "id":             _next_invoice_id(payload["invoices"]),
            "table_number":   str(patch.get("table_number") or "").strip()[:16],
            "closed_at":      int(patch.get("closed_at") or time.time()),
            "server_name":    str(patch.get("server_name") or "").strip()[:200],
            "guests":         max(0, int(patch.get("guests") or 0)),
            "items":          norm_items,
            "subtotal":       subtotal,
            "tax":            tax,
            "tip":            tip,
            "total":          total,
            "currency":       DEFAULT_CURRENCY,
            "payment_method": payment,
        }
        payload["invoices"].append(inv)
        _save_locked(payload)
        return inv
