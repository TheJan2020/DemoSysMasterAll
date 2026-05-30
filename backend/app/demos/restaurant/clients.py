"""
Custom Meals — clients database.

A simple per-restaurant book of subscribers / clients who order custom
meal plans. Fields are kept lean so the operator can edit a row in one
dialog without paging through tabs.

Storage:
    data/demos/restaurant/clients.json

Tracked by git (operator-edited content); the `.tmp` from atomic
writes is gitignored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.clients")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_CLIENTS_PATH = _DATA_DIR / "clients.json"

_LOCK = threading.Lock()

# Diet tags the operator can pick from. Free-text additions are allowed
# (we don't enforce this list on save) but the SPA's quick-pick chips
# use these so the data stays roughly consistent.
#
# Note: "halal" is intentionally NOT in this list — every meal we serve
# is halal by default (Saudi context), so tagging individual clients as
# halal would be noise. If a non-halal exception ever arises, the
# operator can still add it as a custom tag.
DIET_TAGS = [
    "vegetarian", "vegan", "gluten-free", "dairy-free",
    "low-carb", "low-sodium", "high-protein", "diabetic", "keto",
]


# ----------------------------------------------------------------------
# Seed — 10 Lebanese / Saudi diners with realistic phones + diet mixes.
# ----------------------------------------------------------------------

_NOW = int(time.time())


def _days_ago(d: int) -> int:
    return _NOW - d * 86400


DEFAULT_CLIENTS: list[dict] = [
    {"id": "CLT-001", "name": "Khalid Al-Otaibi",     "name_ar": "خالد العتيبي",
     "phone": "+966 50 123 4567", "email": "khalid.otaibi@example.com",
     "city": "Riyadh",
     "diet_tags": ["high-protein"],
     "allergies": "",
     "notes": "Prefers grilled, avoids spicy.",
     "created_at": _days_ago(45), "active": True},
    {"id": "CLT-002", "name": "Aisha Al-Harbi",       "name_ar": "عائشة الحربي",
     "phone": "+966 55 987 6543", "email": "aisha.h@example.com",
     "city": "Riyadh",
     "diet_tags": ["gluten-free"],
     "allergies": "Wheat (celiac).",
     "notes": "Two kids — half portions on weekends.",
     "created_at": _days_ago(120), "active": True},
    {"id": "CLT-003", "name": "Mohammed Al-Faraj",    "name_ar": "محمد الفرج",
     "phone": "+966 53 222 1111", "email": "mfaraj@example.com",
     "city": "Riyadh",
     "diet_tags": ["low-sodium", "diabetic"],
     "allergies": "",
     "notes": "Cardiologist-recommended low-sodium plan.",
     "created_at": _days_ago(78), "active": True},
    {"id": "CLT-004", "name": "Layla Al-Qahtani",     "name_ar": "ليلى القحطاني",
     "phone": "+966 56 333 7788", "email": "layla.q@example.com",
     "city": "Riyadh",
     "diet_tags": ["vegetarian"],
     "allergies": "Peanuts.",
     "notes": "Eats fish but no meat.",
     "created_at": _days_ago(30), "active": True},
    {"id": "CLT-005", "name": "Omar Al-Bandar",       "name_ar": "عمر البندر",
     "phone": "+966 54 555 8899", "email": "obandar@example.com",
     "city": "Riyadh",
     "diet_tags": ["high-protein", "keto"],
     "allergies": "",
     "notes": "Trains 6 days/week — wants 200g protein/day.",
     "created_at": _days_ago(12), "active": True},
    {"id": "CLT-006", "name": "Noura Al-Saleh",       "name_ar": "نورة الصالح",
     "phone": "+966 55 234 5678", "email": "noura.s@example.com",
     "city": "Riyadh",
     "diet_tags": ["low-carb"],
     "allergies": "Shellfish.",
     "notes": "Working professional — likes ready-to-eat lunches.",
     "created_at": _days_ago(60), "active": True},
    {"id": "CLT-007", "name": "Salem Al-Rashid",      "name_ar": "سالم الرشيد",
     "phone": "+966 50 777 8899", "email": "salem.r@example.com",
     "city": "Riyadh",
     "diet_tags": [],
     "allergies": "",
     "notes": "Family plan for 5 — Friday lunch is the big meal.",
     "created_at": _days_ago(200), "active": True},
    {"id": "CLT-008", "name": "Hanan Al-Mutairi",     "name_ar": "حنان المطيري",
     "phone": "+966 53 888 9911", "email": "hanan.m@example.com",
     "city": "Riyadh",
     "diet_tags": ["vegan"],
     "allergies": "Nuts (cashew + walnut).",
     "notes": "Strict vegan — no honey.",
     "created_at": _days_ago(15), "active": True},
    {"id": "CLT-009", "name": "Faisal Al-Dosari",     "name_ar": "فيصل الدوسري",
     "phone": "+966 56 444 7700", "email": "faisal.d@example.com",
     "city": "Riyadh",
     "diet_tags": ["low-carb", "high-protein"],
     "allergies": "",
     "notes": "Dropped subscription twice — handle politely.",
     "created_at": _days_ago(180), "active": False},
    {"id": "CLT-010", "name": "Mariam Al-Subaie",     "name_ar": "مريم السبيعي",
     "phone": "+966 50 222 3344", "email": "mariam.s@example.com",
     "city": "Riyadh",
     "diet_tags": ["dairy-free"],
     "allergies": "Lactose intolerant.",
     "notes": "Two daughters in the plan — peanut-friendly.",
     "created_at": _days_ago(7), "active": True},
]


def _default_payload() -> dict:
    return {"clients": deepcopy(DEFAULT_CLIENTS)}


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_clients() -> list[dict]:
    """Return every client, sorted name-first."""
    with _LOCK:
        rows = _load_locked().get("clients") or []
    rows.sort(key=lambda r: (r.get("name") or r.get("name_ar") or "").lower())
    return rows


def _load_locked() -> dict:
    if not _CLIENTS_PATH.exists():
        return _default_payload()
    try:
        data = json.loads(_CLIENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("clients.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(data, dict):
        return _default_payload()
    rows = data.get("clients")
    if not isinstance(rows, list):
        return _default_payload()
    return {"clients": [r for r in rows if isinstance(r, dict)]}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CLIENTS_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(_CLIENTS_PATH)
    return payload


def reset_clients() -> list[dict]:
    """Restore the Lebanese-restaurant seed (10 clients). Overwrites
    whatever's currently on disk — every edit is lost."""
    with _LOCK:
        _save_locked(_default_payload())
    return load_clients()


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

