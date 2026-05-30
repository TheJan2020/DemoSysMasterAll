"""
Clinic main settings — operator-editable name / location / contact info
for the WHOLE clinic group (not per-individual-clinic; that's snapshot's
clinics list). Used as the canonical "about us" data the agent and bot
can quote, and as the source-of-truth values for WhatsApp templates
that reference {clinic_name}, {clinic_location}, etc.

Storage:
    data/demos/clinic/main_settings.json

The file IS tracked by git — same carve-out as persona.txt + kb.txt +
whatsapp_templates.json. It's content the operator writes on the
Settings page, not a secret or per-machine state, so we want both
machines to see the same edits after a pull.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("demo_clinic.main_settings")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "clinic"
_SETTINGS_PATH = _DATA_DIR / "main_settings.json"

# All keys are stored as plain strings so the SPA can edit them in one
# pass. Empty string = unset; we never store nulls.
DEFAULT_MAIN_SETTINGS: dict[str, str] = {
    "clinic_name_en":     "Primewave Mate Clinics",
    "clinic_name_ar":     "عيادات برايم ميت",
    "clinic_location_en": "",
    "clinic_location_ar": "",
    "clinic_phone":       "",
    "clinic_whatsapp":    "",
    "clinic_email":       "",
    "clinic_website":     "",
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
