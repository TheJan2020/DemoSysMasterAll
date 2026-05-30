"""
Kitchen back-of-house layout — two floors (Ground + First), each with
its own set of rooms (chillers, freezers, processing rooms, offices,
etc.) on a shared 900×600 canvas.

The default seed is modelled on the architectural drawings in
`docs/restaurant-floor-plans/` (PRIMEWAVE A-1 + A-2). The operator can
still drag, resize, rename, add, and delete zones from the Kitchen →
Layout page; "Reset to default" restores this seed.

Each zone carries a `floor_id` that ties it to one of the floors in
`DEFAULT_FLOORS`. The SPA shows only the zones on the currently-
selected floor tab.

Storage:
    data/demos/restaurant/kitchen_layout.json
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.kitchen_layout")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "kitchen_layout.json"
_LOCK = threading.Lock()

CANVAS_W = 900
CANVAS_H = 600

ZONE_KINDS = [
    "hot_line",       # cooking station with ranges, grills, ovens
    "cold_line",      # garde manger, salads, mezze
    "prep",           # mise en place
    "pastry",         # desserts + bakery / bread
    "walk_in_cooler", # chiller room
    "walk_in_freezer",
    "dishwashing",
    "pass",           # expediter window
    "storage",        # dry goods pantry
    "office",
    "processing",     # protein / fish / veg processing
    "receiving",      # receiving / dispatch / loading
    "utility",        # janitor / chemical / lab / toilet
    "lounge",         # employees lounge / locker
    "corridor",       # hallway / passage between room clusters
]


DEFAULT_FLOORS: list[dict] = [
    {"id": "ground", "name_en": "Ground Floor",
      "name_ar": "الطابق الأرضي", "sort_order": 1},
    {"id": "first",  "name_en": "First Floor",
      "name_ar": "الطابق الأول",   "sort_order": 2},
]


# ----------------------------------------------------------------------
# Default zone catalog — modelled on the architectural drawings A-1
# (Ground Floor) + A-2 (First Floor). Rather than a uniform grid, the
# rooms are clustered into the same functional groups as the drawings
# (cooking + storage band, chillers row, dispatch + offices band), with
# explicit "corridor" zones representing the hallways that connect them.
# Sizes vary intentionally — small utility closets, large lounges + dry
# stores — so the layout reads like a real floor plan, not a spreadsheet.
# The operator can still drag, resize, rename, add, and delete zones
# from the Kitchen → Layout page's Edit mode; "Reset to default"
# restores this seed.
# ----------------------------------------------------------------------

DEFAULT_ZONES: list[dict] = [
    # ========================== GROUND FLOOR (21 zones) ===========================
    # Layout is modelled directly on the PRIMEWAVE A-1 drawing:
    #   * three small rooms in a narrow protrusion at the top centre
    #     (Chemical Room · Janitor · Clean Pot)
    #   * a tall column of utility + storage on the west wall
    #     (Dry Store · Veg Cleaning · Protein Freezer Room · Veg Chiller)
    #   * the central kitchen area is intentionally left empty (no
    #     "Hot Kitchen" label per the operator's request)
    #   * a row of stocks behind the cooking line (Chicken / Meat / Fish)
    #   * a band of processing + chillers below the stocks
    #   * a wide receiving bay tucked into the south-west
    #   * offices, dispatch, lounge, entry and exit along the south wall
    # Each room is rendered as a soft-pastel rounded card in the SPA;
    # the colour below feeds both the border and the (low-opacity) fill.

    # ----- Top protrusion (north): Chemical · Janitor · Clean Pot -----
    {"id": "GF-CHEMICAL",      "floor_id": "ground",
      "name_en": "CHEMICAL ROOM", "name_ar": "غرفة المواد الكيميائية",
      "kind": "utility",
      "x": 320, "y": 10,  "width": 135, "height": 70,
      "color": "#f97316", "sort_order": 1},
    {"id": "GF-JANITOR",       "floor_id": "ground",
      "name_en": "JANITOR", "name_ar": "غرفة التنظيف",
      "kind": "utility",
      "x": 460, "y": 10,  "width": 100, "height": 70,
      "color": "#eab308", "sort_order": 2},
    {"id": "GF-CLEAN-POT",     "floor_id": "ground",
      "name_en": "CLEAN POT", "name_ar": "أواني نظيفة",
      "kind": "utility",
      "x": 565, "y": 10,  "width": 135, "height": 70,
      "color": "#84cc16", "sort_order": 3},

    # ----- West column: Dry Store + 3 stacked utility rooms -----
    {"id": "GF-DRY-STORE",     "floor_id": "ground",
      "name_en": "DRY STORE", "name_ar": "المخزن الجاف",
      "kind": "storage",
      "x": 10,  "y": 95,  "width": 190, "height": 165,
      "color": "#ca8a04", "sort_order": 10},
    {"id": "GF-VEG-CLEANING",  "floor_id": "ground",
      "name_en": "VEG. CLEANING", "name_ar": "تنظيف الخضار",
      "kind": "processing",
      "x": 10,  "y": 270, "width": 190, "height": 70,
      "color": "#10b981", "sort_order": 11},
    {"id": "GF-PROTEIN-FRZ",   "floor_id": "ground",
      "name_en": "PROTEIN FREEZER ROOM", "name_ar": "غرفة تجميد البروتين",
      "kind": "walk_in_freezer",
      "x": 10,  "y": 350, "width": 190, "height": 95,
      "color": "#2563eb", "sort_order": 12},
    {"id": "GF-VEG-CHILLER",   "floor_id": "ground",
      "name_en": "VEG. CHILLER", "name_ar": "ثلاجة الخضار",
      "kind": "walk_in_cooler",
      "x": 10,  "y": 455, "width": 190, "height": 70,
      "color": "#0891b2", "sort_order": 13},

    # ----- Central stocks row (behind the cooking area) -----
    {"id": "GF-CHICKEN-STOCK", "floor_id": "ground",
      "name_en": "CHICKEN STOCK", "name_ar": "مخزون الدجاج",
      "kind": "walk_in_cooler",
      "x": 215, "y": 270, "width": 130, "height": 100,
      "color": "#db2777", "sort_order": 20},
    {"id": "GF-MEAT-STOCK",    "floor_id": "ground",
      "name_en": "MEAT STOCK", "name_ar": "مخزون اللحم",
      "kind": "walk_in_cooler",
      "x": 355, "y": 270, "width": 130, "height": 100,
      "color": "#dc2626", "sort_order": 21},
    {"id": "GF-FISH-STOCK",    "floor_id": "ground",
      "name_en": "FISH STOCK", "name_ar": "مخزون السمك",
      "kind": "walk_in_cooler",
      "x": 495, "y": 270, "width": 130, "height": 100,
      "color": "#2563eb", "sort_order": 22},

    # ----- Processed + cafe/rice chillers row -----
    {"id": "GF-VEG-PROCESS",   "floor_id": "ground",
      "name_en": "VEG. PROCESS", "name_ar": "تجهيز الخضار",
      "kind": "processing",
      "x": 215, "y": 380, "width": 130, "height": 90,
      "color": "#16a34a", "sort_order": 30},
    {"id": "GF-VEG-PROCESSED", "floor_id": "ground",
      "name_en": "VEG. PROCESSED CHILLER", "name_ar": "ثلاجة الخضار المجهّز",
      "kind": "walk_in_cooler",
      "x": 355, "y": 380, "width": 170, "height": 90,
      "color": "#0d9488", "sort_order": 31},
    {"id": "GF-CAFE-BREAK",    "floor_id": "ground",
      "name_en": "CAFE BREAK CHILLER", "name_ar": "ثلاجة استراحة الكافيه",
      "kind": "walk_in_cooler",
      "x": 535, "y": 380, "width": 175, "height": 90,
      "color": "#ea580c", "sort_order": 32},

    # ----- East side: cooked rice chiller -----
    {"id": "GF-COOKED-RICE",   "floor_id": "ground",
      "name_en": "COOKED RICE CHILLER", "name_ar": "ثلاجة الأرز المطبوخ",
      "kind": "walk_in_cooler",
      "x": 720, "y": 270, "width": 170, "height": 200,
      "color": "#d97706", "sort_order": 40},

    # ----- Receiving bay (south-west, wide) -----
    {"id": "GF-RECEIVING-BAY", "floor_id": "ground",
      "name_en": "RECEIVING BAY", "name_ar": "ساحة الاستلام",
      "kind": "receiving",
      "x": 10,  "y": 540, "width": 245, "height": 50,
      "color": "#475569", "sort_order": 50},

    # ----- South wall row: offices · dispatch · lounge · entry · exit -----
    {"id": "GF-STORE-OFFICE",  "floor_id": "ground",
      "name_en": "STORE OFFICE", "name_ar": "مكتب المخزن",
      "kind": "office",
      "x": 260, "y": 480, "width": 110, "height": 110,
      "color": "#7c3aed", "sort_order": 60},
    {"id": "GF-CHIEF-OFFICE",  "floor_id": "ground",
      "name_en": "CHIEF OFFICE", "name_ar": "مكتب الشيف",
      "kind": "office",
      "x": 375, "y": 480, "width": 110, "height": 110,
      "color": "#4338ca", "sort_order": 61},
    {"id": "GF-DISPATCH",      "floor_id": "ground",
      "name_en": "DISPATCH STORAGE", "name_ar": "مخزن الإرسال",
      "kind": "receiving",
      "x": 490, "y": 480, "width": 175, "height": 110,
      "color": "#64748b", "sort_order": 62},
    {"id": "GF-EMPLOYEES",     "floor_id": "ground",
      "name_en": "EMPLOYEE LOUNGE / LOCKERS", "name_ar": "استراحة الموظفين",
      "kind": "lounge",
      "x": 670, "y": 480, "width": 130, "height": 110,
      "color": "#be185d", "sort_order": 63},
    {"id": "GF-ENTRY",         "floor_id": "ground",
      "name_en": "ENTRY", "name_ar": "الدخول",
      "kind": "receiving",
      "x": 805, "y": 480, "width": 40,  "height": 110,
      "color": "#059669", "sort_order": 64},
    {"id": "GF-EXIT",          "floor_id": "ground",
      "name_en": "EXIT", "name_ar": "الخروج",
      "kind": "receiving",
      "x": 850, "y": 480, "width": 40,  "height": 110,
      "color": "#b91c1c", "sort_order": 65},

    # ========================== FIRST FLOOR (18 zones) ===========================
    # The first-floor building footprint is narrower than the ground floor;
    # the seed reproduces that by leaving the top + bottom strips of the
    # canvas empty (no zones, just the grid background) and packing every
    # room into the central band y = 100..490.

    # Row 1 — process rooms.
    {"id": "FF-CHICKEN-PROC", "floor_id": "first",
      "name_en": "Chicken Process", "name_ar": "تجهيز الدجاج",
      "kind": "processing",
      "x": 60,  "y": 100, "width": 140, "height": 100,
      "color": "#22c55e", "sort_order": 1},
    {"id": "FF-MEAT-PROC",    "floor_id": "first",
      "name_en": "Meat Process", "name_ar": "تجهيز اللحم",
      "kind": "processing",
      "x": 210, "y": 100, "width": 140, "height": 100,
      "color": "#22c55e", "sort_order": 2},
    {"id": "FF-FISH-THAWING", "floor_id": "first",
      "name_en": "Fish Thawing", "name_ar": "إذابة السمك",
      "kind": "processing",
      "x": 360, "y": 100, "width": 95,  "height": 100,
      "color": "#22c55e", "sort_order": 3},
    {"id": "FF-FISH-PROC",    "floor_id": "first",
      "name_en": "Fish Process Room", "name_ar": "غرفة تجهيز السمك",
      "kind": "processing",
      "x": 465, "y": 100, "width": 160, "height": 100,
      "color": "#22c55e", "sort_order": 4},
    {"id": "FF-FISH-FRZ",     "floor_id": "first",
      "name_en": "Fish Freezer", "name_ar": "تجميد السمك",
      "kind": "walk_in_freezer",
      "x": 635, "y": 100, "width": 110, "height": 100,
      "color": "#3b82f6", "sort_order": 5},
    {"id": "FF-LAB",          "floor_id": "first",
      "name_en": "Lab", "name_ar": "المختبر",
      "kind": "utility",
      "x": 755, "y": 100, "width": 135, "height": 100,
      "color": "#f59e0b", "sort_order": 6},

    # Horizontal corridor #1.
    {"id": "FF-CORR-A",       "floor_id": "first",
      "name_en": "Hallway", "name_ar": "ممر",
      "kind": "corridor",
      "x": 60,  "y": 210, "width": 830, "height": 30,
      "color": "#9ca3af", "sort_order": 10},

    # Row 2 — chillers + bread.
    {"id": "FF-CHICKEN-CH",   "floor_id": "first",
      "name_en": "Chicken Chiller", "name_ar": "ثلاجة الدجاج",
      "kind": "walk_in_cooler",
      "x": 60,  "y": 250, "width": 140, "height": 110,
      "color": "#0ea5e9", "sort_order": 20},
    {"id": "FF-MEAT-CH",      "floor_id": "first",
      "name_en": "Meat Chiller", "name_ar": "ثلاجة اللحم",
      "kind": "walk_in_cooler",
      "x": 210, "y": 250, "width": 140, "height": 110,
      "color": "#0ea5e9", "sort_order": 21},
    {"id": "FF-DAIRY-CH",     "floor_id": "first",
      "name_en": "Dairy Chiller", "name_ar": "ثلاجة الألبان",
      "kind": "walk_in_cooler",
      "x": 360, "y": 250, "width": 140, "height": 110,
      "color": "#0ea5e9", "sort_order": 22},
    {"id": "FF-CHILLER-ROOM", "floor_id": "first",
      "name_en": "Chiller Room", "name_ar": "غرفة التبريد",
      "kind": "walk_in_cooler",
      "x": 510, "y": 250, "width": 180, "height": 110,
      "color": "#06b6d4", "sort_order": 23},
    {"id": "FF-CHILLER-BREAD","floor_id": "first",
      "name_en": "Chiller Bread", "name_ar": "تبريد الخبز",
      "kind": "pastry",
      "x": 700, "y": 250, "width": 100, "height": 110,
      "color": "#ec4899", "sort_order": 24},
    {"id": "FF-FRZ-BREAD",    "floor_id": "first",
      "name_en": "Freezer Bread", "name_ar": "تجميد الخبز",
      "kind": "walk_in_freezer",
      "x": 810, "y": 250, "width": 80,  "height": 110,
      "color": "#3b82f6", "sort_order": 25},

    # Horizontal corridor #2.
    {"id": "FF-CORR-B",       "floor_id": "first",
      "name_en": "Hallway", "name_ar": "ممر",
      "kind": "corridor",
      "x": 60,  "y": 370, "width": 830, "height": 25,
      "color": "#9ca3af", "sort_order": 30},

    # Row 3 — utility + a combo freezer/chiller.
    {"id": "FF-JANITOR",      "floor_id": "first",
      "name_en": "Janitor", "name_ar": "غرفة التنظيف",
      "kind": "utility",
      "x": 60,  "y": 405, "width": 110, "height": 90,
      "color": "#f97316", "sort_order": 40},
    {"id": "FF-TOILET",       "floor_id": "first",
      "name_en": "Toilet", "name_ar": "دورة المياه",
      "kind": "utility",
      "x": 180, "y": 405, "width": 110, "height": 90,
      "color": "#f97316", "sort_order": 41},
    {"id": "FF-FRZ-CHILLER",  "floor_id": "first",
      "name_en": "Freezer / Chiller", "name_ar": "تجميد / تبريد",
      "kind": "walk_in_freezer",
      "x": 300, "y": 405, "width": 150, "height": 90,
      "color": "#3b82f6", "sort_order": 42},
]


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def _default_payload() -> dict:
    return {
        "floors": deepcopy(DEFAULT_FLOORS),
        "zones":  deepcopy(DEFAULT_ZONES),
        "canvas": {"width": CANVAS_W, "height": CANVAS_H},
    }


def load_layout() -> dict:
    with _LOCK:
        d = _load_locked()
    d["floors"] = sorted(d["floors"], key=lambda f: f.get("sort_order") or 99)
    d["zones"]  = sorted(d["zones"],  key=lambda z: (z.get("floor_id") or "",
                                                       z.get("sort_order") or 99))
    return d


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("kitchen_layout.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    floors = d.get("floors") if isinstance(d.get("floors"), list) else None
    zones  = d.get("zones")  if isinstance(d.get("zones"),  list) else []
    out_zones = [z for z in zones if isinstance(z, dict)]
    # If the on-disk file predates the multi-floor change, every zone
    # missing a floor_id is treated as Ground-floor. We don't auto-
    # rewrite the file here — the next save will pick up the field.
    for z in out_zones:
        if not z.get("floor_id"):
            z["floor_id"] = "ground"
    return {
        "floors":  [f for f in floors if isinstance(f, dict)] if floors
                    else deepcopy(DEFAULT_FLOORS),
        "zones":   out_zones,
        "canvas":  {"width": CANVAS_W, "height": CANVAS_H},
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_layout() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_layout()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_zone_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("ZN-"):
            suff = rid[3:]
            if suff.isdigit():
                try: n = max(n, int(suff))
                except Exception: pass
    return f"ZN-{n + 1:03d}"


_KNOWN_FLOOR_IDS = {f["id"] for f in DEFAULT_FLOORS}


def _coerce_zone(z: dict, existing: list[dict]) -> dict:
    kind = str(z.get("kind") or "prep").strip().lower()
    if kind not in ZONE_KINDS: kind = "prep"
    floor_id = str(z.get("floor_id") or "ground").strip().lower()
    # Allow custom floors via the layout file, but the seed defines
    # only "ground" + "first" — anything else collapses to ground so
    # the SPA never has zones orphaned on an unknown tab.
    if floor_id not in _KNOWN_FLOOR_IDS:
        floor_id = "ground"
    return {
        "id":          str(z.get("id") or _next_zone_id(existing)).strip()[:32],
        "floor_id":    floor_id,
        "name_en":     str(z.get("name_en") or "").strip()[:120],
        "name_ar":     str(z.get("name_ar") or "").strip()[:120],
        "kind":        kind,
        "x":           max(0, min(CANVAS_W, int(z.get("x") or 0))),
        "y":           max(0, min(CANVAS_H, int(z.get("y") or 0))),
        "width":       max(40, min(CANVAS_W, int(z.get("width") or 100))),
        "height":      max(30, min(CANVAS_H, int(z.get("height") or 80))),
        "color":       str(z.get("color") or "#64748b").strip()[:16],
        "sort_order":  int(z.get("sort_order") or 99),
    }


def add_zone(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Zone needs at least one name.")
    with _LOCK:
        payload = _load_locked()
        z = _coerce_zone(patch, payload["zones"])
        payload["zones"].append(z)
        _save_locked(payload)
    return z


def update_zone(zone_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, z in enumerate(payload["zones"]):
            if z.get("id") == zone_id:
                merged = {**z, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = zone_id
                payload["zones"][i] = _coerce_zone(merged, payload["zones"])
                _save_locked(payload)
                return payload["zones"][i]
    return None


def delete_zone(zone_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["zones"])
        payload["zones"] = [z for z in payload["zones"] if z.get("id") != zone_id]
        removed = len(payload["zones"]) < before
        if removed:
            _save_locked(payload)
        return removed


def zone_centroid(zone: dict) -> tuple[int, int]:
    return (zone["x"] + zone["width"] // 2,
            zone["y"] + zone["height"] // 2)