def _next_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("CLT-"):
            try:
                n = max(n, int(rid[4:]))
            except Exception:
                pass
    return f"CLT-{n + 1:03d}"


_PHONE_RE = re.compile(r"[^\d+\s\-()]+")


def _coerce_client(c: dict, existing: list[dict]) -> dict:
    tags = c.get("diet_tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "id":          str(c.get("id") or _next_id(existing)).strip(),
        "name":        str(c.get("name") or "").strip()[:200],
        "name_ar":     str(c.get("name_ar") or "").strip()[:200],
        "phone":       _PHONE_RE.sub("", str(c.get("phone") or "")).strip()[:32],
        "email":       str(c.get("email") or "").strip()[:200],
        "city":        str(c.get("city") or "").strip()[:120],
        "diet_tags":   [str(t).strip().lower()[:32] for t in tags if str(t).strip()],
        "allergies":   str(c.get("allergies") or "").strip()[:500],
        "notes":       str(c.get("notes") or "").strip()[:1000],
        "created_at":  int(c.get("created_at") or time.time()),
        "active":      bool(c.get("active", True)),
    }


class _Refused(ValueError):
    """Validation error surfaced as HTTP 400 by the router."""


def _validate(patch: dict) -> None:
    name    = (patch.get("name") or "").strip()
    name_ar = (patch.get("name_ar") or "").strip()
    if not (name or name_ar):
        raise _Refused("Either English or Arabic name is required.")


def add_client(patch: dict) -> dict:
    _validate(patch)
    with _LOCK:
        payload = _load_locked()
        if "created_at" not in patch:
            patch["created_at"] = int(time.time())
        cli = _coerce_client(patch, payload["clients"])
        payload["clients"].append(cli)
        _save_locked(payload)
    return cli


def update_client(client_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, c in enumerate(payload["clients"]):
            if c.get("id") == client_id:
                merged = {**c, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = client_id
                # If name fields were both blanked out, refuse.
                if not (merged.get("name") or merged.get("name_ar") or "").strip():
                    raise _Refused("Either English or Arabic name is required.")
                payload["clients"][i] = _coerce_client(merged, payload["clients"])
                _save_locked(payload)
                return payload["clients"][i]
    return None


def delete_client(client_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["clients"])
        payload["clients"] = [c for c in payload["clients"] if c.get("id") != client_id]
        removed = len(payload["clients"]) < before
        if removed:
            _save_locked(payload)
        return removed
