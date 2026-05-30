"""
Kitchen AI-Camera config — the **people** and **actions** that Gemini
should look for when analysing a kitchen camera snapshot.

The vision pipeline elsewhere (Frigate → snapshot → Gemini) doesn't care
which person/action labels exist; this catalog is just operator-supplied
context we embed in the prompt. Add a person + a description hint that
helps Gemini recognise them by appearance/role, and add an action +
where it normally happens. The kitchen camera page renders both as
editable lists and Gemini matches against them.

We also track the per-camera assignment — i.e. "this Frigate camera
points at the Sandwich Station" — so the prompt can name the station
in the question.

Storage:
    data/demos/restaurant/kitchen_camera_config.json

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

logger = logging.getLogger("demo_restaurant.kitchen_camera_config")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "kitchen_camera_config.json"
_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# Seed — Lebanese-restaurant kitchen people + common stations/actions.
# ----------------------------------------------------------------------

DEFAULT_PEOPLE: list[dict] = [
    {"id": "KP-001", "name": "Tarek Al-Halabi",
     "role": "head_chef",
     "description":
         "Head chef. Tall, wearing a white double-breasted chef coat with "
         "rolled-up sleeves, dark trousers, white skullcap, well-trimmed "
         "beard. Often near the pass or hot line directing plating.",
     "active": True},
    {"id": "KP-002", "name": "Yara Karam",
     "role": "sous_chef",
     "description":
         "Sous chef. Medium height, white chef coat (single-breasted), "
         "black apron, hair tied back with a white bandana. Floats between "
         "stations.",
     "active": True},
    {"id": "KP-003", "name": "Bashir Naim",
     "role": "grill_cook",
     "description":
         "Grill cook. Black apron over a black t-shirt + black baseball "
         "cap. Always at the grill / mixed-grill station with tongs.",
     "active": True},
    {"id": "KP-004", "name": "Rana Saad",
     "role": "pastry_chef",
     "description":
         "Pastry chef. White coat + pink apron, hair under a white cap. "
         "Works at the pastry / dessert station with knafeh and baklava.",
     "active": True},
    {"id": "KP-005", "name": "Joud Mansour",
     "role": "prep_cook",
     "description":
         "Prep cook. Black t-shirt + black apron, blue disposable gloves "
         "while chopping. At the prep table or near the walk-in cooler.",
     "active": True},
    {"id": "KP-006", "name": "Eyad Rifai",
     "role": "dishwasher",
     "description":
         "Dishwasher. Black rubber apron, yellow gloves, always at the "
         "dish station behind a conveyor dishwasher.",
     "active": True},
]


DEFAULT_ACTIONS: list[dict] = [
    {"id": "KA-001", "label": "Preparing sandwiches",
     "description":
         "Building sandwiches/wraps: cutting bread, assembling fillings, "
         "wrapping in paper or grilling on a panini press.",
     "station_zone_id": "ZN-COLD",
     "color": "#0ea5e9",
     "active": True},
    {"id": "KA-002", "label": "Cleaning station",
     "description":
         "Wiping a worktop with a cloth, spraying sanitiser, mopping the "
         "floor, scrubbing a station, taking out trash.",
     "station_zone_id": "ZN-DISH",
     "color": "#a855f7",
     "active": True},
    {"id": "KA-003", "label": "Grilling meat",
     "description":
         "Turning skewers, kebabs or burgers on a charcoal/gas grill; "
         "flipping shawarma meat on a vertical rotisserie.",
     "station_zone_id": "ZN-HOT",
     "color": "#ef4444",
     "active": True},
    {"id": "KA-004", "label": "Plating dishes at the pass",
     "description":
         "Arranging food on a plate at the pass/expediter window for "
         "service, garnishing, wiping plate rims, calling tickets.",
     "station_zone_id": "ZN-PASS",
     "color": "#f59e0b",
     "active": True},
    {"id": "KA-005", "label": "Mise-en-place / prep work",
     "description":
         "Chopping vegetables, deboning chicken, portioning rice into "
         "containers, restocking the line, weighing ingredients.",
     "station_zone_id": "ZN-PREP",
     "color": "#22c55e",
     "active": True},
    {"id": "KA-006", "label": "Pastry / dessert work",
     "description":
         "Rolling pastry, layering knafeh, piping cream, dusting sugar, "
         "arranging baklava trays.",
     "station_zone_id": "ZN-PASTRY",
     "color": "#ec4899",
     "active": True},
    {"id": "KA-007", "label": "Washing dishes",
     "description":
         "Loading or unloading the conveyor dishwasher, hand-scrubbing "
         "pots in a sink, racking glassware.",
     "station_zone_id": "ZN-DISH",
     "color": "#a855f7",
     "active": True},
    {"id": "KA-008", "label": "Idle / off-task",
     "description":
         "Standing around not actively working — phone in hand, leaning "
         "on a counter, chatting away from their station.",
     "station_zone_id": "",
     "color": "#94a3b8",
     "active": True},
]


# Camera-to-zone mapping. Empty by default; the operator sets it on the
# AI-Camera page after selecting which Frigate camera points at which
# kitchen zone.
DEFAULT_CAMERA_ASSIGNMENTS: list[dict] = []


def _default_payload() -> dict:
    return {
        "people":              deepcopy(DEFAULT_PEOPLE),
        "actions":             deepcopy(DEFAULT_ACTIONS),
        "camera_assignments":  deepcopy(DEFAULT_CAMERA_ASSIGNMENTS),
    }


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_config() -> dict:
    with _LOCK:
        return _load_locked()


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("kitchen_camera_config.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    def _list(key: str) -> list[dict]:
        v = d.get(key)
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    return {
        "people":             _list("people"),
        "actions":            _list("actions"),
        "camera_assignments": _list("camera_assignments"),
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_config() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_config()


# ----------------------------------------------------------------------
# CRUD — people
# ----------------------------------------------------------------------

class _Refused(ValueError):
    pass


def _next_id(rows: list[dict], prefix: str) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith(prefix):
            try: n = max(n, int(rid[len(prefix):]))
            except Exception: pass
    return f"{prefix}{n + 1:03d}"


def _coerce_person(p: dict, existing: list[dict]) -> dict:
    return {
        "id":          str(p.get("id") or _next_id(existing, "KP-")).strip(),
        "name":        str(p.get("name") or "").strip()[:200],
        "role":        str(p.get("role") or "").strip()[:64],
        "description": str(p.get("description") or "").strip()[:1000],
        "active":      bool(p.get("active", True)),
    }


def add_person(patch: dict) -> dict:
    if not (patch.get("name") or "").strip():
        raise _Refused("Person needs a name.")
    with _LOCK:
        payload = _load_locked()
        p = _coerce_person(patch, payload["people"])
        payload["people"].append(p)
        _save_locked(payload)
    return p


def update_person(person_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, p in enumerate(payload["people"]):
            if p.get("id") == person_id:
                merged = {**p, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = person_id
                if not (merged.get("name") or "").strip():
                    raise _Refused("Person needs a name.")
                payload["people"][i] = _coerce_person(merged, payload["people"])
                _save_locked(payload)
                return payload["people"][i]
    return None


def delete_person(person_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["people"])
        payload["people"] = [p for p in payload["people"] if p.get("id") != person_id]
        removed = len(payload["people"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# CRUD — actions
# ----------------------------------------------------------------------

def _coerce_action(a: dict, existing: list[dict]) -> dict:
    return {
        "id":              str(a.get("id") or _next_id(existing, "KA-")).strip(),
        "label":           str(a.get("label") or "").strip()[:200],
        "description":     str(a.get("description") or "").strip()[:1000],
        "station_zone_id": str(a.get("station_zone_id") or "").strip()[:32],
        "color":           str(a.get("color") or "#64748b").strip()[:16],
        "active":          bool(a.get("active", True)),
    }


def add_action(patch: dict) -> dict:
    if not (patch.get("label") or "").strip():
        raise _Refused("Action needs a label.")
    with _LOCK:
        payload = _load_locked()
        a = _coerce_action(patch, payload["actions"])
        payload["actions"].append(a)
        _save_locked(payload)
    return a


def update_action(action_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, a in enumerate(payload["actions"]):
            if a.get("id") == action_id:
                merged = {**a, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = action_id
                if not (merged.get("label") or "").strip():
                    raise _Refused("Action needs a label.")
                payload["actions"][i] = _coerce_action(merged, payload["actions"])
                _save_locked(payload)
                return payload["actions"][i]
    return None


def delete_action(action_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["actions"])
        payload["actions"] = [a for a in payload["actions"] if a.get("id") != action_id]
        removed = len(payload["actions"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# Camera-zone assignments — small map of "Frigate camera name → kitchen zone".
# ----------------------------------------------------------------------

def set_camera_zone(camera: str, zone_id: str, label: str = "") -> dict:
    camera = (camera or "").strip()
    zone_id = (zone_id or "").strip()
    label = (label or "").strip()
    if not camera:
        raise _Refused("camera name required")
    with _LOCK:
        payload = _load_locked()
        found = False
        for a in payload["camera_assignments"]:
            if a.get("camera") == camera:
                a["zone_id"] = zone_id
                a["label"]   = label
                found = True
                break
        if not found:
            payload["camera_assignments"].append({
                "camera": camera, "zone_id": zone_id, "label": label,
            })
        _save_locked(payload)
        return {"camera": camera, "zone_id": zone_id, "label": label}
