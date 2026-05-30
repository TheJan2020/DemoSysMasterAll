"""
Kitchen staff roster + live position simulator.

The position simulator pretends the staff are wearing BLE beacons that
ping back to anchors in each kitchen zone. For a demo we don't actually
need radios — we just compute "which zone each staff member is in right
now" as a deterministic function of time + their per-staff schedule.

Each staff member has a `schedule`: a list of (zone_id, dwell_seconds)
pairs. The simulator picks a zone by walking the schedule modulo the
cycle length, using current unix-time as the cursor. Same time → same
zone, so the page doesn't flicker on refresh, but the cursor advances
every second so polled positions actually move.

Within a zone, each staff is placed at a deterministic point computed
from their beacon_id + the current zone's bounding box (stable while
they're inside the zone, jumps when they cross to another zone — and
the SPA tweens the dot with CSS transitions for smoothness).

Storage:
    data/demos/restaurant/kitchen_staff.json

Tracked by git; `.tmp` is gitignored.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

from . import kitchen_layout as zones_mod

logger = logging.getLogger("demo_restaurant.kitchen_staff")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "kitchen_staff.json"
_LOCK = threading.Lock()

ROLES = [
    "head_chef", "sous_chef", "line_cook", "grill_cook",
    "pastry_chef", "prep_cook", "dishwasher", "stock_lead",
    "server", "manager",
]
STATUSES = ["on_shift", "on_break", "off_shift"]


_PHONE_RE = re.compile(r"[^\d+\s\-()]+")


# ----------------------------------------------------------------------
# Seed roster — 8 staff, schedules tuned to their role so the live map
# tells a believable story (chef ping-pongs hot_line ↔ pass, dishwasher
# stays put, stock lead walks the cooler/freezer/storage triangle, etc.).
# ----------------------------------------------------------------------

DEFAULT_STAFF: list[dict] = [
    {"id": "STF-001", "name": "Tarek Al-Halabi", "name_ar": "طارق الحلبي",
     "role": "head_chef", "phone": "+966 50 110 1100",
     "beacon_id": "BCN-2A41", "shift": "evening",
     "status": "on_shift", "tint": "#dc2626",
     "schedule": [
         {"zone_id": "GF-CHICKEN-STOCK", "dwell_s": 60},
         {"zone_id": "GF-MEAT-STOCK",    "dwell_s": 60},
         {"zone_id": "GF-FISH-STOCK",    "dwell_s": 30},
         {"zone_id": "GF-DRY-STORE",     "dwell_s": 45},
         {"zone_id": "GF-CHIEF-OFFICE",  "dwell_s": 40},
     ],
     "active": True, "notes": "Walks the line, plates expedite."},
    {"id": "STF-002", "name": "Yara Karam", "name_ar": "يارا كرم",
     "role": "sous_chef", "phone": "+966 55 220 2200",
     "beacon_id": "BCN-2A42", "shift": "evening",
     "status": "on_shift", "tint": "#ea580c",
     "schedule": [
         {"zone_id": "GF-MEAT-STOCK",    "dwell_s": 70},
         {"zone_id": "GF-VEG-PROCESS",   "dwell_s": 60},
         {"zone_id": "FF-CHICKEN-PROC",  "dwell_s": 40},
         {"zone_id": "GF-FISH-STOCK",    "dwell_s": 50},
     ],
     "active": True, "notes": "Floats between stations during peak."},
    {"id": "STF-003", "name": "Bashir Naim", "name_ar": "بشير نعيم",
     "role": "grill_cook", "phone": "+966 53 330 3300",
     "beacon_id": "BCN-2A43", "shift": "evening",
     "status": "on_shift", "tint": "#f97316",
     "schedule": [
         {"zone_id": "GF-MEAT-STOCK",    "dwell_s": 220},
         {"zone_id": "GF-CHICKEN-STOCK", "dwell_s": 90},
         {"zone_id": "GF-DRY-STORE",     "dwell_s": 30},
     ],
     "active": True, "notes": "Mixed grill specialist."},
    {"id": "STF-004", "name": "Rana Saad", "name_ar": "رنا سعد",
     "role": "pastry_chef", "phone": "+966 56 440 4400",
     "beacon_id": "BCN-2A44", "shift": "evening",
     "status": "on_shift", "tint": "#ec4899",
     "schedule": [
         {"zone_id": "FF-CHILLER-BREAD","dwell_s": 200},
         {"zone_id": "FF-FRZ-BREAD",    "dwell_s": 30},
         {"zone_id": "FF-CHILLER-BREAD","dwell_s": 220},
         {"zone_id": "FF-DAIRY-CH",     "dwell_s": 35},
     ],
     "active": True, "notes": "Knafeh + baklava + bread station."},
    {"id": "STF-005", "name": "Joud Mansour", "name_ar": "جود منصور",
     "role": "prep_cook", "phone": "+966 50 550 5500",
     "beacon_id": "BCN-2A45", "shift": "afternoon",
     "status": "on_shift", "tint": "#10b981",
     "schedule": [
         {"zone_id": "GF-VEG-CLEANING",  "dwell_s": 150},
         {"zone_id": "GF-VEG-PROCESS",   "dwell_s": 120},
         {"zone_id": "GF-VEG-CHILLER",   "dwell_s": 40},
         {"zone_id": "GF-VEG-PROCESSED", "dwell_s": 40},
     ],
     "active": True, "notes": "Chops + stocks the cold line."},
    {"id": "STF-006", "name": "Sami Hadid", "name_ar": "سامي هادي",
     "role": "prep_cook", "phone": "+966 55 660 6600",
     "beacon_id": "BCN-2A46", "shift": "afternoon",
     "status": "on_break", "tint": "#14b8a6",
     "schedule": [
         {"zone_id": "FF-CHICKEN-PROC", "dwell_s": 100},
         {"zone_id": "FF-FISH-PROC",    "dwell_s": 70},
         {"zone_id": "FF-MEAT-PROC",    "dwell_s": 90},
     ],
     "active": True, "notes": "On 15-min break."},
    {"id": "STF-007", "name": "Eyad Rifai", "name_ar": "إياد الرفاعي",
     "role": "dishwasher", "phone": "+966 53 770 7700",
     "beacon_id": "BCN-2A47", "shift": "evening",
     "status": "on_shift", "tint": "#a855f7",
     "schedule": [
         {"zone_id": "GF-CLEAN-POT", "dwell_s": 280},
         {"zone_id": "GF-JANITOR",   "dwell_s": 30},
     ],
     "active": True, "notes": "Anchored at clean pot."},
    {"id": "STF-008", "name": "Mounir Saade", "name_ar": "منير سعادة",
     "role": "stock_lead", "phone": "+966 56 880 8800",
     "beacon_id": "BCN-2A48", "shift": "afternoon",
     "status": "on_shift", "tint": "#0ea5e9",
     "schedule": [
         {"zone_id": "GF-DRY-STORE",       "dwell_s": 40},
         {"zone_id": "GF-PROTEIN-FRZ",     "dwell_s": 35},
         {"zone_id": "GF-RECEIVING-BAY",   "dwell_s": 50},
         {"zone_id": "GF-DISPATCH",        "dwell_s": 30},
         {"zone_id": "FF-FISH-FRZ",        "dwell_s": 30},
         {"zone_id": "FF-CHILLER-ROOM",    "dwell_s": 35},
     ],
     "active": True, "notes": "Receives + rotates stock across both floors."},
]


def _default_payload() -> dict:
    return {"staff": deepcopy(DEFAULT_STAFF)}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_staff() -> list[dict]:
    with _LOCK:
        rows = _load_locked().get("staff") or []
    rows.sort(key=lambda r: (r.get("name") or r.get("name_ar") or "").lower())
    return rows


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("kitchen_staff.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    staff = d.get("staff") if isinstance(d.get("staff"), list) else []
    return {"staff": [s for s in staff if isinstance(s, dict)]}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_staff() -> list[dict]:
    with _LOCK:
        _save_locked(_default_payload())
    return load_staff()


# ----------------------------------------------------------------------
# Position simulator — the heart of the live tracking page.
# ----------------------------------------------------------------------

def _stable_offset(beacon_id: str, w: int, h: int) -> tuple[int, int]:
    """Deterministic per-staff offset inside a zone bounding box so two
    staff in the same zone don't render on top of each other. Uses the
    bottom 16 bits of the beacon_id hash to spread them evenly."""
    h_int = int(hashlib.md5(beacon_id.encode("utf-8")).hexdigest(), 16)
    dx = (h_int & 0xFF) / 255.0       # 0..1
    dy = ((h_int >> 8) & 0xFF) / 255.0
    # Keep dots away from zone edges (padding 18 px).
    pad = 18
    fx = pad + int(dx * max(1, w - 2 * pad))
    fy = pad + int(dy * max(1, h - 2 * pad))
    return fx, fy


def _zone_for_staff(staff: dict, now_s: int) -> Optional[str]:
    schedule = staff.get("schedule") or []
    if not schedule:
        return None
    total = sum(max(1, int(s.get("dwell_s") or 0)) for s in schedule) or 1
    # Offset each staff's cursor by a beacon-derived shift so they don't
    # all turn over at the same moment.
    beacon = staff.get("beacon_id") or staff.get("id") or ""
    offset = int(hashlib.md5(beacon.encode("utf-8")).hexdigest()[:4], 16) % total
    cursor = (now_s + offset) % total
    acc = 0
    for step in schedule:
        d = max(1, int(step.get("dwell_s") or 0))
        if cursor < acc + d:
            return str(step.get("zone_id") or "")
        acc += d
    return str(schedule[-1].get("zone_id") or "")


def live_positions(now_s: Optional[int] = None) -> list[dict]:
    """Return one position row per active staff member.
    Off-shift members are returned with `zone_id=None` so the SPA can
    show them dimmed in a sidebar."""
    if now_s is None:
        now_s = int(time.time())
    layout = zones_mod.load_layout()
    zones_by_id = {z["id"]: z for z in layout["zones"]}
    out: list[dict] = []
    for s in load_staff():
        if not s.get("active"): continue
        status = (s.get("status") or "on_shift").lower()
        if status == "off_shift":
            out.append({
                "staff_id":   s["id"],
                "name":       s.get("name") or "",
                "name_ar":    s.get("name_ar") or "",
                "role":       s.get("role") or "",
                "beacon_id":  s.get("beacon_id") or "",
                "tint":       s.get("tint") or "#94a3b8",
                "status":     "off_shift",
                "zone_id":    None,
                "x":          None,
                "y":          None,
                "dwell_s":    0,
            })
            continue
        zone_id = _zone_for_staff(s, now_s)
        zone = zones_by_id.get(zone_id) if zone_id else None
        if not zone:
            continue
        dx, dy = _stable_offset(s.get("beacon_id") or s["id"],
                                  zone.get("width") or 100,
                                  zone.get("height") or 80)
        # Compute "how long they've been in the zone" so the UI can show
        # a dwell timer next to each dot.
        dwell_s = _dwell_in_current(s, now_s)
        out.append({
            "staff_id":   s["id"],
            "name":       s.get("name") or "",
            "name_ar":    s.get("name_ar") or "",
            "role":       s.get("role") or "",
            "beacon_id":  s.get("beacon_id") or "",
            "tint":       s.get("tint") or "#64748b",
            "status":     status,
            "zone_id":    zone_id,
            "x":          int(zone["x"]) + dx,
            "y":          int(zone["y"]) + dy,
            "dwell_s":    dwell_s,
        })
    return out


def _dwell_in_current(staff: dict, now_s: int) -> int:
    """Seconds since the staff entered their current zone."""
    schedule = staff.get("schedule") or []
    if not schedule: return 0
    total = sum(max(1, int(s.get("dwell_s") or 0)) for s in schedule) or 1
    beacon = staff.get("beacon_id") or staff.get("id") or ""
    offset = int(hashlib.md5(beacon.encode("utf-8")).hexdigest()[:4], 16) % total
    cursor = (now_s + offset) % total
    acc = 0
    for step in schedule:
        d = max(1, int(step.get("dwell_s") or 0))
        if cursor < acc + d:
            return cursor - acc
        acc += d
    return 0


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_staff_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("STF-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"STF-{n + 1:03d}"


def _coerce_step(s: dict) -> dict:
    return {
        "zone_id": str(s.get("zone_id") or "").strip()[:32],
        "dwell_s": max(1, int(s.get("dwell_s") or 60)),
    }


def _coerce_staff(s: dict, existing: list[dict]) -> dict:
    role = str(s.get("role") or "line_cook").strip().lower()
    if role not in ROLES: role = "line_cook"
    status = str(s.get("status") or "on_shift").strip().lower()
    if status not in STATUSES: status = "on_shift"
    sched = s.get("schedule") or []
    if not isinstance(sched, list): sched = []
    return {
        "id":         str(s.get("id") or _next_staff_id(existing)).strip(),
        "name":       str(s.get("name") or "").strip()[:200],
        "name_ar":    str(s.get("name_ar") or "").strip()[:200],
        "role":       role,
        "phone":      _PHONE_RE.sub("", str(s.get("phone") or "")).strip()[:32],
        "beacon_id":  str(s.get("beacon_id") or "").strip().upper()[:32],
        "shift":      str(s.get("shift") or "evening").strip().lower()[:32],
        "status":     status,
        "tint":       str(s.get("tint") or "#64748b").strip()[:16],
        "schedule":   [_coerce_step(x) for x in sched if isinstance(x, dict)],
        "active":     bool(s.get("active", True)),
        "notes":      str(s.get("notes") or "").strip()[:500],
    }


def add_staff(patch: dict) -> dict:
    if not (patch.get("name") or patch.get("name_ar") or "").strip():
        raise _Refused("Staff needs at least one name.")
    with _LOCK:
        payload = _load_locked()
        st = _coerce_staff(patch, payload["staff"])
        payload["staff"].append(st)
        _save_locked(payload)
    return st


def update_staff(staff_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, s in enumerate(payload["staff"]):
            if s.get("id") == staff_id:
                merged = {**s, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = staff_id
                payload["staff"][i] = _coerce_staff(merged, payload["staff"])
                _save_locked(payload)
                return payload["staff"][i]
    return None


def delete_staff(staff_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["staff"])
        payload["staff"] = [s for s in payload["staff"] if s.get("id") != staff_id]
        removed = len(payload["staff"]) < before
        if removed:
            _save_locked(payload)
        return removed
