"""
WhatsApp message templates the Live Agent can fire mid-call.

Each template has an English and an Arabic body. The agent picks the
language based on the call's language detection (Arabic by default,
English when the caller switches). Bodies are {var}-interpolated
against the agent's call context — the agent passes whatever fields
it has (name, file_number, appointment_id, …) and missing fields are
replaced with an empty string rather than crashing.

Storage:
    data/demos/restaurant/whatsapp_templates.json — author-edited content,
    travels through git (the .gitignore carve-out matches persona.txt
    and kb.txt's logic).

Five templates ship by default; the operator can edit any of them on
Call Center → WhatsApp Templates.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.whatsapp_templates")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_TEMPLATES_PATH = _DATA_DIR / "whatsapp_templates.json"

# ----------------------------------------------------------------------
# Defaults — exact wording the operator can override via the SPA page.
# Variable conventions:
#   {patient_name}      English name (the agent's transliteration)
#   {patient_name_ar}   Arabic name
#   {file_number}       e.g. A123456
#   {appointment_id}    e.g. APT-001
#   {appointment_date}  YYYY-MM-DD (the tool returns this; templates
#                       display verbatim — formatting in a future iteration)
#   {appointment_time}  HH:MM (24-hour; same caveat)
#   {previous_date}     reschedule: the old date
#   {previous_time}     reschedule: the old time
#   {clinic_name}       English clinic name
#   {clinic_name_ar}    Arabic clinic name
#   {clinic_location}   English address
#   {clinic_location_ar} Arabic address
#   {maps_link}         Google Maps URL — operator pastes once
#   {clinic_phone}      Reception number
# ----------------------------------------------------------------------

DEFAULT_TEMPLATES: dict[str, dict] = {
    "clinic_location": {
        "name":          "Send Location of Clinic",
        "description":   "Share the clinic's address and a Google Maps link.",
        "variables":     ["patient_name", "patient_name_ar",
                          "clinic_name", "clinic_name_ar",
                          "clinic_location", "clinic_location_ar",
                          "maps_link"],
        "en":
            "Hello {patient_name},\n\n"
            "Here is the location of {clinic_name}:\n"
            "{clinic_location}\n\n"
            "📍 Google Maps: {maps_link}\n\n"
            "If you need anything else, just reply to this message.\n"
            "— Primewave Mate Clinics",
        "ar":
            "السلام عليكم {patient_name_ar},\n\n"
            "موقع {clinic_name_ar}:\n"
            "{clinic_location_ar}\n\n"
            "📍 خرائط قوقل: {maps_link}\n\n"
            "في حال احتجت شيء إضافي، الرد على هذه الرسالة كافٍ.\n"
            "— عيادات برايم ميت",
    },
    "file_creation": {
        "name":          "Send File Creation",
        "description":   "Confirm a new patient file was created and share the file number.",
        "variables":     ["patient_name", "patient_name_ar",
                          "file_number", "clinic_name", "clinic_name_ar"],
        "en":
            "Welcome to {clinic_name}, {patient_name}!\n\n"
            "Your patient file has been created.\n\n"
            "📁 File number: {file_number}\n\n"
            "Please keep this number — you'll need it for future "
            "appointments. We look forward to serving you.\n\n"
            "— Primewave Mate Clinics",
        "ar":
            "أهلاً وسهلاً بك في {clinic_name_ar}، {patient_name_ar}!\n\n"
            "تم إنشاء ملفك الطبي.\n\n"
            "📁 رقم الملف: {file_number}\n\n"
            "يرجى الاحتفاظ بهذا الرقم — ستحتاجه لمواعيدك القادمة. "
            "نتشرف بخدمتك.\n\n"
            "— عيادات برايم ميت",
    },
    "appointment_creation": {
        "name":          "Send Appointment Creation",
        "description":   "Confirm a newly-booked appointment with date, time, and location.",
        "variables":     ["patient_name", "patient_name_ar",
                          "appointment_id", "appointment_date", "appointment_time",
                          "clinic_name", "clinic_name_ar",
                          "clinic_location", "clinic_location_ar"],
        "en":
            "Hello {patient_name},\n\n"
            "Your appointment has been confirmed ✅\n\n"
            "🏥 {clinic_name}\n"
            "📅 {appointment_date}\n"
            "🕐 {appointment_time}\n"
            "📍 {clinic_location}\n"
            "🔢 Appointment ID: {appointment_id}\n\n"
            "Please arrive 10 minutes early.\n"
            "— Primewave Mate Clinics",
        "ar":
            "السلام عليكم {patient_name_ar},\n\n"
            "تم تأكيد موعدك ✅\n\n"
            "🏥 {clinic_name_ar}\n"
            "📅 {appointment_date}\n"
            "🕐 {appointment_time}\n"
            "📍 {clinic_location_ar}\n"
            "🔢 رقم الحجز: {appointment_id}\n\n"
            "يرجى الحضور قبل ١٠ دقائق من الموعد.\n"
            "— عيادات برايم ميت",
    },
    "appointment_reschedule": {
        "name":          "Send Appointment Reschedule",
        "description":   "Confirm a moved appointment with the old → new slot.",
        "variables":     ["patient_name", "patient_name_ar",
                          "appointment_id",
                          "previous_date", "previous_time",
                          "appointment_date", "appointment_time",
                          "clinic_name", "clinic_name_ar",
                          "clinic_location", "clinic_location_ar"],
        "en":
            "Hello {patient_name},\n\n"
            "Your appointment has been rescheduled ✅\n\n"
            "🔢 Appointment ID: {appointment_id}\n\n"
            "Previous: {previous_date} at {previous_time}\n"
            "➡ New: {appointment_date} at {appointment_time}\n\n"
            "🏥 {clinic_name}\n"
            "📍 {clinic_location}\n\n"
            "— Primewave Mate Clinics",
        "ar":
            "السلام عليكم {patient_name_ar},\n\n"
            "تم تغيير موعدك ✅\n\n"
            "🔢 رقم الحجز: {appointment_id}\n\n"
            "السابق: {previous_date} الساعة {previous_time}\n"
            "➡ الجديد: {appointment_date} الساعة {appointment_time}\n\n"
            "🏥 {clinic_name_ar}\n"
            "📍 {clinic_location_ar}\n\n"
            "— عيادات برايم ميت",
    },
    "appointment_cancellation": {
        "name":          "Send Appointment Cancellation",
        "description":   "Confirm a cancellation and invite the caller to rebook.",
        "variables":     ["patient_name", "patient_name_ar",
                          "appointment_id",
                          "appointment_date", "appointment_time",
                          "clinic_name", "clinic_name_ar"],
        "en":
            "Hello {patient_name},\n\n"
            "Your appointment has been cancelled.\n\n"
            "🔢 Appointment ID: {appointment_id}\n"
            "🏥 {clinic_name}\n"
            "📅 Was: {appointment_date} at {appointment_time}\n\n"
            "If you'd like to rebook, just reply to this message or "
            "call us back.\n"
            "— Primewave Mate Clinics",
        "ar":
            "السلام عليكم {patient_name_ar},\n\n"
            "تم إلغاء موعدك.\n\n"
            "🔢 رقم الحجز: {appointment_id}\n"
            "🏥 {clinic_name_ar}\n"
            "📅 كان: {appointment_date} الساعة {appointment_time}\n\n"
            "في حال أردت إعادة الحجز، الرد على هذه الرسالة كافٍ، أو "
            "اتصل بنا.\n"
            "— عيادات برايم ميت",
    },
}


# Ordered list — drives the order they appear in the UI and the order
# the agent sees them documented in the persona.
TEMPLATE_ORDER: list[str] = [
    "clinic_location",
    "file_creation",
    "appointment_creation",
    "appointment_reschedule",
    "appointment_cancellation",
]


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def _empty_overrides() -> dict[str, dict]:
    return {}


def load_overrides() -> dict[str, dict]:
    """Return the on-disk customisations only (not merged with defaults).
    The SPA renders the merged view; the JSON file stays minimal so it
    only carries operator-edited content."""
    if not _TEMPLATES_PATH.exists():
        return _empty_overrides()
    try:
        data = json.loads(_TEMPLATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("whatsapp_templates.json corrupt — using defaults")
        return _empty_overrides()
    if not isinstance(data, dict):
        return _empty_overrides()
    return data


def save_overrides(payload: dict[str, dict]) -> dict[str, dict]:
    """Persist operator edits. We accept a partial dict
    {template_id: {en?, ar?}} and merge over what's already on disk.
    Unknown template ids are dropped."""
    current = load_overrides()
    for tid, patch in (payload or {}).items():
        if tid not in DEFAULT_TEMPLATES:
            continue
        if not isinstance(patch, dict):
            continue
        existing = dict(current.get(tid, {}))
        for k in ("en", "ar"):
            if k in patch and isinstance(patch[k], str):
                existing[k] = patch[k]
        if existing:
            current[tid] = existing
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _TEMPLATES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(_TEMPLATES_PATH)
    return current


def resolved_templates() -> dict[str, dict]:
    """Return DEFAULT_TEMPLATES with on-disk overrides applied. Used by
    both the GET endpoint (so the SPA renders the live view) and the
    send tool (so the agent fires the operator's wording)."""
    overrides = load_overrides()
    out: dict[str, dict] = {}
    for tid in TEMPLATE_ORDER:
        base = dict(DEFAULT_TEMPLATES[tid])
        ovr  = overrides.get(tid) or {}
        if isinstance(ovr.get("en"), str): base["en"] = ovr["en"]
        if isinstance(ovr.get("ar"), str): base["ar"] = ovr["ar"]
        out[tid] = base
    return out


# ----------------------------------------------------------------------
# Variable interpolation
# ----------------------------------------------------------------------

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def interpolate(body: str, variables: dict) -> str:
    """Replace {var} with variables[var]. Missing keys become "". Never
    raises — we'd rather send a slightly-incomplete message than crash
    the agent's tool call mid-conversation."""
    if not body:
        return ""
    def repl(m):
        return str(variables.get(m.group(1), "")).strip()
    return _VAR_RE.sub(repl, body)


def render(template_id: str, language: str, variables: Optional[dict] = None) -> dict:
    """Resolve a template by id + language, interpolate variables, and
    return a dict the agent tool can hand straight to wasender.send_text.

    Returns:
      {ok: True, text: "...", template_id: "...", language: "en"|"ar"}
      {ok: False, error: "..."}
    """
    if template_id not in DEFAULT_TEMPLATES:
        return {"ok": False, "error": f"Unknown template_id: {template_id}"}
    lang = (language or "ar").strip().lower()
    if lang not in ("en", "ar"):
        lang = "ar"
    body = resolved_templates()[template_id].get(lang, "")
    if not body.strip():
        return {"ok": False, "error": f"Template {template_id}/{lang} is empty"}
    text = interpolate(body, variables or {})
    return {
        "ok":          True,
        "text":        text,
        "template_id": template_id,
        "language":    lang,
    }
