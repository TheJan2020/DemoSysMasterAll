"""
Catering — pre-defined packages.

A package bundles catering items into a reusable template the operator
can quote from in one click. Each package targets an event type
(wedding, corporate, ramadan_iftar, …), spans a guest-count range, and
carries a per-guest price the sales team uses as a starting point.

Items inside a package reference `catering_menu.py` by `code` so the
catalog stays the single source of truth for price + allergens. We do
NOT denormalise the menu item's price into the package row.

Storage:
    data/demos/restaurant/catering_packages.json

Tracked by git (operator content); `.tmp` from atomic writes is
gitignored.
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.catering_packages")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "catering_packages.json"
_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

# Event-type values must match catering_orders.EVENT_TYPES so the SPA's
# "Use this package" action can drop the package straight into an
# order without re-mapping.
EVENT_TYPES = ["wedding", "corporate", "birthday", "graduation",
                "conference", "reception", "ramadan_iftar", "other"]

SERVICE_STYLES = [
    "plated",            # 3-course served at table
    "buffet",            # operator-managed buffet line
    "stations",          # multiple live + static stations
    "box",               # individually packed boxes
    "tea_ceremony",      # qahwa + mezze service
    "cocktail",          # passed canapés + bar service
]


# ----------------------------------------------------------------------
# Seed — 8 Lebanese-restaurant catering packages tuned to the Saudi
# market. Items reference catering_menu by `code`.
# ----------------------------------------------------------------------

DEFAULT_PACKAGES: list[dict] = [
    {
        "id": "PKG-001",
        "code": "WEDDING-PLATED-200",
        "name_en": "Wedding Plated Dinner — Grand",
        "name_ar": "عشاء زفاف فاخر بأطباق منفردة",
        "description_en": "3-course plated dinner with welcome mezze, "
                          "premium mains, and a live knafeh dessert station. "
                          "Includes service captain + 8 waitstaff.",
        "description_ar": "عشاء زفاف بثلاثة أطباق مع مقبلات استقبال "
                          "ومحطة كنافة مباشرة. يشمل كابتن خدمة وثمانية نادلين.",
        "event_type": "wedding",
        "service_style": "plated",
        "min_guests": 120,
        "max_guests": 300,
        "per_guest_price": 285.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 14,
        "items": [
            {"code": "MEZZE-COLD-LG",   "qty_basis": "per_block",
              "block_size": 25, "note": "1 platter per table of 25"},
            {"code": "MANSAF-TRAY",     "qty_basis": "per_block",
              "block_size": 12, "note": "Family-style on banquet tables"},
            {"code": "STATION-KNAFEH",  "qty_basis": "flat", "qty": 1,
              "note": "Live dessert station"},
            {"code": "STAFF-CAPTAIN",   "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-WAITER",    "qty_basis": "per_block",
              "block_size": 25, "note": "1 waiter per 25 guests"},
            {"code": "LINENS-BASIC",    "qty_basis": "per_block",
              "block_size": 10, "note": "1 set per banquet table"},
        ],
        "included_diet_tags": [],
        "tier": "premium",
        "popular": True,
        "active": True,
        "sort_order": 1,
    },
    {
        "id": "PKG-002",
        "code": "WEDDING-BUFFET-150",
        "name_en": "Wedding Buffet — Lebanese Spread",
        "name_ar": "بوفيه زفاف — مائدة لبنانية",
        "description_en": "Full Lebanese buffet with cold + hot mezze, "
                          "two mains, grill station, dessert tray. "
                          "Captain + 6 waitstaff included.",
        "description_ar": "بوفيه لبناني كامل مع مقبلات باردة وساخنة، "
                          "طبقين رئيسيين، محطة مشاوي وحلويات. يشمل "
                          "كابتن وستة نادلين.",
        "event_type": "wedding",
        "service_style": "buffet",
        "min_guests": 80,
        "max_guests": 250,
        "per_guest_price": 195.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 10,
        "items": [
            {"code": "MEZZE-COLD-LG",  "qty_basis": "per_block",
              "block_size": 25},
            {"code": "FALAFEL-100",    "qty_basis": "per_block",
              "block_size": 25},
            {"code": "KIBBEH-60",      "qty_basis": "per_block",
              "block_size": 20},
            {"code": "OUZI-TRAY",      "qty_basis": "per_block",
              "block_size": 30},
            {"code": "GRILL-MIXED",    "qty_basis": "flat", "qty": 1},
            {"code": "BAKLAVA-60",     "qty_basis": "per_block",
              "block_size": 30},
            {"code": "STAFF-CAPTAIN",  "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-WAITER",   "qty_basis": "per_block",
              "block_size": 25},
        ],
        "included_diet_tags": [],
        "tier": "standard",
        "popular": True,
        "active": True,
        "sort_order": 2,
    },
    {
        "id": "PKG-003",
        "code": "CORP-BUFFET-LUNCH",
        "name_en": "Corporate Buffet Lunch",
        "name_ar": "بوفيه غداء للشركات",
        "description_en": "Mid-tier Lebanese buffet for office events. "
                          "Mezze + 2 mains + dessert. Drop-off or onsite "
                          "service with 2 waitstaff.",
        "description_ar": "بوفيه لبناني للشركات. مقبلات وطبقين رئيسيين "
                          "وحلوى. توصيل أو خدمة في الموقع مع نادلين.",
        "event_type": "corporate",
        "service_style": "buffet",
        "min_guests": 30,
        "max_guests": 100,
        "per_guest_price": 145.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 3,
        "items": [
            {"code": "MEZZE-COLD-SM",  "qty_basis": "per_block",
              "block_size": 10},
            {"code": "FATTOUSH-LG",    "qty_basis": "per_block",
              "block_size": 15},
            {"code": "KABSA-TRAY",     "qty_basis": "per_block",
              "block_size": 12},
            {"code": "FALAFEL-100",    "qty_basis": "per_block",
              "block_size": 25},
            {"code": "BAKLAVA-60",     "qty_basis": "per_block",
              "block_size": 30},
            {"code": "STAFF-WAITER",   "qty_basis": "per_block",
              "block_size": 40},
        ],
        "included_diet_tags": [],
        "tier": "standard",
        "popular": True,
        "active": True,
        "sort_order": 3,
    },
    {
        "id": "PKG-004",
        "code": "CORP-BOX-LUNCH",
        "name_en": "Corporate Box Lunch",
        "name_ar": "صناديق غداء للشركات",
        "description_en": "Individually packed box lunches — wrap, side, "
                          "dessert, water. GF + vegetarian options. "
                          "Includes drop-off, no onsite staff.",
        "description_ar": "صناديق غداء فردية — لفافة وطبق جانبي وحلوى "
                          "وماء. تشمل خيارات نباتية وخالية من الجلوتين.",
        "event_type": "corporate",
        "service_style": "box",
        "min_guests": 20,
        "max_guests": 250,
        "per_guest_price": 85.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 1,
        "items": [
            {"code": "BOX-STANDARD",   "qty_basis": "per_guest", "qty": 1,
              "note": "Default 80% of guests"},
            {"code": "BOX-GF",         "qty_basis": "per_guest_optional",
              "qty": 0, "note": "Specify GF count separately"},
        ],
        "included_diet_tags": [],
        "tier": "value",
        "popular": True,
        "active": True,
        "sort_order": 4,
    },
    {
        "id": "PKG-005",
        "code": "TEA-CEREMONY-80",
        "name_en": "Tea & Mezze Ceremony",
        "name_ar": "حفل قهوة ومقبلات",
        "description_en": "Saudi qahwa & dates with mezze and knafeh — "
                          "perfect for henna nights, post-wedding, or "
                          "intimate family gatherings. 2-hour service.",
        "description_ar": "قهوة وتمر مع مقبلات وكنافة — مثالي لليلة "
                          "الحنّاء أو لحظات ما بعد العرس أو التجمعات "
                          "العائلية. خدمة لمدة ساعتين.",
        "event_type": "wedding",
        "service_style": "tea_ceremony",
        "min_guests": 30,
        "max_guests": 120,
        "per_guest_price": 125.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 5,
        "items": [
            {"code": "STATION-QAHWA",   "qty_basis": "flat", "qty": 1},
            {"code": "MEZZE-COLD-LG",   "qty_basis": "per_block",
              "block_size": 25},
            {"code": "STATION-KNAFEH",  "qty_basis": "flat", "qty": 1},
            {"code": "BAKLAVA-60",      "qty_basis": "per_block",
              "block_size": 30},
            {"code": "STAFF-WAITER",    "qty_basis": "per_block",
              "block_size": 40},
        ],
        "included_diet_tags": ["vegetarian"],
        "tier": "standard",
        "popular": False,
        "active": True,
        "sort_order": 5,
    },
    {
        "id": "PKG-006",
        "code": "IFTAR-PREMIUM",
        "name_en": "Ramadan Iftar Premium",
        "name_ar": "بوفيه إفطار رمضان فاخر",
        "description_en": "Date + qahwa break, hot mezze, lamb mansaf, "
                          "stuffed lamb ouzi, and traditional sweets. "
                          "Service timed to sunset.",
        "description_ar": "تمر وقهوة لكسر الصيام، مقبلات ساخنة، منسف "
                          "لحم، خروف عوزي، وحلويات تقليدية. خدمة في "
                          "موعد المغرب.",
        "event_type": "ramadan_iftar",
        "service_style": "buffet",
        "min_guests": 30,
        "max_guests": 200,
        "per_guest_price": 175.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 7,
        "items": [
            {"code": "STATION-QAHWA",   "qty_basis": "flat", "qty": 1},
            {"code": "SAMBOUSEK-72",    "qty_basis": "per_block",
              "block_size": 25},
            {"code": "MANSAF-TRAY",     "qty_basis": "per_block",
              "block_size": 12},
            {"code": "OUZI-TRAY",       "qty_basis": "per_block",
              "block_size": 30},
            {"code": "MHALABIA-25",     "qty_basis": "per_block",
              "block_size": 25},
            {"code": "STAFF-CAPTAIN",   "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-WAITER",    "qty_basis": "per_block",
              "block_size": 30},
        ],
        "included_diet_tags": [],
        "tier": "premium",
        "popular": True,
        "active": True,
        "sort_order": 6,
    },
    {
        "id": "PKG-007",
        "code": "COCKTAIL-RECEPTION",
        "name_en": "Cocktail Reception",
        "name_ar": "كوكتيل ريسبشن",
        "description_en": "8-12 passed canapés per guest across cold + hot "
                          "stations. Live shawarma + juice bar. Stand-up "
                          "service with high-tops.",
        "description_ar": "8-12 صنف مقبلات لكل ضيف بين بارد وساخن. محطة "
                          "شاورما مباشرة وعصائر طازجة. خدمة وقوف على "
                          "طاولات عالية.",
        "event_type": "reception",
        "service_style": "cocktail",
        "min_guests": 60,
        "max_guests": 250,
        "per_guest_price": 165.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 5,
        "items": [
            {"code": "MEZZE-COLD-LG",     "qty_basis": "per_block",
              "block_size": 25},
            {"code": "SAMBOUSEK-72",      "qty_basis": "per_block",
              "block_size": 25},
            {"code": "FATAYER-72",        "qty_basis": "per_block",
              "block_size": 25},
            {"code": "STATION-SHAWARMA",  "qty_basis": "flat", "qty": 1},
            {"code": "JUICE-STATION",     "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-CAPTAIN",     "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-WAITER",      "qty_basis": "per_block",
              "block_size": 25},
        ],
        "included_diet_tags": [],
        "tier": "premium",
        "popular": False,
        "active": True,
        "sort_order": 7,
    },
    {
        "id": "PKG-008",
        "code": "GRADUATION-BUFFET",
        "name_en": "Graduation Buffet",
        "name_ar": "بوفيه التخرّج",
        "description_en": "Family-style Lebanese buffet for graduation "
                          "ceremonies — generous portions, kid-friendly "
                          "options, large cake table.",
        "description_ar": "بوفيه لبناني عائلي لحفلات التخرّج — حصص "
                          "وافرة وخيارات للأطفال وطاولة كيك كبيرة.",
        "event_type": "graduation",
        "service_style": "buffet",
        "min_guests": 100,
        "max_guests": 500,
        "per_guest_price": 88.00,
        "currency": DEFAULT_CURRENCY,
        "prep_lead_days": 7,
        "items": [
            {"code": "MEZZE-COLD-LG",    "qty_basis": "per_block",
              "block_size": 25},
            {"code": "FALAFEL-100",      "qty_basis": "per_block",
              "block_size": 25},
            {"code": "KABSA-TRAY",       "qty_basis": "per_block",
              "block_size": 12},
            {"code": "BAKLAVA-60",       "qty_basis": "per_block",
              "block_size": 30},
            {"code": "JUICE-STATION",    "qty_basis": "flat", "qty": 1},
            {"code": "STAFF-WAITER",     "qty_basis": "per_block",
              "block_size": 40},
        ],
        "included_diet_tags": [],
        "tier": "value",
        "popular": True,
        "active": True,
        "sort_order": 8,
    },
]


def _default_payload() -> dict:
    return {"packages": deepcopy(DEFAULT_PACKAGES)}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_packages() -> list[dict]:
    with _LOCK:
        rows = _load_locked().get("packages") or []
    rows.sort(key=lambda r: (r.get("sort_order") or 99,
                              r.get("name_en") or ""))
    return rows


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("catering_packages.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    rows = d.get("packages")
    return {"packages": [r for r in rows if isinstance(r, dict)]
            if isinstance(rows, list) else []}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_packages() -> list[dict]:
    with _LOCK:
        _save_locked(_default_payload())
    return load_packages()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("PKG-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"PKG-{n + 1:03d}"


_QTY_BASES = {"per_guest", "per_guest_optional", "per_block", "flat"}


def _coerce_item(it: dict) -> dict:
    basis = str(it.get("qty_basis") or "flat").strip().lower()
    if basis not in _QTY_BASES:
        basis = "flat"
    return {
        "code":       str(it.get("code") or "").strip().upper()[:48],
        "qty_basis":  basis,
        "qty":        max(0, int(it.get("qty") or 0)),
        "block_size": max(1, int(it.get("block_size") or 1)),
        "note":       str(it.get("note") or "").strip()[:200],
    }


def _coerce(p: dict, existing: list[dict]) -> dict:
    et = str(p.get("event_type") or "other").strip().lower()
    if et not in EVENT_TYPES: et = "other"
    style = str(p.get("service_style") or "buffet").strip().lower()
    if style not in SERVICE_STYLES: style = "buffet"
    tier = str(p.get("tier") or "standard").strip().lower()
    if tier not in ("value", "standard", "premium"): tier = "standard"
    items_raw = p.get("items") or []
    items = [_coerce_item(i) for i in items_raw if isinstance(i, dict)]
    return {
        "id":              str(p.get("id") or _next_id(existing)).strip(),
        "code":            str(p.get("code") or "").strip().upper()[:48],
        "name_en":         str(p.get("name_en") or "").strip()[:200],
        "name_ar":         str(p.get("name_ar") or "").strip()[:200],
        "description_en":  str(p.get("description_en") or "").strip()[:600],
        "description_ar":  str(p.get("description_ar") or "").strip()[:600],
        "event_type":      et,
        "service_style":   style,
        "min_guests":      max(1, int(p.get("min_guests") or 1)),
        "max_guests":      max(1, int(p.get("max_guests") or 1)),
        "per_guest_price": round(max(0.0, float(p.get("per_guest_price") or 0)), 2),
        "currency":        (str(p.get("currency") or DEFAULT_CURRENCY).strip()
                              or DEFAULT_CURRENCY)[:8],
        "prep_lead_days":  max(0, int(p.get("prep_lead_days") or 0)),
        "items":           items,
        "included_diet_tags": sorted({str(t).strip().lower()[:32]
                                       for t in (p.get("included_diet_tags") or [])
                                       if str(t).strip()}),
        "tier":            tier,
        "popular":         bool(p.get("popular", False)),
        "active":          bool(p.get("active", True)),
        "sort_order":      int(p.get("sort_order") or 99),
    }


def add_package(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Package needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        pk = _coerce(patch, payload["packages"])
        if pk["min_guests"] > pk["max_guests"]:
            raise _Refused("min_guests cannot exceed max_guests.")
        payload["packages"].append(pk)
        _save_locked(payload)
        return pk


def update_package(pkg_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, x in enumerate(payload["packages"]):
            if x.get("id") == pkg_id:
                merged = {**x, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = pkg_id
                if not (merged.get("name_en") or merged.get("name_ar") or "").strip():
                    raise _Refused("Package needs at least one name.")
                pk = _coerce(merged, payload["packages"])
                if pk["min_guests"] > pk["max_guests"]:
                    raise _Refused("min_guests cannot exceed max_guests.")
                payload["packages"][i] = pk
                _save_locked(payload)
                return payload["packages"][i]
    return None


def delete_package(pkg_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["packages"])
        payload["packages"] = [x for x in payload["packages"]
                                if x.get("id") != pkg_id]
        removed = len(payload["packages"]) < before
        if removed:
            _save_locked(payload)
        return removed
