"""
Kitchen appliances — fridges, freezers, chillers, dishwashers, ovens.

Each appliance carries: target temperature, alarm thresholds, rated
power, current sensor readings (temp + power + door), and a 24-hour
history of (temp, power) samples for the graphs on the Appliances
page.

The "current" readings + history are SYNTHESISED at request time from
a deterministic generator keyed on (appliance_id, hour). This means:
  * Page refreshes see slightly different values per minute (jitter),
    so the page feels live.
  * Two operators looking at the same minute see the same graph (no
    flicker).
  * No background thread, no DB, no real sensor — pure demo data.

Acknowledged limitations: the temperature curve is plausible but
schematic — fridges hold target ±0.4°C with brief 1-1.5°C spikes
during door-open intervals + defrost. Power follows compressor cycling.

Storage:
    data/demos/restaurant/kitchen_appliances.json   — catalog (static)
    (no on-disk history; computed at request time)

Tracked by git; `.tmp` is gitignored.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.kitchen_appliances")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "kitchen_appliances.json"
_LOCK = threading.Lock()

APPLIANCE_TYPES = [
    "walk_in_freezer", "walk_in_cooler",
    "reach_in_fridge", "reach_in_freezer",
    "blast_chiller",   "beverage_fridge",
    "ice_maker",       "prep_table_fridge",
    "combi_oven",      "dishwasher",
]

STATUSES = ["running", "alarm", "off"]
ALERT_KINDS = [
    "temp_high", "temp_low", "door_open_too_long",
    "power_spike", "defrost_overrun", "sensor_offline",
]


# ----------------------------------------------------------------------
# Seed — 10 appliances with realistic Saudi-restaurant naming + zone refs.
# ----------------------------------------------------------------------

DEFAULT_APPLIANCES: list[dict] = [
    {"id": "APL-001", "name_en": "Walk-in Freezer #1", "name_ar": "غرفة التجميد ١",
     "type": "walk_in_freezer", "zone_id": "ZN-WIF",
     "model": "Foster XR1300L",     "age_years": 3,
     "target_temp_c": -18.0, "alarm_low_c": -25.0, "alarm_high_c": -14.0,
     "rated_power_kw": 2.4,  "capacity_l": 1300,
     "active": True, "installed_at": "2023-03-12",
     "notes": "Primary protein freezer — lamb, chicken, ground beef."},
    {"id": "APL-002", "name_en": "Walk-in Freezer #2", "name_ar": "غرفة التجميد ٢",
     "type": "walk_in_freezer", "zone_id": "ZN-WIF",
     "model": "Foster XR700L",      "age_years": 5,
     "target_temp_c": -20.0, "alarm_low_c": -26.0, "alarm_high_c": -15.0,
     "rated_power_kw": 1.8,  "capacity_l": 700,
     "active": True, "installed_at": "2021-08-04",
     "notes": "Pastry + ice cream — slightly cooler target."},
    {"id": "APL-003", "name_en": "Walk-in Cooler (Chiller Room)", "name_ar": "غرفة التبريد",
     "type": "walk_in_cooler", "zone_id": "ZN-WIC",
     "model": "Foster CR1800L",     "age_years": 3,
     "target_temp_c": 3.0,  "alarm_low_c": -1.0, "alarm_high_c": 6.0,
     "rated_power_kw": 1.5,  "capacity_l": 1800,
     "active": True, "installed_at": "2023-03-12",
     "notes": "Mezze + dairy + fresh produce."},
    {"id": "APL-004", "name_en": "Cold Line Reach-in Fridge", "name_ar": "ثلاجة الخط البارد",
     "type": "reach_in_fridge", "zone_id": "ZN-COLD",
     "model": "Williams HJ1SA",     "age_years": 2,
     "target_temp_c": 3.0,  "alarm_low_c": -1.0, "alarm_high_c": 6.0,
     "rated_power_kw": 0.6,  "capacity_l": 600,
     "active": True, "installed_at": "2024-01-20",
     "notes": "Mise-en-place for cold mezze service."},
    {"id": "APL-005", "name_en": "Pastry Reach-in Fridge", "name_ar": "ثلاجة الحلويات",
     "type": "reach_in_fridge", "zone_id": "ZN-PASTRY",
     "model": "Williams HJ1TSA",    "age_years": 4,
     "target_temp_c": 4.0,  "alarm_low_c": 0.0,  "alarm_high_c": 7.5,
     "rated_power_kw": 0.5,  "capacity_l": 600,
     "active": True, "installed_at": "2022-05-15",
     "notes": "Knafeh + mhalabia + cream prep."},
    {"id": "APL-006", "name_en": "Hot Line Prep-Table Fridge", "name_ar": "ثلاجة طاولة التحضير",
     "type": "prep_table_fridge", "zone_id": "ZN-HOT",
     "model": "True TSSU-72",       "age_years": 6,
     "target_temp_c": 4.0,  "alarm_low_c": 0.0,  "alarm_high_c": 8.0,
     "rated_power_kw": 0.55, "capacity_l": 300,
     "active": True, "installed_at": "2020-09-01",
     "notes": "Door cycles a LOT during service — see alarm history."},
    {"id": "APL-007", "name_en": "Blast Chiller", "name_ar": "مبرد سريع",
     "type": "blast_chiller", "zone_id": "ZN-PREP",
     "model": "Irinox MF 30.1",     "age_years": 4,
     "target_temp_c": -2.0, "alarm_low_c": -10.0, "alarm_high_c": 5.0,
     "rated_power_kw": 1.6,  "capacity_l": 80,
     "active": True, "installed_at": "2022-02-10",
     "notes": "Used for cooling rice + sauces post-cook."},
    {"id": "APL-008", "name_en": "Beverage Fridge", "name_ar": "ثلاجة المشروبات",
     "type": "beverage_fridge", "zone_id": "ZN-PASS",
     "model": "True GDM-23",        "age_years": 2,
     "target_temp_c": 5.0,  "alarm_low_c": 1.0,  "alarm_high_c": 8.0,
     "rated_power_kw": 0.4,  "capacity_l": 650,
     "active": True, "installed_at": "2024-03-05",
     "notes": "Mint lemonade + ayran + bottled water."},
    {"id": "APL-009", "name_en": "Ice Maker", "name_ar": "آلة الثلج",
     "type": "ice_maker", "zone_id": "ZN-DISH",
     "model": "Hoshizaki KM-515MAH","age_years": 2,
     "target_temp_c": -3.0, "alarm_low_c": -8.0, "alarm_high_c": 2.0,
     "rated_power_kw": 1.0,  "capacity_l": 200,
     "active": True, "installed_at": "2024-02-01",
     "notes": ""},
    {"id": "APL-010", "name_en": "Dishwasher (Conveyor)", "name_ar": "غسالة الصحون",
     "type": "dishwasher", "zone_id": "ZN-DISH",
     "model": "Hobart CLPS66e",     "age_years": 5,
     "target_temp_c": 65.0, "alarm_low_c": 55.0, "alarm_high_c": 75.0,
     "rated_power_kw": 12.0, "capacity_l": 0,
     "active": True, "installed_at": "2021-04-18",
     "notes": "Wash temp not refrigeration — green when above 60°C."},
]


def _default_payload() -> dict:
    return {"appliances": deepcopy(DEFAULT_APPLIANCES),
            "alerts": _seed_alerts()}


def _seed_alerts() -> list[dict]:
    now = int(time.time())
    return [
        {"id": "ALR-0001", "appliance_id": "APL-006",
         "kind": "door_open_too_long", "severity": "warn",
         "at": now - 12 * 60,
         "message": "Hot Line prep fridge door open > 5 min during 14:30 service.",
         "resolved": True, "resolved_at": now - 8 * 60},
        {"id": "ALR-0002", "appliance_id": "APL-002",
         "kind": "defrost_overrun", "severity": "info",
         "at": now - 3 * 3600,
         "message": "Walk-in Freezer #2 defrost cycle ran 12 min longer than expected.",
         "resolved": True, "resolved_at": now - 2 * 3600},
        {"id": "ALR-0003", "appliance_id": "APL-001",
         "kind": "temp_high", "severity": "alarm",
         "at": now - 25 * 60,
         "message": "Walk-in Freezer #1 climbed above -14°C target during stock rotation.",
         "resolved": False, "resolved_at": None},
    ]


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_catalog() -> dict:
    with _LOCK:
        return _load_locked()


def load_appliances() -> list[dict]:
    rows = load_catalog().get("appliances") or []
    rows.sort(key=lambda r: (r.get("type") or "", r.get("name_en") or ""))
    return rows


def load_alerts() -> list[dict]:
    rows = load_catalog().get("alerts") or []
    rows.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return rows


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("kitchen_appliances.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    apps   = d.get("appliances") if isinstance(d.get("appliances"), list) else []
    alerts = d.get("alerts")     if isinstance(d.get("alerts"),     list) else []
    return {
        "appliances": [a for a in apps if isinstance(a, dict)],
        "alerts":     [a for a in alerts if isinstance(a, dict)],
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_catalog() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_catalog()


# ----------------------------------------------------------------------
# Synthetic readings — deterministic per (appliance, minute).
# ----------------------------------------------------------------------

def _hash01(seed: str) -> float:
    """Hash-derived float in [0, 1) — stable for the same seed."""
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16)
    return h / 0x100000000


def _temp_at(appliance: dict, ts: int) -> float:
    """Compute appliance temperature at unix-time `ts` (resolution: minute).
    Models:
      • base = target_temp_c
      • cyclic compressor drift: ±0.3°C around target on a ~6-min cycle
      • door-open spikes: small bump in lunch + dinner hours
      • for the seeded alarm appliance (APL-001), nudge it above alarm.
    """
    target = float(appliance.get("target_temp_c") or 0.0)
    minute = ts // 60
    cycle = math.sin(minute / 6.0 * math.pi) * 0.3
    hour = time.localtime(ts).tm_hour

    door_bump = 0.0
    # Service hours: 12-15 and 19-23 → small bumps
    in_service = (12 <= hour <= 15) or (19 <= hour <= 23)
    if in_service:
        # Door opens roughly every 8 min during service
        if (minute % 8) < 2:
            door_bump = 0.8 + _hash01(f"{appliance['id']}-{minute // 8}") * 0.6

    # Per-appliance type quirks
    a_type = (appliance.get("type") or "").lower()
    if a_type == "dishwasher":
        # Cycles around 65°C, drops during idle (3-6 AM, 16-19)
        if (3 <= hour <= 5) or hour == 16:
            return target - 8.0 + cycle
        return target + cycle * 2 + door_bump * 2
    if a_type == "ice_maker":
        # Ice production cycles
        if minute % 20 < 5:
            return target + 2.5
        return target + cycle * 0.5

    # SEEDED HOTSPOT — Walk-in Freezer #1 has the active temp_high alert.
    # Push it just above its alarm threshold (-14) so the UI shows red.
    if appliance["id"] == "APL-001":
        return -13.4 + math.sin(minute / 4.0) * 0.3

    return target + cycle + door_bump


def _power_at(appliance: dict, ts: int) -> float:
    """Power draw in kW at unix-time `ts`. Roughly 30-70% of rated when
    compressor is cycling, 5-15% on standby."""
    rated = float(appliance.get("rated_power_kw") or 0.5)
    minute = ts // 60
    # Compressor on/off — rough 8-min duty cycle of 65%.
    on_window = (minute % 8) < 5
    a_type = (appliance.get("type") or "").lower()

    if a_type == "dishwasher":
        # Spikes during wash cycles, near-zero otherwise.
        if (minute % 6) < 2:
            return rated * 0.85
        return rated * 0.05
    if a_type == "ice_maker":
        if minute % 20 < 8:
            return rated * 0.7
        return rated * 0.15

    if on_window:
        return rated * (0.55 + _hash01(f"{appliance['id']}-pwr-{minute // 8}") * 0.25)
    return rated * (0.08 + _hash01(f"{appliance['id']}-idle-{minute // 8}") * 0.07)


def _door_status(appliance: dict, ts: int) -> tuple[str, int]:
    """('closed'|'open', door_last_open_unix). Walk-ins open briefly
    during service hours; reach-ins more often."""
    a_type = (appliance.get("type") or "").lower()
    hour = time.localtime(ts).tm_hour
    minute = ts // 60

    if a_type in ("dishwasher", "ice_maker"):
        return "closed", ts - 600

    # Probability seed varies per appliance
    seed = f"{appliance['id']}-door-{minute}"
    p = _hash01(seed)
    in_service = (12 <= hour <= 15) or (19 <= hour <= 23)

    if a_type.startswith("walk_in"):
        # Walk-ins open rarely — about 4% of minutes during service.
        if in_service and p < 0.04: return "open", ts - 30
        return "closed", ts - int(_hash01(f"{appliance['id']}-lopn") * 1800)
    # Reach-ins / prep tables open often during service.
    if in_service and p < 0.18:
        return "open", ts - 15
    return "closed", ts - int(_hash01(f"{appliance['id']}-lopn") * 600)


def current_reading(appliance: dict, ts: Optional[int] = None) -> dict:
    if ts is None: ts = int(time.time())
    temp = round(_temp_at(appliance, ts), 1)
    pwr  = round(_power_at(appliance, ts), 2)
    door, last_open = _door_status(appliance, ts)
    high = float(appliance.get("alarm_high_c") or 999)
    low  = float(appliance.get("alarm_low_c")  or -999)
    a_type = (appliance.get("type") or "").lower()
    # For heated appliances (dishwasher), reverse alarm semantics: alarm
    # if BELOW low threshold.
    if a_type == "dishwasher":
        in_alarm = temp < low
    else:
        in_alarm = (temp > high) or (temp < low)
    status = "alarm" if in_alarm else "running"
    return {
        "appliance_id":  appliance["id"],
        "temp_c":        temp,
        "power_kw":      pwr,
        "door_status":   door,
        "door_last_open": last_open,
        "status":        status,
        "in_alarm":      in_alarm,
        "target_temp_c": appliance.get("target_temp_c"),
        "alarm_low_c":   appliance.get("alarm_low_c"),
        "alarm_high_c":  appliance.get("alarm_high_c"),
        "rated_power_kw": appliance.get("rated_power_kw"),
        "as_of":         ts,
    }


def history_for(appliance_id: str, hours: int = 24) -> dict:
    """Generate `hours` hours of history at 15-min resolution. Each row:
    {"t": unix_ts, "temp_c": …, "power_kw": …, "door": "open"|"closed"}.
    Returns the appliance metadata too for convenience."""
    catalog = load_catalog()
    appliance = next((a for a in catalog["appliances"] if a.get("id") == appliance_id), None)
    if not appliance:
        return {}
    now = int(time.time())
    start = now - hours * 3600
    samples: list[dict] = []
    # 15-min resolution → 4 samples per hour
    for i in range(hours * 4):
        ts = start + i * 900
        door, _ = _door_status(appliance, ts)
        samples.append({
            "t":        ts,
            "temp_c":   round(_temp_at(appliance, ts), 1),
            "power_kw": round(_power_at(appliance, ts), 2),
            "door":     door,
        })
    return {"appliance": appliance, "samples": samples}


# ----------------------------------------------------------------------
# CRUD (catalog edits — operator can add/remove appliances)
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_appliance_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("APL-"):
            try: n = max(n, int(rid[4:]))
            except Exception: pass
    return f"APL-{n + 1:03d}"


def _coerce_appliance(a: dict, existing: list[dict]) -> dict:
    typ = str(a.get("type") or "reach_in_fridge").strip().lower()
    if typ not in APPLIANCE_TYPES: typ = "reach_in_fridge"
    return {
        "id":              str(a.get("id") or _next_appliance_id(existing)).strip(),
        "name_en":         str(a.get("name_en") or "").strip()[:200],
        "name_ar":         str(a.get("name_ar") or "").strip()[:200],
        "type":            typ,
        "zone_id":         str(a.get("zone_id") or "").strip()[:32],
        "model":           str(a.get("model") or "").strip()[:120],
        "age_years":       max(0, int(a.get("age_years") or 0)),
        "target_temp_c":   round(float(a.get("target_temp_c") or 0), 2),
        "alarm_low_c":     round(float(a.get("alarm_low_c")  or 0), 2),
        "alarm_high_c":    round(float(a.get("alarm_high_c") or 0), 2),
        "rated_power_kw":  round(max(0.0, float(a.get("rated_power_kw") or 0)), 2),
        "capacity_l":      max(0, int(a.get("capacity_l") or 0)),
        "active":          bool(a.get("active", True)),
        "installed_at":    str(a.get("installed_at") or "").strip()[:32],
        "notes":           str(a.get("notes") or "").strip()[:500],
    }


def add_appliance(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Appliance needs at least one name.")
    with _LOCK:
        payload = _load_locked()
        a = _coerce_appliance(patch, payload["appliances"])
        payload["appliances"].append(a)
        _save_locked(payload)
    return a


def update_appliance(app_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, a in enumerate(payload["appliances"]):
            if a.get("id") == app_id:
                merged = {**a, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = app_id
                payload["appliances"][i] = _coerce_appliance(merged, payload["appliances"])
                _save_locked(payload)
                return payload["appliances"][i]
    return None


def delete_appliance(app_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["appliances"])
        payload["appliances"] = [a for a in payload["appliances"] if a.get("id") != app_id]
        # Cascade — drop alerts pointing at this appliance.
        payload["alerts"] = [al for al in payload["alerts"]
                              if al.get("appliance_id") != app_id]
        removed = len(payload["appliances"]) < before
        if removed:
            _save_locked(payload)
        return removed


def resolve_alert(alert_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        changed = False
        for al in payload["alerts"]:
            if al.get("id") == alert_id and not al.get("resolved"):
                al["resolved"]    = True
                al["resolved_at"] = int(time.time())
                changed = True
                break
        if changed:
            _save_locked(payload)
        return changed
