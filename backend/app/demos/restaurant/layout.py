"""
Restaurant floor layout — operator-editable indoor + outdoor table map.

Storage:
    data/demos/restaurant/layout.json

Tracked by git (operator-edited content, not per-machine state); the
`.tmp` from atomic writes is gitignored.

Each floor is a fixed 900×500 canvas; table x/y coordinates are pixels
within that canvas. Tables carry per-row state (seats, guests, status,
opened_at, server, ordered_items) so the page can show "T03 has 4
guests, ordered hummus + shish tawook" without joining anything.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.layout")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_LAYOUT_PATH = _DATA_DIR / "layout.json"

_LOCK = threading.Lock()
DEFAULT_CURRENCY = "SAR"

# Canvas size used by every floor — frontend renders at this aspect.
CANVAS_W = 900
CANVAS_H = 500

DEFAULT_FLOORS: list[dict] = [
    {"id": "indoor",  "name_en": "Indoor",  "name_ar": "داخلي",  "sort_order": 1},
    {"id": "outdoor", "name_en": "Outdoor", "name_ar": "خارجي", "sort_order": 2},
]

# Times below are a couple of hours back so a fresh-deploy demo looks
# like "service is in progress". Computed at import so the file content
# changes per deploy — that's fine for a demo.
_NOW = int(time.time())


def _h_ago(h: float) -> int:
    return _NOW - int(h * 3600)


# All tables are visually uniform now (same size + shape + colour on the
# canvas), so width/height/shape stay constant across the seed. The
# logical `seats` count still varies — the agent + reservations page
# both consult it — but the floor tile renders identically regardless.
TILE_W = 90
TILE_H = 90

# 3×3 grid for each floor, centred inside the 900×500 canvas:
#   columns at x = 180, 405, 630 ; rows at y = 80, 200, 320
_GX = [180, 405, 630]
_GY = [80,  200, 320]


def _grid(col: int, row: int) -> dict:
    return {"x": _GX[col], "y": _GY[row],
            "width": TILE_W, "height": TILE_H, "shape": "round"}


DEFAULT_TABLES: list[dict] = [
    # ------------------------------- INDOOR -------------------------------
    {"id": "TBL-001", "floor_id": "indoor",  "number": "T01", **_grid(0, 0),
     "seats": 4, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-002", "floor_id": "indoor",  "number": "T02", **_grid(1, 0),
     "seats": 2, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-003", "floor_id": "indoor",  "number": "T03", **_grid(2, 0),
     "seats": 6, "status": "occupied",
     "guests": 4,
     "ordered_items": [
         {"name_en": "Hummus",         "name_ar": "حمّص",        "qty": 1, "price": 18.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Tabbouleh",      "name_ar": "تبّولة",       "qty": 1, "price": 25.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Shish Tawook",   "name_ar": "شيش طاووق",  "qty": 2, "price": 65.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Mint Lemonade",  "name_ar": "ليمون نعناع", "qty": 2, "price": 18.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(0.75), "server_name": "Omar"},
    {"id": "TBL-004", "floor_id": "indoor",  "number": "T04", **_grid(0, 1),
     "seats": 4, "status": "occupied",
     "guests": 3,
     "ordered_items": [
         {"name_en": "Mutabbal",       "name_ar": "متبّل",       "qty": 1, "price": 22.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Falafel",        "name_ar": "فلافل",      "qty": 1, "price": 22.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Fattoush",       "name_ar": "فتّوش",       "qty": 1, "price": 24.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Arabic Coffee",  "name_ar": "قهوة عربية", "qty": 3, "price": 14.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(1.25), "server_name": "Layla"},
    {"id": "TBL-005", "floor_id": "indoor",  "number": "T05", **_grid(1, 1),
     "seats": 2, "status": "reserved",
     "guests": 0, "ordered_items": [],
     "opened_at": None,
     "server_name": "Reservation: Mr. Khalid · 20:00"},
    {"id": "TBL-006", "floor_id": "indoor",  "number": "T06", **_grid(2, 1),
     "seats": 8, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-007", "floor_id": "indoor",  "number": "T07", **_grid(0, 2),
     "seats": 4, "status": "occupied",
     "guests": 4,
     "ordered_items": [
         {"name_en": "Mixed Grill",    "name_ar": "مشاوي مشكّلة", "qty": 2, "price": 110.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Ayran",          "name_ar": "عيران",      "qty": 4, "price": 12.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Baklava",        "name_ar": "بقلاوة",      "qty": 1, "price": 28.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(0.5), "server_name": "Omar"},
    {"id": "TBL-008", "floor_id": "indoor",  "number": "T08", **_grid(1, 2),
     "seats": 2, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-009", "floor_id": "indoor",  "number": "T09", **_grid(2, 2),
     "seats": 6, "status": "occupied",
     "guests": 5,
     "ordered_items": [
         {"name_en": "Lamb Chops",     "name_ar": "ريش الغنم",    "qty": 2, "price": 95.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Mansaf",         "name_ar": "منسف",        "qty": 1, "price": 88.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Knafeh Nabulsia","name_ar": "كنافة نابلسية", "qty": 2, "price": 32.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Mint Tea",       "name_ar": "شاي بالنعناع",  "qty": 3, "price": 12.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(1.5), "server_name": "Layla"},

    # ------------------------------- OUTDOOR ------------------------------
    {"id": "TBL-010", "floor_id": "outdoor", "number": "T10", **_grid(0, 0),
     "seats": 4, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-011", "floor_id": "outdoor", "number": "T11", **_grid(1, 0),
     "seats": 2, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-012", "floor_id": "outdoor", "number": "T12", **_grid(2, 0),
     "seats": 6, "status": "occupied",
     "guests": 5,
     "ordered_items": [
         {"name_en": "Ouzi",           "name_ar": "أوزي",        "qty": 1, "price": 95.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Sayadieh",       "name_ar": "صيادية",      "qty": 1, "price": 85.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Hummus",         "name_ar": "حمّص",        "qty": 1, "price": 18.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Jallab",         "name_ar": "جلاب",        "qty": 3, "price": 16.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(0.4), "server_name": "Hadi"},
    {"id": "TBL-013", "floor_id": "outdoor", "number": "T13", **_grid(0, 1),
     "seats": 4, "status": "reserved",
     "guests": 0, "ordered_items": [],
     "opened_at": None,
     "server_name": "Reservation: Mrs. Aisha · 21:00"},
    {"id": "TBL-014", "floor_id": "outdoor", "number": "T14", **_grid(1, 1),
     "seats": 2, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-015", "floor_id": "outdoor", "number": "T15", **_grid(2, 1),
     "seats": 4, "status": "occupied",
     "guests": 2,
     "ordered_items": [
         {"name_en": "Rocket & Halloumi", "name_ar": "جرجير وحلوم", "qty": 1, "price": 32.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Chicken Shawarma Plate", "name_ar": "صحن شاورما دجاج", "qty": 1, "price": 55.00, "currency": DEFAULT_CURRENCY},
         {"name_en": "Mint Lemonade",     "name_ar": "ليمون نعناع",  "qty": 2, "price": 18.00, "currency": DEFAULT_CURRENCY},
     ],
     "opened_at": _h_ago(0.3), "server_name": "Hadi"},
    {"id": "TBL-016", "floor_id": "outdoor", "number": "T16", **_grid(0, 2),
     "seats": 2, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-017", "floor_id": "outdoor", "number": "T17", **_grid(1, 2),
     "seats": 4, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None, "server_name": ""},
    {"id": "TBL-018", "floor_id": "outdoor", "number": "T18", **_grid(2, 2),
     "seats": 10, "status": "free",
     "guests": 0, "ordered_items": [], "opened_at": None,
     "server_name": "(VIP table — pergola)"},
]


def _default_layout() -> dict:
    return {
        "canvas":  {"width": CANVAS_W, "height": CANVAS_H},
        "floors":  deepcopy(DEFAULT_FLOORS),
        "tables":  deepcopy(DEFAULT_TABLES),
    }


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def load_layout() -> dict:
    with _LOCK:
        layout = _load_locked()
        # Self-heal stale opened_at values. The seed timestamps drift the
        # moment the FastAPI process restarts (they're stamped at module
        # import), so by the time an operator opens the demo a few hours
        # later, the "elapsed since opened" badges on Orders/POS would
        # read 6+ hours. Refresh any occupied tables whose opened_at is
        # older than 3 hours so they always look like an in-progress
        # service. Persist the fix so subsequent reads are stable.
        if _refresh_stale_opened_at(layout["tables"]):
            _save_locked(layout)
        return layout


def _refresh_stale_opened_at(tables: list[dict]) -> bool:
    """Mutates `tables` in place. Returns True if anything changed."""
    now = int(time.time())
    stale_threshold = 3 * 3600  # 3 hours
    # Deterministic spread of "minutes ago" by table index so multiple
    # occupied tables stagger like a real service (T03 = 45 min in,
    # T04 = 75 min in, etc.) instead of all snapping to the same value.
    spread_min = [25, 45, 75, 30, 55, 90, 15, 65, 40]
    changed = False
    idx = 0
    for t in tables:
        if (t.get("status") or "") != "occupied":
            continue
        opened = t.get("opened_at")
        if opened is None or (now - int(opened)) > stale_threshold:
            offset_min = spread_min[idx % len(spread_min)]
            t["opened_at"] = now - offset_min * 60
            changed = True
        idx += 1
    return changed


def _load_locked() -> dict:
    if not _LAYOUT_PATH.exists():
        return _default_layout()
    try:
        data = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("layout.json corrupt — falling back to seed")
        return _default_layout()
    if not isinstance(data, dict):
        return _default_layout()
    canvas = data.get("canvas") if isinstance(data.get("canvas"), dict) else {}
    floors = data.get("floors") if isinstance(data.get("floors"), list) else []
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    return {
        "canvas": {"width": int(canvas.get("width") or CANVAS_W),
                   "height": int(canvas.get("height") or CANVAS_H)},
        "floors": [f for f in floors if isinstance(f, dict)],
        "tables": [t for t in tables if isinstance(t, dict)],
    }


def _save_locked(layout: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _LAYOUT_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(layout, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(_LAYOUT_PATH)
    return layout


def reset_layout() -> dict:
    """Restore the seed (8 indoor + 9 outdoor tables, including 5
    occupied with order details). Overwrites whatever is on disk."""
    with _LOCK:
        return _save_locked(_default_layout())


# ----------------------------------------------------------------------
# Table CRUD
# ----------------------------------------------------------------------

_VALID_STATUS  = {"free", "occupied", "reserved"}
_VALID_SHAPE   = {"round", "square"}


def _next_table_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("TBL-"):
            try:
                n = max(n, int(rid[4:]))
            except Exception:
                pass
    return f"TBL-{n + 1:03d}"


def _next_table_number(rows: list[dict]) -> str:
    """Pick the next T## number not in use. Doesn't have to be strictly
    sequential — operator may have renamed tables."""
    used = {str((r or {}).get("number") or "").upper().lstrip("T")
            for r in rows}
    for i in range(1, 999):
        if str(i).zfill(2) not in used:
            return f"T{i:02d}"
    return "T?"


def _coerce_item(it: dict) -> dict:
    return {
        "name_en":  str(it.get("name_en") or "").strip()[:200],
        "name_ar":  str(it.get("name_ar") or "").strip()[:200],
        "qty":      max(0, int(it.get("qty") or 0)),
        "price":    round(max(0.0, float(it.get("price") or 0)), 2),
        "currency": (str(it.get("currency") or DEFAULT_CURRENCY).strip()
                      or DEFAULT_CURRENCY)[:8],
    }


def _coerce_table(t: dict, existing: list[dict]) -> dict:
    status = str(t.get("status") or "free").strip().lower()
    if status not in _VALID_STATUS:
        status = "free"
    shape = str(t.get("shape") or "round").strip().lower()
    if shape not in _VALID_SHAPE:
        shape = "round"
    items = t.get("ordered_items") or []
    if not isinstance(items, list):
        items = []
    return {
        "id":            str(t.get("id") or _next_table_id(existing)).strip(),
        "floor_id":      str(t.get("floor_id") or "indoor").strip(),
        "number":        str(t.get("number") or _next_table_number(existing)).strip()[:16],
        "x":             max(0, int(t.get("x") or 0)),
        "y":             max(0, int(t.get("y") or 0)),
        "width":         max(40, int(t.get("width") or 90)),
        "height":        max(40, int(t.get("height") or 90)),
        "shape":         shape,
        "seats":         max(1, int(t.get("seats") or 2)),
        "status":        status,
        "guests":        max(0, int(t.get("guests") or 0)),
        "ordered_items": [_coerce_item(i) for i in items if isinstance(i, dict)],
        "opened_at":     (int(t["opened_at"]) if t.get("opened_at") else None),
        "server_name":   str(t.get("server_name") or "").strip()[:200],
    }


def add_table(patch: dict) -> dict:
    with _LOCK:
        layout = _load_locked()
        if "number" not in patch or not str(patch.get("number") or "").strip():
            patch["number"] = _next_table_number(layout["tables"])
        if patch.get("x") is None: patch["x"] = 100
        if patch.get("y") is None: patch["y"] = 100
        table = _coerce_table(patch, layout["tables"])
        layout["tables"].append(table)
        _save_locked(layout)
        return table


def update_table(table_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        layout = _load_locked()
        for i, t in enumerate(layout["tables"]):
            if t.get("id") == table_id:
                merged = {**t, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = table_id
                # If the status flips away from occupied, clear stale state.
                if merged.get("status") != "occupied":
                    if "guests" not in patch:
                        merged["guests"] = 0
                    if "ordered_items" not in patch:
                        merged["ordered_items"] = []
                    if "opened_at" not in patch:
                        merged["opened_at"] = None
                layout["tables"][i] = _coerce_table(merged, layout["tables"])
                _save_locked(layout)
                return layout["tables"][i]
    return None


def delete_table(table_id: str) -> bool:
    with _LOCK:
        layout = _load_locked()
        before = len(layout["tables"])
        layout["tables"] = [t for t in layout["tables"] if t.get("id") != table_id]
        removed = len(layout["tables"]) < before
        if removed:
            _save_locked(layout)
        return removed
