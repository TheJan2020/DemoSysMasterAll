"""
Restaurant main settings — operator-editable name / location / contact +
restaurant-specific profile (cuisine, hours, capacity, etc.) for the
restaurant brand. Used as the canonical "about us" data the agent and
bot can quote, and as the source-of-truth values for WhatsApp templates.

Storage:
    data/demos/restaurant/main_settings.json

The file IS tracked by git — same carve-out as persona.txt + kb.txt +
whatsapp_templates.json. It's content the operator writes on the
Settings page, not a secret or per-machine state, so we want both
machines to see the same edits after a pull.

Field naming note: the core identity fields are still keyed as
`clinic_*` because the WhatsApp templates and the Live Agent's
instructions reference those placeholders by name (e.g. {clinic_name},
{clinic_location}). Renaming them across the codebase isn't worth the
churn for a UI-only rebrand, so the SPA presents these as "Restaurant
name", "Restaurant address", etc. while the storage key stays stable.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("demo_restaurant.main_settings")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_SETTINGS_PATH = _DATA_DIR / "main_settings.json"

# All keys are stored as plain strings so the SPA can edit them in one
# pass. Empty string = unset; we never store nulls.
DEFAULT_MAIN_SETTINGS: dict[str, str] = {
    # Core identity — shared with WhatsApp templates ({clinic_name}, etc).
    "clinic_name_en":     "Prime Mate Restaurant",
    "clinic_name_ar":     "مطعم برايم ميت",
    "clinic_location_en": "Al-Olaya, Riyadh",
    "clinic_location_ar": "العليا، الرياض",
    "clinic_phone":       "+966 11 234 5678",
    "clinic_whatsapp":    "+966 50 123 4567",
    "clinic_email":       "hello@primematerestaurant.com",
    "clinic_website":     "https://primematerestaurant.com",
    # Restaurant-specific profile.
    "cuisine_type":       "Lebanese",
    "opening_hours":      "Sat–Thu 12:00–23:30 · Fri 13:30–23:30",
    "capacity_seats":     "120",
    "private_dining":     "Yes — 2 majlis rooms (12 + 20 guests)",
    "manager_name":       "",
    "manager_phone":      "",
    "delivery_radius_km": "15",
    "min_order_sar":      "75",
    "delivery_fee_sar":   "20",
    "halal_certified":    "yes",
    "vat_number":         "",
    "cr_number":          "",
    "founded_year":       "2018",
    # Socials.
    "instagram_handle":   "",
    "twitter_handle":     "",
    "tiktok_handle":      "",
    "google_maps_url":    "",
}

_LOCK = threading.Lock()


def load_main_settings() -> dict[str, str]:
    """Return the full settings dict. Missing keys are filled from
    DEFAULT_MAIN_SETTINGS so callers can always rely on every key being
    present. Never raises — falls back to defaults on parse error."""
    out = dict(DEFAULT_MAIN_SETTINGS)
    if _SETTINGS_PATH.exists():
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in out:
                    if k in data and isinstance(data[k], str):
                        out[k] = data[k]
        except Exception:
            logger.exception("main_settings.json corrupt — using defaults")
    return out


def save_main_settings(patch: dict) -> dict[str, str]:
    """Merge `patch` into the on-disk settings and return the full
    result. Unknown keys are ignored. Values are trimmed + length-capped
    (1024 chars) to keep the file diffable."""
    with _LOCK:
        current = load_main_settings()
        for k, v in (patch or {}).items():
            if k not in current:
                continue
            if not isinstance(v, str):
                v = "" if v is None else str(v)
            current[k] = v.strip()[:1024]
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(_SETTINGS_PATH)
        return current
