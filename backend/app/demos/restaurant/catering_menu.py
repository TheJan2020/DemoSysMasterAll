"""
Catering item catalog — distinct from the F&B Menu (which is for in-house
dining). Catering items are sold by tray / platter / station / per-head,
not as single plated portions.

Storage:
    data/demos/restaurant/catering_menu.json

Tracked by git; `.tmp` from atomic writes is gitignored.
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.catering_menu")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "catering_menu.json"
_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

CATEGORIES = [
    "cold_platter",     # mezze, salads, cold appetisers
    "hot_platter",      # falafel, kibbeh, fatayer
    "salad_tray",
    "mains_tray",       # mansaf, kabsa, ouzi
    "grill_station",
    "live_station",     # knafeh live, shawarma live
    "dessert_tray",
    "beverage_station",
    "box_lunch",
    "addon",            # waitstaff, linens, AV, etc.
]

UNIT_TYPES = [
    "per_tray",     # 1 tray serves N
    "per_station",  # one station, scoped to event
    "per_box",      # one box per guest
    "per_guest",    # priced per head
    "flat",         # flat fee (e.g. setup, breakdown)
]

COMMON_DIET = ["vegetarian", "vegan", "gluten-free", "dairy-free",
                "low-carb", "high-protein"]
COMMON_ALLERGENS = ["peanut", "tree-nut", "dairy", "egg", "wheat",
                     "soy", "fish", "shellfish", "sesame"]

# ----------------------------------------------------------------------
# Seed — Lebanese-restaurant catering catalog.
# ----------------------------------------------------------------------

DEFAULT_ITEMS: list[dict] = [
    # ---- Cold platters ----
    {"id": "CM-001", "code": "MEZZE-COLD-SM",
     "name_en": "Cold Mezze Platter (serves 10)",
     "name_ar": "صحن مقبلات باردة (10 أشخاص)",
     "description_en": "Hummus, mutabbal, labneh, tabbouleh, olives, fresh bread.",
     "description_ar": "حمّص ومتبّل ولبنة وتبّولة وزيتون وخبز طازج.",
     "category": "cold_platter", "unit_type": "per_tray",
     "serves": 10, "prep_time_min": 25,
     "price": 280.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["dairy", "sesame", "wheat"],
     "image_url": "", "active": True, "sort_order": 1},
    {"id": "CM-002", "code": "MEZZE-COLD-LG",
     "name_en": "Cold Mezze Grand Platter (serves 25)",
     "name_ar": "صحن مقبلات باردة كبير (25 شخص)",
     "description_en": "Premium Lebanese mezze spread with 12 items.",
     "description_ar": "تشكيلة فاخرة من 12 صنفاً من المقبلات اللبنانية.",
     "category": "cold_platter", "unit_type": "per_tray",
     "serves": 25, "prep_time_min": 45,
     "price": 620.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["dairy", "sesame", "wheat"],
     "image_url": "", "active": True, "sort_order": 2},
    {"id": "CM-003", "code": "VINE-LEAVES",
     "name_en": "Stuffed Vine Leaves Tray (60 pcs)",
     "name_ar": "صحن ورق عنب (60 قطعة)",
     "description_en": "Cold vine leaves stuffed with rice, herbs, lemon.",
     "description_ar": "ورق عنب محشي بالأرز والأعشاب والليمون.",
     "category": "cold_platter", "unit_type": "per_tray",
     "serves": 12, "prep_time_min": 90,
     "price": 320.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 3},

    # ---- Hot platters ----
    {"id": "CM-010", "code": "FALAFEL-100",
     "name_en": "Falafel Tray (100 pcs)",
     "name_ar": "صحن فلافل (100 قطعة)",
     "description_en": "Crispy chickpea fritters with tahini sauce.",
     "description_ar": "أقراص الحمّص المقلية مع صلصة الطحينة.",
     "category": "hot_platter", "unit_type": "per_tray",
     "serves": 25, "prep_time_min": 60,
     "price": 380.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  ["sesame"],
     "image_url": "", "active": True, "sort_order": 10},
    {"id": "CM-011", "code": "KIBBEH-60",
     "name_en": "Kibbeh Platter (60 pcs)",
     "name_ar": "صحن كبّة (60 قطعة)",
     "description_en": "Bulgur shells stuffed with spiced minced lamb.",
     "description_ar": "أقراص البرغل المحشية بلحم الغنم المتبّل.",
     "category": "hot_platter", "unit_type": "per_tray",
     "serves": 20, "prep_time_min": 90,
     "price": 540.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["wheat"],
     "image_url": "", "active": True, "sort_order": 11},
    {"id": "CM-012", "code": "SAMBOUSEK-72",
     "name_en": "Sambousek Cheese Tray (72 pcs)",
     "name_ar": "صحن سمبوسك بالجبن (72 قطعة)",
     "description_en": "Fried pastry parcels filled with akkawi cheese.",
     "description_ar": "عجينة مقلية محشوة بالجبن العكاوي.",
     "category": "hot_platter", "unit_type": "per_tray",
     "serves": 24, "prep_time_min": 70,
     "price": 420.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["dairy", "wheat"],
     "image_url": "", "active": True, "sort_order": 12},
    {"id": "CM-013", "code": "FATAYER-72",
     "name_en": "Spinach Fatayer (72 pcs)",
     "name_ar": "فطاير سبانخ (72 قطعة)",
     "description_en": "Triangle pastries with spinach, sumac, pine nuts.",
     "description_ar": "فطائر السبانخ مع السماق والصنوبر.",
     "category": "hot_platter", "unit_type": "per_tray",
     "serves": 24, "prep_time_min": 75,
     "price": 380.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  ["wheat", "tree-nut"],
     "image_url": "", "active": True, "sort_order": 13},

    # ---- Salads ----
    {"id": "CM-020", "code": "FATTOUSH-LG",
     "name_en": "Fattoush Salad — Large",
     "name_ar": "سلطة فتّوش — كبير",
     "description_en": "Mixed greens, toasted bread, sumac, pomegranate.",
     "description_ar": "خضار مع خبز محمّر وسماق ودبس الرمان.",
     "category": "salad_tray", "unit_type": "per_tray",
     "serves": 15, "prep_time_min": 20,
     "price": 220.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  ["wheat"],
     "image_url": "", "active": True, "sort_order": 20},
    {"id": "CM-021", "code": "TABBOULEH-LG",
     "name_en": "Tabbouleh — Large Bowl",
     "name_ar": "تبّولة — وعاء كبير",
     "description_en": "Finely chopped parsley, mint, bulgur, lemon, oil.",
     "description_ar": "بقدونس ونعناع وبرغل مع ليمون وزيت زيتون.",
     "category": "salad_tray", "unit_type": "per_tray",
     "serves": 15, "prep_time_min": 35,
     "price": 240.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  ["wheat"],
     "image_url": "", "active": True, "sort_order": 21},

    # ---- Mains ----
    {"id": "CM-030", "code": "MANSAF-TRAY",
     "name_en": "Lamb Mansaf — Full Tray (serves 15)",
     "name_ar": "منسف لحم — صحن كامل (15 شخص)",
     "description_en": "Slow-cooked lamb in jameed yoghurt over rice with pine nuts.",
     "description_ar": "لحم غنم مطبوخ بصلصة الجميد فوق الأرز مع الصنوبر.",
     "category": "mains_tray", "unit_type": "per_tray",
     "serves": 15, "prep_time_min": 240,
     "price": 1450.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["dairy", "tree-nut"],
     "image_url": "", "active": True, "sort_order": 30},
    {"id": "CM-031", "code": "OUZI-TRAY",
     "name_en": "Ouzi — Full Tray (serves 15)",
     "name_ar": "أوزي — صحن كامل (15 شخص)",
     "description_en": "Slow-roasted lamb shoulder over spiced rice with nuts and raisins.",
     "description_ar": "كتف الغنم مع أرز بهارات ومكسرات وزبيب.",
     "category": "mains_tray", "unit_type": "per_tray",
     "serves": 15, "prep_time_min": 300,
     "price": 1580.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["tree-nut"],
     "image_url": "", "active": True, "sort_order": 31},
    {"id": "CM-032", "code": "KABSA-TRAY",
     "name_en": "Chicken Kabsa — Tray (serves 12)",
     "name_ar": "كبسة دجاج — صحن (12 شخص)",
     "description_en": "Saudi-style spiced rice with chicken and roasted nuts.",
     "description_ar": "كبسة دجاج بالأرز والبهارات والمكسرات.",
     "category": "mains_tray", "unit_type": "per_tray",
     "serves": 12, "prep_time_min": 90,
     "price": 780.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["tree-nut"],
     "image_url": "", "active": True, "sort_order": 32},
    {"id": "CM-033", "code": "SAYADIEH-TRAY",
     "name_en": "Sayadieh Fish — Tray (serves 12)",
     "name_ar": "صيادية سمك — صحن (12 شخص)",
     "description_en": "Spiced fish over caramelised onion rice with tahini sauce.",
     "description_ar": "سمك مع أرز البصل المحمّر وصلصة الطحينة.",
     "category": "mains_tray", "unit_type": "per_tray",
     "serves": 12, "prep_time_min": 75,
     "price": 920.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["fish", "sesame"],
     "image_url": "", "active": True, "sort_order": 33},

    # ---- Grill stations ----
    {"id": "CM-040", "code": "GRILL-MIXED",
     "name_en": "Mixed Grill Station",
     "name_ar": "محطة مشاوي مشكّلة",
     "description_en": "Shish tawook, kafta, lamb skewers — grilled live on charcoal.",
     "description_ar": "شيش طاووق وكفتة وأسياخ لحم مشوية على الفحم.",
     "category": "grill_station", "unit_type": "per_guest",
     "serves": 1, "prep_time_min": 0,    # live, on-demand
     "price": 95.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 40},
    {"id": "CM-041", "code": "GRILL-CHICKEN",
     "name_en": "Shish Tawook Live Station",
     "name_ar": "محطة شيش طاووق مباشر",
     "description_en": "Marinated chicken skewers grilled to order.",
     "description_ar": "أسياخ دجاج متبّلة تُشوى عند الطلب.",
     "category": "grill_station", "unit_type": "per_guest",
     "serves": 1, "prep_time_min": 0,
     "price": 75.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 41},

    # ---- Live stations ----
    {"id": "CM-050", "code": "STATION-KNAFEH",
     "name_en": "Knafeh Live Station",
     "name_ar": "محطة كنافة مباشرة",
     "description_en": "Fresh knafeh prepared live with orange-blossom syrup.",
     "description_ar": "كنافة طازجة تُحضّر مباشرةً مع ماء الزهر.",
     "category": "live_station", "unit_type": "per_station",
     "serves": 80, "prep_time_min": 0,
     "price": 1800.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["dairy", "wheat"],
     "image_url": "", "active": True, "sort_order": 50},
    {"id": "CM-051", "code": "STATION-SHAWARMA",
     "name_en": "Shawarma Live Station",
     "name_ar": "محطة شاورما مباشرة",
     "description_en": "Whole shawarma spit carved on demand, served in wraps.",
     "description_ar": "شاورما كاملة تُقطع مباشرةً وتُقدم باللفائف.",
     "category": "live_station", "unit_type": "per_station",
     "serves": 100, "prep_time_min": 0,
     "price": 2200.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["high-protein"],
     "allergens":  ["wheat"],
     "image_url": "", "active": True, "sort_order": 51},
    {"id": "CM-052", "code": "STATION-QAHWA",
     "name_en": "Saudi Coffee & Dates Station",
     "name_ar": "محطة قهوة عربية وتمر",
     "description_en": "Traditional dallah service with premium dates.",
     "description_ar": "ضيافة الدلّة العربية مع تمور فاخرة.",
     "category": "live_station", "unit_type": "per_station",
     "serves": 100, "prep_time_min": 0,
     "price": 950.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 52},

    # ---- Desserts ----
    {"id": "CM-060", "code": "BAKLAVA-60",
     "name_en": "Baklava Assortment (60 pcs)",
     "name_ar": "تشكيلة بقلاوة (60 قطعة)",
     "description_en": "Layered filo with pistachio + walnut, rose syrup.",
     "description_ar": "بقلاوة بطبقات الفيلو والفستق والجوز.",
     "category": "dessert_tray", "unit_type": "per_tray",
     "serves": 20, "prep_time_min": 30,
     "price": 380.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["tree-nut", "wheat", "dairy"],
     "image_url": "", "active": True, "sort_order": 60},
    {"id": "CM-061", "code": "MHALABIA-25",
     "name_en": "Mhalabia Cups (25 cups)",
     "name_ar": "مهلبية (25 كوب)",
     "description_en": "Rose-water milk pudding with pistachio.",
     "description_ar": "مهلبية بماء الورد والفستق.",
     "category": "dessert_tray", "unit_type": "per_tray",
     "serves": 25, "prep_time_min": 40,
     "price": 320.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegetarian"],
     "allergens":  ["dairy", "tree-nut"],
     "image_url": "", "active": True, "sort_order": 61},

    # ---- Beverages ----
    {"id": "CM-070", "code": "JUICE-STATION",
     "name_en": "Fresh Juice & Lemonade Station",
     "name_ar": "محطة عصائر وليمون طازج",
     "description_en": "Mint lemonade, jallab, fresh juices — unlimited refills.",
     "description_ar": "ليمون نعناع وجلاب وعصائر — تعبئة مستمرة.",
     "category": "beverage_station", "unit_type": "per_guest",
     "serves": 1, "prep_time_min": 0,
     "price": 22.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["vegan", "vegetarian"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 70},

    # ---- Box lunches ----
    {"id": "CM-080", "code": "BOX-STANDARD",
     "name_en": "Standard Box Lunch",
     "name_ar": "صندوق غذاء عادي",
     "description_en": "Wrap + side + dessert + bottled water in branded box.",
     "description_ar": "لفافة وطبق جانبي وحلوى وماء في صندوق بعلامتنا.",
     "category": "box_lunch", "unit_type": "per_box",
     "serves": 1, "prep_time_min": 5,
     "price": 80.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": [],
     "allergens":  ["wheat"],
     "image_url": "", "active": True, "sort_order": 80},
    {"id": "CM-081", "code": "BOX-GF",
     "name_en": "Gluten-Free Box Lunch",
     "name_ar": "صندوق غذاء خالي من الجلوتين",
     "description_en": "GF wrap + salad + GF dessert + water, sealed.",
     "description_ar": "لفافة خالية من الجلوتين وسلطة وحلوى وماء.",
     "category": "box_lunch", "unit_type": "per_box",
     "serves": 1, "prep_time_min": 7,
     "price": 95.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": ["gluten-free"],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 81},

    # ---- Add-ons (waitstaff, linens, etc.) ----
    {"id": "CM-090", "code": "STAFF-WAITER",
     "name_en": "Service Waiter (4-hour shift)",
     "name_ar": "نادل خدمة (نوبة 4 ساعات)",
     "description_en": "Trained service staff in uniform, includes setup + breakdown.",
     "description_ar": "نادل مدرّب بالزي الرسمي، يشمل التحضير وإنهاء الخدمة.",
     "category": "addon", "unit_type": "flat",
     "serves": 0, "prep_time_min": 0,
     "price": 240.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": [],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 90},
    {"id": "CM-091", "code": "STAFF-CAPTAIN",
     "name_en": "Captain / Event Lead (5-hour shift)",
     "name_ar": "كابتن إدارة الحدث (نوبة 5 ساعات)",
     "description_en": "Service captain coordinating waitstaff and timing.",
     "description_ar": "كابتن خدمة لإدارة طاقم النادلين والتوقيت.",
     "category": "addon", "unit_type": "flat",
     "serves": 0, "prep_time_min": 0,
     "price": 480.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": [],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 91},
    {"id": "CM-092", "code": "LINENS-BASIC",
     "name_en": "Table Linens — Basic Set (per table)",
     "name_ar": "مفارش طاولة — أساسي (لكل طاولة)",
     "description_en": "Tablecloth + napkins in neutral white.",
     "description_ar": "مفرش طاولة ومناديل بيضاء.",
     "category": "addon", "unit_type": "flat",
     "serves": 0, "prep_time_min": 0,
     "price": 65.00, "currency": DEFAULT_CURRENCY,
     "diet_tags": [],
     "allergens":  [],
     "image_url": "", "active": True, "sort_order": 92},
]


def _default_payload() -> dict:
    return {"items": deepcopy(DEFAULT_ITEMS)}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_items() -> list[dict]:
    with _LOCK:
        rows = _load_locked().get("items") or []
    rows.sort(key=lambda r: (r.get("sort_order") or 99,
                              r.get("name_en") or ""))
    return rows


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("catering_menu.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    rows = d.get("items")
    return {"items": [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_items() -> list[dict]:
    with _LOCK:
        _save_locked(_default_payload())
    return load_items()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("CM-"):
            try: n = max(n, int(rid[3:]))
            except Exception: pass
    return f"CM-{n + 1:03d}"


def _coerce(it: dict, existing: list[dict]) -> dict:
    cat = str(it.get("category") or "cold_platter").strip().lower()
    if cat not in CATEGORIES: cat = "cold_platter"
    unit = str(it.get("unit_type") or "per_tray").strip().lower()
    if unit not in UNIT_TYPES: unit = "per_tray"
    return {
        "id":             str(it.get("id") or _next_id(existing)).strip(),
        "code":           str(it.get("code") or "").strip().upper()[:32],
        "name_en":        str(it.get("name_en") or "").strip()[:200],
        "name_ar":        str(it.get("name_ar") or "").strip()[:200],
        "description_en": str(it.get("description_en") or "").strip()[:500],
        "description_ar": str(it.get("description_ar") or "").strip()[:500],
        "category":       cat,
        "unit_type":      unit,
        "serves":         max(0, int(it.get("serves") or 0)),
        "prep_time_min":  max(0, int(it.get("prep_time_min") or 0)),
        "price":          round(max(0.0, float(it.get("price") or 0)), 2),
        "currency":       (str(it.get("currency") or DEFAULT_CURRENCY).strip()
                            or DEFAULT_CURRENCY)[:8],
        "diet_tags":      sorted({str(t).strip().lower()[:32]
                                   for t in (it.get("diet_tags") or [])
                                   if str(t).strip()}),
        "allergens":      sorted({str(a).strip().lower()[:32]
                                   for a in (it.get("allergens") or [])
                                   if str(a).strip()}),
        "image_url":      str(it.get("image_url") or "").strip()[:500],
        "active":         bool(it.get("active", True)),
        "sort_order":     int(it.get("sort_order") or 99),
    }


def add_item(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Item needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        it = _coerce(patch, payload["items"])
        payload["items"].append(it)
        _save_locked(payload)
        return it


def update_item(item_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, x in enumerate(payload["items"]):
            if x.get("id") == item_id:
                merged = {**x, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = item_id
                if not (merged.get("name_en") or merged.get("name_ar") or "").strip():
                    raise _Refused("Item needs at least one name.")
                payload["items"][i] = _coerce(merged, payload["items"])
                _save_locked(payload)
                return payload["items"][i]
    return None


def delete_item(item_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["items"])
        payload["items"] = [x for x in payload["items"] if x.get("id") != item_id]
        removed = len(payload["items"]) < before
        if removed:
            _save_locked(payload)
        return removed
