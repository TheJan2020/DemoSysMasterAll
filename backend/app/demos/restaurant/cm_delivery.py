"""
Custom Meals — delivery drivers + daily route assignments.

Tied to subscriptions (not to one-off orders the way Catering's delivery
is). One driver can be assigned to multiple subscriptions for a given
delivery date; the daily manifest aggregator joins active schedules +
client address + driver into a single route brief.

Storage:
    data/demos/restaurant/cm_delivery.json
        { "drivers": [...], "assignments": [...] }

Assignment schema (one row per subscription per day, persisted only
when the operator pins it explicitly — unpinned routes are computed
from the driver's zone preferences):
    {
      "subscription_id": "SUB-001",
      "date":            "2026-05-19",
      "driver_id":       "CMD-001",
      "depart_time":     "07:30",
      "arrive_time":     "08:15",
      "route_note":      "Use service gate B"
    }

Tracked by git (operator content); `.tmp` from atomic writes is gitignored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.cm_delivery")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "cm_delivery.json"
_LOCK = threading.Lock()

VEHICLE_TYPES = ["van_refrigerated", "van_standard", "suv", "motorbike",
                  "scooter"]
DRIVER_STATUSES = ["available", "on_route", "off_duty"]

_PHONE_RE = re.compile(r"[^\d+\s\-()]+")


# ----------------------------------------------------------------------
# Seed — 4 drivers tuned to Custom Meals' light-weight subscription
# deliveries (insulated bags, not banquet trays). Riyadh districts only.
# ----------------------------------------------------------------------

DEFAULT_DRIVERS: list[dict] = [
    {"id": "CMD-001", "name": "Hassan Al-Yami",   "name_ar": "حسن اليامي",
     "phone": "+966 50 200 1100", "license_no": "2200000001",
     "vehicle_type": "van_refrigerated", "vehicle_plate": "RYD-1001",
     "shift": "morning",   "capacity_orders": 35,
     "zones": ["Al-Olaya", "Al-Murabba", "Al-Sahafah"],
     "status": "available", "rating": 4.8, "active": True,
     "notes": "Lead driver — primary breakfast route."},
    {"id": "CMD-002", "name": "Ibrahim Al-Khalil","name_ar": "إبراهيم الخليل",
     "phone": "+966 55 300 2200", "license_no": "2200000002",
     "vehicle_type": "van_refrigerated", "vehicle_plate": "RYD-1002",
     "shift": "morning",   "capacity_orders": 35,
     "zones": ["Al-Yasmin", "Al-Malqa", "Al-Nakheel"],
     "status": "available", "rating": 4.7, "active": True,
     "notes": "Covers north Riyadh — fastest at Al-Yasmin compounds."},
    {"id": "CMD-003", "name": "Majid Al-Zahrani", "name_ar": "ماجد الزهراني",
     "phone": "+966 56 400 3300", "license_no": "2200000003",
     "vehicle_type": "suv",              "vehicle_plate": "RYD-1003",
     "shift": "afternoon", "capacity_orders": 20,
     "zones": ["Hittin", "Diriyah"],
     "status": "available", "rating": 4.6, "active": True,
     "notes": ""},
    {"id": "CMD-004", "name": "Rayan Al-Mutairi", "name_ar": "ريان المطيري",
     "phone": "+966 53 500 4400", "license_no": "2200000004",
     "vehicle_type": "motorbike",        "vehicle_plate": "RYD-1004",
     "shift": "evening",   "capacity_orders": 12,
     "zones": ["Al-Olaya", "Al-Murabba"],
     "status": "off_duty", "rating": 4.5, "active": True,
     "notes": "Evening dinner deliveries within 5 km radius."},
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
    rows = load_all().get("drivers") or []
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
        logger.exception("cm_delivery.json corrupt — falling back to seed")
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
        if rid.startswith("CMD-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"CMD-{n + 1:03d}"


_SHIFTS = ("morning", "afternoon", "evening", "anytime")


def _coerce_driver(d: dict, existing: list[dict]) -> dict:
    vt = str(d.get("vehicle_type") or "van_standard").strip().lower()
    if vt not in VEHICLE_TYPES: vt = "van_standard"
    st = str(d.get("status") or "available").strip().lower()
    if st not in DRIVER_STATUSES: st = "available"
    shift = str(d.get("shift") or "morning").strip().lower()
    if shift not in _SHIFTS: shift = "morning"
    return {
        "id":              str(d.get("id") or _next_driver_id(existing)).strip(),
        "name":            str(d.get("name") or "").strip()[:200],
        "name_ar":         str(d.get("name_ar") or "").strip()[:200],
        "phone":           _PHONE_RE.sub("", str(d.get("phone") or "")).strip()[:32],
        "license_no":      str(d.get("license_no") or "").strip()[:64],
        "vehicle_type":    vt,
        "vehicle_plate":   str(d.get("vehicle_plate") or "").strip().upper()[:16],
        "shift":           shift,
        "capacity_orders": max(0, int(d.get("capacity_orders") or 0)),
        "zones":           [str(z).strip()[:64]
                              for z in (d.get("zones") or []) if str(z).strip()],
        "status":          st,
        "rating":          round(max(0.0, min(5.0, float(d.get("rating") or 0))), 2),
        "active":          bool(d.get("active", True)),
        "notes":           str(d.get("notes") or "").strip()[:500],
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
        # Cascade — drop any assignments pinning this driver.
        payload["assignments"] = [a for a in payload["assignments"]
                                   if a.get("driver_id") != driver_id]
        removed = len(payload["drivers"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# Assignments — upsert by (subscription_id, date).
# ----------------------------------------------------------------------

def _coerce_assignment(a: dict) -> dict:
    return {
        "subscription_id": str(a.get("subscription_id") or "").strip()[:32],
        "date":            str(a.get("date") or "").strip()[:10],
        "driver_id":       str(a.get("driver_id") or "").strip()[:32],
        "depart_time":     str(a.get("depart_time") or "").strip()[:8],
        "arrive_time":     str(a.get("arrive_time") or "").strip()[:8],
        "route_note":      str(a.get("route_note") or "").strip()[:400],
    }


def upsert_assignment(patch: dict) -> dict:
    a = _coerce_assignment(patch)
    if not a["subscription_id"] or not a["date"]:
        raise _Refused("Assignment needs subscription_id + date.")
    with _LOCK:
        payload = _load_locked()
        for i, x in enumerate(payload["assignments"]):
            if (x.get("subscription_id") == a["subscription_id"]
                    and x.get("date") == a["date"]):
                payload["assignments"][i] = a
                _save_locked(payload)
                return a
        payload["assignments"].append(a)
        _save_locked(payload)
        return a


def delete_assignment(subscription_id: str, date: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["assignments"])
        payload["assignments"] = [a for a in payload["assignments"]
                                   if not (a.get("subscription_id") == subscription_id
                                            and a.get("date") == date)]
        removed = len(payload["assignments"]) < before
        if removed:
            _save_locked(payload)
        return removed
