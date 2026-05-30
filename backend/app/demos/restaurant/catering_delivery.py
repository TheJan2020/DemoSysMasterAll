"""
Catering — drivers + per-order delivery assignments.

A small fleet/drivers book plus an assignments map that pins each
catering order to a driver, vehicle, departure time, and route note.
Used by the daily delivery manifest page.

Storage:
    data/demos/restaurant/catering_delivery.json
        { "drivers": [...], "assignments": [...] }

Tracked by git (operator content); `.tmp` from atomic writes is
gitignored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.catering_delivery")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "catering_delivery.json"
_LOCK = threading.Lock()

VEHICLE_TYPES = ["van_refrigerated", "van_standard", "truck_small",
                  "suv", "motorbike"]
DRIVER_STATUSES = ["available", "on_route", "off_duty"]

_PHONE_RE = re.compile(r"[^\d+\s\-()]+")


# ----------------------------------------------------------------------
# Seed — 6 drivers covering Riyadh districts.
# ----------------------------------------------------------------------

DEFAULT_DRIVERS: list[dict] = [
    {"id": "DRV-001", "name": "Ahmed Al-Ghamdi", "name_ar": "أحمد الغامدي",
     "phone": "+966 50 111 2233", "license_no": "1234567890",
     "vehicle_type": "van_refrigerated", "vehicle_plate": "ABC-2451",
     "capacity_kg": 800, "zones": ["Al-Olaya", "Al-Sahafah", "Al-Malqa"],
     "status": "available", "rating": 4.8, "active": True,
     "notes": "Lead driver — handles wedding events."},
    {"id": "DRV-002", "name": "Yousef Al-Shahrani", "name_ar": "يوسف الشهراني",
     "phone": "+966 55 222 4455", "license_no": "1234567891",
     "vehicle_type": "van_refrigerated", "vehicle_plate": "XYZ-7788",
     "capacity_kg": 800, "zones": ["Hittin", "Al-Yasmin", "Diriyah"],
     "status": "available", "rating": 4.7, "active": True,
     "notes": "Has chilled cabinet — preferred for cold-platter heavy loads."},
    {"id": "DRV-003", "name": "Saad Al-Otaibi", "name_ar": "سعد العتيبي",
     "phone": "+966 53 333 6677", "license_no": "1234567892",
     "vehicle_type": "van_standard", "vehicle_plate": "DEF-1102",
     "capacity_kg": 600, "zones": ["Al-Murabba", "Al-Nakheel", "Al-Olaya"],
     "status": "available", "rating": 4.6, "active": True,
     "notes": ""},
    {"id": "DRV-004", "name": "Tariq Al-Mansour", "name_ar": "طارق المنصور",
     "phone": "+966 56 444 8899", "license_no": "1234567893",
     "vehicle_type": "truck_small", "vehicle_plate": "GHI-5544",
     "capacity_kg": 1500, "zones": ["Diriyah", "Hittin", "Al-Sahafah"],
     "status": "available", "rating": 4.9, "active": True,
     "notes": "Big-event truck — needed for 200+ guest setups with linens."},
    {"id": "DRV-005", "name": "Khaled Al-Sharif", "name_ar": "خالد الشريف",
     "phone": "+966 50 555 1100", "license_no": "1234567894",
     "vehicle_type": "suv", "vehicle_plate": "JKL-8821",
     "capacity_kg": 250, "zones": ["Al-Olaya", "Al-Malqa", "Al-Yasmin"],
     "status": "available", "rating": 4.5, "active": True,
     "notes": "Small loads + box-lunch drop-offs to offices."},
    {"id": "DRV-006", "name": "Bassam Al-Qurashi", "name_ar": "بسام القرشي",
     "phone": "+966 54 666 7700", "license_no": "1234567895",
     "vehicle_type": "motorbike", "vehicle_plate": "MNO-3030",
     "capacity_kg": 30, "zones": ["Al-Olaya", "Al-Murabba"],
     "status": "off_duty", "rating": 4.4, "active": True,
     "notes": "Last-mile rush for forgotten items / extra dessert trays."},
]


def _default_payload() -> dict:
    return {"drivers": deepcopy(DEFAULT_DRIVERS), "assignments": []}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_all() -> dict:
    with _LOCK:
        return _load_locked()


def load_drivers() -> list[dict]:
    rows = (load_all().get("drivers") or [])
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=lambda r: (r.get("name") or r.get("name_ar") or "").lower())
    return rows


def load_assignments() -> list[dict]:
    return [a for a in (load_all().get("assignments") or [])
            if isinstance(a, dict)]


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("catering_delivery.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    drivers = d.get("drivers") if isinstance(d.get("drivers"), list) else []
    assigns = d.get("assignments") if isinstance(d.get("assignments"), list) else []
    return {
        "drivers":     [r for r in drivers if isinstance(r, dict)],
        "assignments": [a for a in assigns if isinstance(a, dict)],
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_delivery() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_all()


# ----------------------------------------------------------------------
# Drivers CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_driver_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("DRV-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"DRV-{n + 1:03d}"


def _coerce_driver(d: dict, existing: list[dict]) -> dict:
    vt = str(d.get("vehicle_type") or "van_standard").strip().lower()
    if vt not in VEHICLE_TYPES: vt = "van_standard"
    st = str(d.get("status") or "available").strip().lower()
    if st not in DRIVER_STATUSES: st = "available"
    return {
        "id":            str(d.get("id") or _next_driver_id(existing)).strip(),
        "name":          str(d.get("name") or "").strip()[:200],
        "name_ar":       str(d.get("name_ar") or "").strip()[:200],
        "phone":         _PHONE_RE.sub("", str(d.get("phone") or "")).strip()[:32],
        "license_no":    str(d.get("license_no") or "").strip()[:64],
        "vehicle_type":  vt,
        "vehicle_plate": str(d.get("vehicle_plate") or "").strip().upper()[:16],
        "capacity_kg":   max(0, int(d.get("capacity_kg") or 0)),
        "zones":         [str(z).strip()[:64]
                            for z in (d.get("zones") or []) if str(z).strip()],
        "status":        st,
        "rating":        round(max(0.0, min(5.0, float(d.get("rating") or 0))), 2),
        "active":        bool(d.get("active", True)),
        "notes":         str(d.get("notes") or "").strip()[:500],
    }


def add_driver(patch: dict) -> dict:
    if not (patch.get("name") or patch.get("name_ar") or "").strip():
        raise _Refused("Driver needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        drv = _coerce_driver(patch, payload["drivers"])
        payload["drivers"].append(drv)
        _save_locked(payload)
        return drv


def update_driver(driver_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, x in enumerate(payload["drivers"]):
            if x.get("id") == driver_id:
                merged = {**x, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = driver_id
                if not (merged.get("name") or merged.get("name_ar") or "").strip():
                    raise _Refused("Driver needs at least one name.")
                payload["drivers"][i] = _coerce_driver(merged, payload["drivers"])
                _save_locked(payload)
                return payload["drivers"][i]
    return None


def delete_driver(driver_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["drivers"])
        payload["drivers"] = [x for x in payload["drivers"]
                               if x.get("id") != driver_id]
        # Cascade — drop any assignments pointing at this driver.
        payload["assignments"] = [a for a in payload["assignments"]
                                   if a.get("driver_id") != driver_id]
        removed = len(payload["drivers"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# Assignments — one per catering order. Upsert by order_id.
# ----------------------------------------------------------------------

def _coerce_assignment(a: dict) -> dict:
    return {
        "order_id":    str(a.get("order_id") or "").strip()[:32],
        "driver_id":   str(a.get("driver_id") or "").strip()[:32],
        "depart_time": str(a.get("depart_time") or "").strip()[:8],   # "HH:MM"
        "arrive_time": str(a.get("arrive_time") or "").strip()[:8],
        "route_note":  str(a.get("route_note") or "").strip()[:400],
        "status":      str(a.get("status") or "scheduled").strip().lower()[:32],
    }


def upsert_assignment(patch: dict) -> dict:
    a = _coerce_assignment(patch)
    if not a["order_id"]:
        raise _Refused("Assignment needs an order_id.")
    with _LOCK:
        payload = _load_locked()
        found = False
        for i, x in enumerate(payload["assignments"]):
            if x.get("order_id") == a["order_id"]:
                payload["assignments"][i] = a
                found = True
                break
        if not found:
            payload["assignments"].append(a)
        _save_locked(payload)
        return a


def delete_assignment(order_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["assignments"])
        payload["assignments"] = [a for a in payload["assignments"]
                                   if a.get("order_id") != order_id]
        removed = len(payload["assignments"]) < before
        if removed:
            _save_locked(payload)
        return removed
