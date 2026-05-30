"""
Restaurant F&B menu — operator-editable categories + items with a
"Reset Dummy Data" path back to a Lebanese-restaurant seed.

Storage:
    data/demos/restaurant/menu.json

Tracked by git so both machines see the same menu after a pull; the
atomic-write `.tmp` is gitignored. Reset Dummy Data overwrites the
file with DEFAULT_MENU.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("demo_restaurant.menu")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_MENU_PATH = _DATA_DIR / "menu.json"

_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

# ----------------------------------------------------------------------
# Lebanese-restaurant seed
# ----------------------------------------------------------------------
# Categories use stable string ids — items reference them by id, so a
# rename in name_en / name_ar doesn't break the item→category link.
# Prices are in SAR; tweak via the UI if you want different currency.

DEFAULT_CATEGORIES: list[dict] = [
    {"id": "CAT-COLD",  "name_en": "Cold Mezze",   "name_ar": "المقبلات الباردة", "sort_order": 1, "active": True},
    {"id": "CAT-HOT",   "name_en": "Hot Mezze",    "name_ar": "المقبلات الساخنة", "sort_order": 2, "active": True},
    {"id": "CAT-SALAD", "name_en": "Salads",       "name_ar": "السلطات",         "sort_order": 3, "active": True},
    {"id": "CAT-GRILL", "name_en": "Grills",       "name_ar": "المشاوي",          "sort_order": 4, "active": True},
    {"id": "CAT-MAIN",  "name_en": "Main Courses", "name_ar": "الأطباق الرئيسية", "sort_order": 5, "active": True},
    {"id": "CAT-DESS",  "name_en": "Desserts",     "name_ar": "الحلويات",         "sort_order": 6, "active": True},
    {"id": "CAT-BEV",   "name_en": "Beverages",    "name_ar": "المشروبات",        "sort_order": 7, "active": True},
]

DEFAULT_ITEMS: list[dict] = [
    # Cold Mezze
    {"id": "ITM-001", "category_id": "CAT-COLD",  "name_en": "Hummus",                 "name_ar": "حمّص",
     "description_en": "Chickpea purée with tahini, lemon and olive oil.",
     "description_ar": "حمّص مع الطحينة والليمون وزيت الزيتون.",
     "price": 18.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-002", "category_id": "CAT-COLD",  "name_en": "Mutabbal",               "name_ar": "متبّل",
     "description_en": "Smoked aubergine with tahini, garlic and lemon.",
     "description_ar": "باذنجان مشوي مع طحينة وثوم وليمون.",
     "price": 22.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-003", "category_id": "CAT-COLD",  "name_en": "Labneh",                 "name_ar": "لبنة",
     "description_en": "Strained yoghurt with olive oil and dried mint.",
     "description_ar": "لبنة مع زيت زيتون ونعناع يابس.",
     "price": 20.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-004", "category_id": "CAT-COLD",  "name_en": "Muhammara",              "name_ar": "محمّرة",
     "description_en": "Roasted red pepper and walnut dip with pomegranate molasses.",
     "description_ar": "فلفل أحمر مشوي وجوز مع دبس الرمان.",
     "price": 24.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-005", "category_id": "CAT-COLD",  "name_en": "Warak Enab (cold)",      "name_ar": "ورق عنب بزيت",
     "description_en": "Vine leaves stuffed with rice, tomato, parsley and lemon.",
     "description_ar": "ورق عنب محشي بالأرز والطماطم والبقدونس والليمون.",
     "price": 26.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Hot Mezze
    {"id": "ITM-006", "category_id": "CAT-HOT",   "name_en": "Falafel (6 pcs)",        "name_ar": "فلافل (6 قطع)",
     "description_en": "Deep-fried chickpea fritters with tahini sauce.",
     "description_ar": "أقراص الحمّص المقلية مع صلصة الطحينة.",
     "price": 22.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-007", "category_id": "CAT-HOT",   "name_en": "Kibbeh (4 pcs)",         "name_ar": "كبّة (4 قطع)",
     "description_en": "Bulgur shells stuffed with spiced minced lamb and pine nuts.",
     "description_ar": "أقراص البرغل المحشية باللحم المفروم والصنوبر.",
     "price": 28.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-008", "category_id": "CAT-HOT",   "name_en": "Sambousek (cheese)",     "name_ar": "سمبوسك بالجبن",
     "description_en": "Fried pastry parcels filled with akkawi cheese.",
     "description_ar": "عجينة مقلية محشوة بالجبن العكاوي.",
     "price": 24.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-009", "category_id": "CAT-HOT",   "name_en": "Fatayer (spinach)",      "name_ar": "فطاير سبانخ",
     "description_en": "Triangle pastries stuffed with spinach, sumac and pine nuts.",
     "description_ar": "فطائر السبانخ المثلثة مع السماق والصنوبر.",
     "price": 22.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-010", "category_id": "CAT-HOT",   "name_en": "Arayes",                 "name_ar": "عرايس",
     "description_en": "Grilled flatbread stuffed with spiced minced meat.",
     "description_ar": "خبز مرقوق محشو باللحم المفروم ومشوي على الفحم.",
     "price": 32.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Salads
    {"id": "ITM-011", "category_id": "CAT-SALAD", "name_en": "Tabbouleh",              "name_ar": "تبّولة",
     "description_en": "Finely chopped parsley, mint, tomato, bulgur, lemon and olive oil.",
     "description_ar": "بقدونس ونعناع وطماطم وبرغل مع ليمون وزيت زيتون.",
     "price": 25.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-012", "category_id": "CAT-SALAD", "name_en": "Fattoush",               "name_ar": "فتّوش",
     "description_en": "Mixed greens with toasted bread, sumac and pomegranate.",
     "description_ar": "خضار طازجة مع خبز محمّص وسماق وحبوب رمان.",
     "price": 24.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-013", "category_id": "CAT-SALAD", "name_en": "Rocket & Halloumi",      "name_ar": "جرجير وحلوم",
     "description_en": "Rocket leaves with grilled halloumi cheese and pomegranate.",
     "description_ar": "ورق الجرجير مع حلوم مشوي ودبس الرمان.",
     "price": 32.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-014", "category_id": "CAT-SALAD", "name_en": "Beetroot Salad",         "name_ar": "سلطة الشمندر",
     "description_en": "Roasted beetroot, walnuts, feta and orange zest.",
     "description_ar": "شمندر مشوي مع جوز وجبن فيتا وبشر البرتقال.",
     "price": 28.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Grills
    {"id": "ITM-015", "category_id": "CAT-GRILL", "name_en": "Shish Tawook",           "name_ar": "شيش طاووق",
     "description_en": "Marinated chicken skewers with garlic sauce and pickles.",
     "description_ar": "أسياخ دجاج متبّل مع ثومية ومخلل.",
     "price": 65.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-016", "category_id": "CAT-GRILL", "name_en": "Lamb Kafta",             "name_ar": "كفتة لحم",
     "description_en": "Minced lamb skewers with parsley and onion.",
     "description_ar": "أسياخ كفتة من لحم الغنم مع البقدونس والبصل.",
     "price": 72.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-017", "category_id": "CAT-GRILL", "name_en": "Lamb Chops (4 pcs)",     "name_ar": "ريش الغنم (4 قطع)",
     "description_en": "Charcoal-grilled lamb chops, seasoned and served with rice.",
     "description_ar": "ريش غنم مشوية على الفحم مع أرز بالشعرية.",
     "price": 95.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-018", "category_id": "CAT-GRILL", "name_en": "Mixed Grill",            "name_ar": "مشاوي مشكّلة",
     "description_en": "Assortment of shish tawook, kafta and lamb cubes.",
     "description_ar": "تشكيلة من شيش طاووق وكفتة وقطع لحم الغنم.",
     "price": 110.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-019", "category_id": "CAT-GRILL", "name_en": "Chicken Shawarma Plate", "name_ar": "صحن شاورما دجاج",
     "description_en": "Shaved marinated chicken with garlic sauce and Lebanese bread.",
     "description_ar": "شرائح دجاج متبّلة مع ثومية وخبز لبناني.",
     "price": 55.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Main Courses
    {"id": "ITM-020", "category_id": "CAT-MAIN",  "name_en": "Ouzi",                   "name_ar": "أوزي",
     "description_en": "Slow-cooked lamb shoulder over spiced rice with nuts.",
     "description_ar": "كتف الغنم المطبوخ مع أرز بهارات ومكسرات.",
     "price": 95.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-021", "category_id": "CAT-MAIN",  "name_en": "Mansaf",                 "name_ar": "منسف",
     "description_en": "Lamb cooked in jameed yoghurt sauce, served over rice.",
     "description_ar": "لحم غنم مطبوخ بصلصة الجميد فوق الأرز.",
     "price": 88.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-022", "category_id": "CAT-MAIN",  "name_en": "Sayadieh",               "name_ar": "صيادية",
     "description_en": "Spiced fish with caramelised onion rice and tahini sauce.",
     "description_ar": "سمك متبّل مع أرز بالبصل المحمّص وصلصة طحينة.",
     "price": 85.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-023", "category_id": "CAT-MAIN",  "name_en": "Mahshi Mixed",           "name_ar": "محشي مشكّل",
     "description_en": "Stuffed courgette, vine leaves and aubergine in tomato sauce.",
     "description_ar": "كوسا وورق عنب وباذنجان محشي بصلصة الطماطم.",
     "price": 68.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Desserts
    {"id": "ITM-024", "category_id": "CAT-DESS",  "name_en": "Baklava (6 pcs)",        "name_ar": "بقلاوة (6 قطع)",
     "description_en": "Layered filo pastry with pistachios and rose-water syrup.",
     "description_ar": "بقلاوة بطبقات الفيلو والفستق وقطر ماء الورد.",
     "price": 28.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-025", "category_id": "CAT-DESS",  "name_en": "Knafeh Nabulsia",        "name_ar": "كنافة نابلسية",
     "description_en": "Sweet cheese pastry with semolina, soaked in syrup.",
     "description_ar": "كنافة بالجبن مع السميد ومنقوعة بالقطر.",
     "price": 32.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-026", "category_id": "CAT-DESS",  "name_en": "Mhalabia",               "name_ar": "مهلبية",
     "description_en": "Rose-water milk pudding with pistachios.",
     "description_ar": "مهلبية بماء الورد والفستق.",
     "price": 22.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-027", "category_id": "CAT-DESS",  "name_en": "Atayef Bil Jibneh",      "name_ar": "قطايف بالجبنة",
     "description_en": "Mini pancakes filled with akkawi cheese and orange-blossom syrup.",
     "description_ar": "قطايف صغيرة محشوة بالجبن العكاوي مع قطر ماء الزهر.",
     "price": 26.00, "currency": DEFAULT_CURRENCY, "available": True},

    # Beverages
    {"id": "ITM-028", "category_id": "CAT-BEV",   "name_en": "Mint Lemonade",          "name_ar": "ليمون بالنعناع",
     "description_en": "Fresh lemonade blended with mint leaves.",
     "description_ar": "ليموناضة طازجة مخفوقة بالنعناع.",
     "price": 18.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-029", "category_id": "CAT-BEV",   "name_en": "Jallab",                 "name_ar": "جلاب",
     "description_en": "Date and rose-water syrup with pine nuts and raisins.",
     "description_ar": "شراب التمر وماء الورد مع الصنوبر والزبيب.",
     "price": 16.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-030", "category_id": "CAT-BEV",   "name_en": "Arabic Coffee",          "name_ar": "قهوة عربية",
     "description_en": "Lightly roasted coffee with cardamom, served in a dallah.",
     "description_ar": "قهوة عربية خفيفة التحميص بالهيل تُقدم في دلّة.",
     "price": 14.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-031", "category_id": "CAT-BEV",   "name_en": "Mint Tea",               "name_ar": "شاي بالنعناع",
     "description_en": "Black tea steeped with fresh mint leaves.",
     "description_ar": "شاي أسود مع أوراق النعناع الطازجة.",
     "price": 12.00, "currency": DEFAULT_CURRENCY, "available": True},
    {"id": "ITM-032", "category_id": "CAT-BEV",   "name_en": "Ayran",                  "name_ar": "عيران",
     "description_en": "Cold salted yoghurt drink.",
     "description_ar": "شراب اللبن المملّح البارد.",
     "price": 12.00, "currency": DEFAULT_CURRENCY, "available": True},
]


def _default_menu() -> dict:
    return {
        "categories": deepcopy(DEFAULT_CATEGORIES),
        "items":      deepcopy(DEFAULT_ITEMS),
    }


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def load_menu() -> dict:
    """Return the full menu dict. Falls back to the Lebanese-restaurant
    seed when the file is missing — that way the page works out of the
    box on a fresh deploy without needing a separate "init" call."""
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _MENU_PATH.exists():
        return _default_menu()
    try:
        data = json.loads(_MENU_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("menu.json corrupt — falling back to seed")
        return _default_menu()
    if not isinstance(data, dict):
        return _default_menu()
    cats = data.get("categories") if isinstance(data.get("categories"), list) else []
    items = data.get("items")      if isinstance(data.get("items"),      list) else []
    return {
        "categories": [c for c in cats  if isinstance(c, dict)],
        "items":      [i for i in items if isinstance(i, dict)],
    }


def _save_locked(menu: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _MENU_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(menu, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(_MENU_PATH)
    return menu


def reset_menu() -> dict:
    """Restore the Lebanese-restaurant seed, overwriting whatever's on
    disk. Returns the fresh menu."""
    with _LOCK:
        return _save_locked(_default_menu())


# ----------------------------------------------------------------------
# ID generation
# ----------------------------------------------------------------------

def _next_id(rows: list[dict], prefix: str) -> str:
    """Pick the next numeric id with the given prefix, max(existing)+1.
    Mirrors the approach the clinic backend uses for patient ids."""
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith(prefix + "-"):
            try:
                n = max(n, int(rid[len(prefix) + 1:]))
            except Exception:
                pass
    return f"{prefix}-{n + 1:03d}"


# ----------------------------------------------------------------------
# Category CRUD
# ----------------------------------------------------------------------

def _coerce_category(c: dict, existing: list[dict]) -> dict:
    return {
        "id":         str(c.get("id") or _next_id(existing, "CAT")).strip(),
        "name_en":    str(c.get("name_en") or "").strip()[:200],
        "name_ar":    str(c.get("name_ar") or "").strip()[:200],
        "sort_order": int(c.get("sort_order") or 0),
        "active":     bool(c.get("active", True)),
    }


def add_category(patch: dict) -> dict:
    with _LOCK:
        menu = _load_locked()
        if not (patch.get("name_en") or "").strip() and not (patch.get("name_ar") or "").strip():
            raise ValueError("category needs at least one name (EN or AR)")
        cat = _coerce_category(patch, menu["categories"])
        if not cat["sort_order"]:
            cat["sort_order"] = max(
                (c.get("sort_order") or 0) for c in menu["categories"]
            ) + 1 if menu["categories"] else 1
        menu["categories"].append(cat)
        _save_locked(menu)
        return cat


def update_category(cat_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        menu = _load_locked()
        for i, c in enumerate(menu["categories"]):
            if c.get("id") == cat_id:
                merged = {**c, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = cat_id  # never let the patch change the id
                menu["categories"][i] = _coerce_category(merged, menu["categories"])
                _save_locked(menu)
                return menu["categories"][i]
    return None


def delete_category(cat_id: str) -> dict:
    """Remove a category. Items in that category get their category_id
    set to "" (uncategorised) so we never silently lose menu items."""
    with _LOCK:
        menu = _load_locked()
        before = len(menu["categories"])
        menu["categories"] = [c for c in menu["categories"] if c.get("id") != cat_id]
        if before == len(menu["categories"]):
            return {"removed": False, "uncategorised_items": 0}
        n = 0
        for it in menu["items"]:
            if it.get("category_id") == cat_id:
                it["category_id"] = ""
                n += 1
        _save_locked(menu)
        return {"removed": True, "uncategorised_items": n}


# ----------------------------------------------------------------------
# Item CRUD
# ----------------------------------------------------------------------

def _coerce_item(i: dict, existing: list[dict]) -> dict:
    return {
        "id":              str(i.get("id") or _next_id(existing, "ITM")).strip(),
        "category_id":     str(i.get("category_id") or "").strip(),
        "name_en":         str(i.get("name_en") or "").strip()[:200],
        "name_ar":         str(i.get("name_ar") or "").strip()[:200],
        "description_en":  str(i.get("description_en") or "").strip()[:500],
        "description_ar":  str(i.get("description_ar") or "").strip()[:500],
        "price":           _safe_price(i.get("price")),
        "currency":        (str(i.get("currency") or DEFAULT_CURRENCY).strip()
                            or DEFAULT_CURRENCY)[:8],
        "available":       bool(i.get("available", True)),
    }


def _safe_price(v: Any) -> float:
    try:
        return round(max(0.0, float(v)), 2)
    except Exception:
        return 0.0


def add_item(patch: dict) -> dict:
    with _LOCK:
        menu = _load_locked()
        if not (patch.get("name_en") or "").strip() and not (patch.get("name_ar") or "").strip():
            raise ValueError("item needs at least one name (EN or AR)")
        item = _coerce_item(patch, menu["items"])
        menu["items"].append(item)
        _save_locked(menu)
        return item


def update_item(item_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        menu = _load_locked()
        for i, it in enumerate(menu["items"]):
            if it.get("id") == item_id:
                merged = {**it, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = item_id
                menu["items"][i] = _coerce_item(merged, menu["items"])
                _save_locked(menu)
                return menu["items"][i]
    return None


def delete_item(item_id: str) -> bool:
    with _LOCK:
        menu = _load_locked()
        before = len(menu["items"])
        menu["items"] = [it for it in menu["items"] if it.get("id") != item_id]
        removed = len(menu["items"]) < before
        if removed:
            _save_locked(menu)
        return removed
