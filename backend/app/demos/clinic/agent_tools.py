"""
Clinic Live Agent — function-call tools + data snapshot.

The clinic data layer lives in the Clinic SPA (localStorage) — the
SPA's Dashboard pushes a snapshot to `POST /api/demo/clinic/data/snapshot`
on mount + after every mutation, and the backend writes it to
`data/demos/clinic/snapshot.json`. Tool calls then read/mutate that
snapshot file directly.

Tools exposed to Gemini Live:
  - lookup_patient_by_phone(phone)
  - lookup_patient_by_id_number(id_number)
  - lookup_patient_by_file_number(file_number)
  - list_free_slots(date, clinic_id?)
  - create_patient(name, name_ar?, phone, id_number, date_of_birth,
                   gender, city?)
  - create_appointment(patient_id, clinic_id, date, time,
                       duration_min?, reason?)
  - end_call(reason)

When a tool MUTATES (create_*), it also broadcasts a `tool_mutation`
event on the service's pub/sub bus so the Dashboard (and the SPA's
localStorage) can mirror the change immediately.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from google.genai import types

logger = logging.getLogger("clinic_agent_tools")

# Snapshot location — matches live_agent.py's _DATA_DIR (parents[4]
# from this nested file: clinic→demos→app→backend→<project-root>).
_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "clinic"
_SNAPSHOT_PATH = _DATA_DIR / "snapshot.json"

# Booking buffer — don't offer today-slots within this many minutes of now.
BOOKING_BUFFER_MIN = 15
SLOT_MIN = 30  # matches the SPA's SLOT_MINUTES


# ============================================================================
# Snapshot read/write
# ============================================================================

def _empty_snapshot() -> dict:
    return {
        "patients":      [],
        "appointments":  [],
        "clinics":       [],
        "providers":     [],
        "slot_overrides": [],
        "updated_at":    None,
    }


def load_snapshot() -> dict:
    if not _SNAPSHOT_PATH.exists():
        return _empty_snapshot()
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("snapshot.json corrupt")
        return _empty_snapshot()


def save_snapshot(snap: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap["updated_at"] = datetime.utcnow().isoformat() + "Z"
    tmp = _SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SNAPSHOT_PATH)


# ============================================================================
# Helpers
# ============================================================================

_DIGITS = re.compile(r"\D+")

def _normalize_phone(s: str) -> str:
    """Strip everything but digits. '+966 50 234 5678' → '966502345678'."""
    return _DIGITS.sub("", s or "")


def _format_saudi_mobile(s: str) -> str:
    """Canonicalise Saudi mobile input to E.164 '+9665XXXXXXXX'.

    Saudi mobiles are 9-digit national numbers that start with 5
    (e.g. 501234567). Callers say them in lots of shapes:

      "0501234567"            → +966501234567   (national trunk prefix)
      "501234567"             → +966501234567   (bare 9-digit)
      "966501234567"          → +966501234567   (country code, no plus)
      "+966 50 123 4567"      → +966501234567   (already E.164, formatted)
      "00966501234567"        → +966501234567   (international dial)

    Returns an empty string on anything that isn't a recognisable
    Saudi mobile (wrong length, wrong leading digit, missing 5-prefix
    after stripping). Empty is the safe sentinel — the caller record
    gets created without a phone instead of with a bogus one.
    """
    d = _DIGITS.sub("", s or "")
    if not d:
        return ""
    # Drop international access prefix '00' if present.
    if d.startswith("00"):
        d = d[2:]
    # Strip country code if leading.
    if d.startswith("966"):
        d = d[3:]
    # Strip national trunk '0' if leading (only meaningful for a 10-digit
    # national-format input like 0501234567).
    if len(d) == 10 and d.startswith("0"):
        d = d[1:]
    # What remains must be the 9-digit national number starting with 5.
    if len(d) != 9 or not d.startswith("5"):
        return ""
    return f"+966{d}"


def _phones_equal(a: str, b: str) -> bool:
    """Match phones loosely — last 9 digits is enough to handle ±country code."""
    na, nb = _normalize_phone(a), _normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb: return True
    # Saudi country code 966 plus 9-digit national → match on last 9
    if len(na) >= 9 and len(nb) >= 9 and na[-9:] == nb[-9:]:
        return True
    return False


# Matches the SPA's nextId() widths: PAT = 4 digits, everything else = 3.
def _next_id(existing: list[dict], prefix: str) -> str:
    max_n = 0
    pat = re.compile(rf"^{prefix}-(\d+)$")
    for x in existing:
        m = pat.match(str(x.get("id") or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    width = 4 if prefix == "PAT" else 3
    return f"{prefix}-{str(max_n + 1).zfill(width)}"


def _next_file_number(patients: list[dict]) -> str:
    """File # is letter A/B/C + 6 digits, first digit 1-9."""
    used = {p.get("file_number") for p in patients}
    letters = ["A", "B", "C"]
    li = (len(used) // 800_000) % 3
    for _ in range(10000):
        # deterministic-ish: bump a counter, wrap letters
        n = (len(used) % 800_000) + 1
        rest = str(n).zfill(5)
        candidate = f"{letters[li]}{1 + (n % 9)}{rest}"
        if candidate not in used:
            return candidate
        li = (li + 1) % 3
        used.add(candidate)  # force a different pick next loop
    # fallback
    return f"A100000"


def _weekday_index(date_str: str) -> int:
    y, m, d = (int(x) for x in date_str.split("-"))
    # Match the SPA's weekdayOf: 0 = Sunday … 6 = Saturday
    return datetime(y, m - 1 + 1, d).weekday() if False else _sunday_indexed(y, m, d)


def _sunday_indexed(y: int, m: int, d: int) -> int:
    """Python's Monday=0; the SPA's weekdayOf is Sunday=0. Convert."""
    py = datetime(y, m, d).weekday()  # Mon=0..Sun=6
    return (py + 1) % 7  # → Sun=0..Sat=6


def _default_working_hours_for_weekday(idx: int) -> dict:
    """Fallback when a clinic has no `working_hours` in the snapshot — same
    Saudi-clinic-week default the SPA seeds with."""
    common = {"open": True,  "open_time": "09:00", "close_time": "17:00",
              "break_enabled": True, "break_start": "13:00", "break_end": "14:00"}
    if idx == 5:  # Friday
        return {"open": False, "open_time": "09:00", "close_time": "17:00",
                "break_enabled": False, "break_start": "13:00", "break_end": "14:00"}
    if idx == 6:  # Saturday
        return {"open": True,  "open_time": "09:00", "close_time": "13:00",
                "break_enabled": False, "break_start": "12:00", "break_end": "13:00"}
    return common


def _time_to_min(t: str) -> int:
    h, m = (int(x) for x in t.split(":"))
    return h * 60 + m


def _min_to_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _slots_for_day(hours: dict) -> list[str]:
    if not hours.get("open"):
        return []
    start = _time_to_min(hours.get("open_time", "09:00"))
    end   = _time_to_min(hours.get("close_time", "17:00"))
    out = []
    for m in range(start, end - SLOT_MIN + 1, SLOT_MIN):
        out.append(_min_to_time(m))
    return out


def _is_break(slot: str, hours: dict) -> bool:
    if not hours.get("break_enabled"):
        return False
    s = _time_to_min(slot)
    return _time_to_min(hours["break_start"]) <= s < _time_to_min(hours["break_end"])


def _booked_slots(appointments: list[dict], date: str, clinic_id: str) -> set[str]:
    out: set[str] = set()
    for a in appointments:
        if a.get("status") in ("cancelled", "no_show"):
            continue
        if a.get("department_id") != clinic_id:
            continue
        sched = a.get("scheduled_at", "")
        if sched[:10] != date:
            continue
        start_min = _time_to_min(sched[11:16])
        duration = int(a.get("duration_min") or SLOT_MIN)
        end_min = start_min + duration
        first = (start_min // SLOT_MIN) * SLOT_MIN
        for m in range(first, end_min, SLOT_MIN):
            out.add(_min_to_time(m))
    return out


def _blocked_slots(overrides: list[dict], date: str, clinic_id: str) -> set[str]:
    out: set[str] = set()
    for o in overrides:
        if o.get("department_id") == clinic_id and o.get("date") == date:
            for s in o.get("blocked_slots", []) or []:
                out.add(s)
    return out


# ============================================================================
# Tool declarations for Gemini Live
# ============================================================================

def build_tools() -> list[types.Tool]:
    """Single Tool with every function declaration we expose."""
    decls: list[types.FunctionDeclaration] = [
        types.FunctionDeclaration(
            name="lookup_patient_by_phone",
            description="Look up a patient by mobile phone number. Returns "
                        "patient details if a unique match is found, otherwise null. "
                        "Phone matching ignores formatting and accepts Saudi numbers "
                        "with or without country code (+966 / 966 / 0).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"phone": types.Schema(type=types.Type.STRING)},
                required=["phone"],
            ),
        ),
        types.FunctionDeclaration(
            name="lookup_patient_by_id_number",
            description="Look up a patient by national / Iqama ID number (10 digits, "
                        "starts with 1 for Saudi nationals or 2 for residents).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"id_number": types.Schema(type=types.Type.STRING)},
                required=["id_number"],
            ),
        ),
        types.FunctionDeclaration(
            name="lookup_patient_by_file_number",
            description="Look up a patient by clinic file number (format: letter "
                        "A/B/C followed by 6 digits, e.g. 'A123456').",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"file_number": types.Schema(type=types.Type.STRING)},
                required=["file_number"],
            ),
        ),
        types.FunctionDeclaration(
            name="list_clinics",
            description="List the clinics (departments) that actually exist in "
                        "this center. Returns id, name, specialty, and location "
                        "for each. ALWAYS call this before claiming a clinic or "
                        "specialty exists — do NOT guess names from the caller's "
                        "phrasing.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "specialty": types.Schema(
                        type=types.Type.STRING,
                        description="Optional fuzzy match (case-insensitive substring) "
                                    "on the English specialty, e.g. 'card' for cardiology.",
                    ),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="list_providers",
            description="List the people (doctors, nurses, techs) who actually "
                        "work at this center. Returns id, name, role, and "
                        "specialty for each. ALWAYS call this before naming a "
                        "doctor — do NOT invent names.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "specialty": types.Schema(
                        type=types.Type.STRING,
                        description="Optional fuzzy match on specialty.",
                    ),
                    "clinic_id": types.Schema(
                        type=types.Type.STRING,
                        description="Optional clinic id to restrict to that clinic's "
                                    "head + matching-specialty staff.",
                    ),
                    "role": types.Schema(
                        type=types.Type.STRING,
                        description="Optional role filter: 'doctor', 'nurse', "
                                    "'tech', or 'admin'.",
                    ),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="list_free_slots",
            description="List available appointment slot start times for one "
                        "clinic on one date. Already filters past times and slots "
                        "within 15 minutes of the current time. Returns a list of "
                        "'HH:MM' strings.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "date":       types.Schema(type=types.Type.STRING,
                                               description="YYYY-MM-DD"),
                    "clinic_id":  types.Schema(type=types.Type.STRING,
                                               description="Department ID like DEP-001. "
                                                           "If omitted, all clinics are returned."),
                },
                required=["date"],
            ),
        ),
        types.FunctionDeclaration(
            name="create_patient",
            description="Create a new patient file. Returns the assigned "
                        "patient_id and file_number. Only 'name' is strictly "
                        "required — pass whatever else the caller gave you "
                        "(phone, id_number, date_of_birth, gender, city). "
                        "Empty / omitted fields are accepted and reception "
                        "fills them in on arrival. You MUST still ASK the "
                        "caller for phone and national/Iqama ID — the "
                        "tool's permissiveness is so the create still "
                        "succeeds when the caller declines to share, NOT a "
                        "licence to skip the questions.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name":          types.Schema(type=types.Type.STRING,
                                                  description="Caller's full name "
                                                              "in English (Latin "
                                                              "letters). You "
                                                              "transliterate it "
                                                              "yourself — do NOT "
                                                              "ask the caller for "
                                                              "an English spelling."),
                    "name_ar":       types.Schema(type=types.Type.STRING,
                                                  description="Full name in Arabic."),
                    "phone":         types.Schema(type=types.Type.STRING,
                                                  description="Saudi mobile in any "
                                                              "shape — '0501234567', "
                                                              "'501234567', "
                                                              "'+966 50 123 4567'. "
                                                              "The tool canonicalises "
                                                              "to '+9665XXXXXXXX' and "
                                                              "stores '' if it can't "
                                                              "(wrong length, wrong "
                                                              "leading digit)."),
                    "id_number":     types.Schema(type=types.Type.STRING),
                    "date_of_birth": types.Schema(type=types.Type.STRING,
                                                  description="YYYY-MM-DD"),
                    "gender":        types.Schema(type=types.Type.STRING,
                                                  description="'male' or 'female'"),
                    "city":          types.Schema(type=types.Type.STRING),
                    "city_ar":       types.Schema(type=types.Type.STRING),
                },
                required=["name"],
            ),
        ),
        types.FunctionDeclaration(
            name="create_appointment",
            description="Book an appointment. Use AFTER the caller confirms a slot. "
                        "Returns the appointment_id and the confirmed date/time.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "patient_id":   types.Schema(type=types.Type.STRING),
                    "clinic_id":    types.Schema(type=types.Type.STRING),
                    "date":         types.Schema(type=types.Type.STRING,
                                                 description="YYYY-MM-DD"),
                    "time":         types.Schema(type=types.Type.STRING,
                                                 description="HH:MM"),
                    "duration_min": types.Schema(type=types.Type.INTEGER),
                    "notes":        types.Schema(type=types.Type.STRING),
                },
                required=["patient_id", "clinic_id", "date", "time"],
            ),
        ),
        types.FunctionDeclaration(
            name="list_patient_appointments",
            description="List a patient's appointments (defaults to upcoming "
                        "only). Use this when the caller wants to change, "
                        "cancel, or reschedule a booking — you need the "
                        "appointment_id from here before you can call "
                        "cancel_appointment or reschedule_appointment.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "patient_id":     types.Schema(type=types.Type.STRING),
                    "include_past":   types.Schema(type=types.Type.BOOLEAN,
                                                   description="Defaults to false — "
                                                               "only upcoming appointments."),
                },
                required=["patient_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="cancel_appointment",
            description="Cancel an existing appointment (sets status to "
                        "'cancelled'). Use AFTER the caller has confirmed "
                        "they want to cancel this specific appointment. "
                        "Look up the appointment_id with "
                        "list_patient_appointments first.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "appointment_id": types.Schema(type=types.Type.STRING),
                    "reason":         types.Schema(type=types.Type.STRING,
                                                   description="Optional short reason "
                                                               "for the cancellation."),
                },
                required=["appointment_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="reschedule_appointment",
            description="Move an existing appointment to a new date+time. "
                        "Same patient, same clinic — only date+time change. "
                        "Re-verifies the new slot is free, within hours, "
                        "and outside the 15-min booking buffer. Returns the "
                        "new appointment record (same id, updated time).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "appointment_id": types.Schema(type=types.Type.STRING),
                    "new_date":       types.Schema(type=types.Type.STRING,
                                                   description="YYYY-MM-DD"),
                    "new_time":       types.Schema(type=types.Type.STRING,
                                                   description="HH:MM (24-hour)"),
                },
                required=["appointment_id", "new_date", "new_time"],
            ),
        ),
        types.FunctionDeclaration(
            name="flag_for_supervisor",
            description=(
                "Raise a red flag for a human supervisor to take over this "
                "call. The Dashboard surfaces the flagged row in red with "
                "the reason you provide. Use this when the caller is angry, "
                "frustrated, repeatedly misunderstood, threatens to escalate, "
                "asks for a manager / human, or whenever continuing alone "
                "would only make things worse. KEEP TALKING to the caller "
                "after calling this tool — do NOT mention 'I'm flagging this' "
                "out loud. A supervisor will join silently or take over."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "reason":   types.Schema(
                        type=types.Type.STRING,
                        description="One short sentence the supervisor sees on the "
                                    "Dashboard, e.g. 'Caller has asked for a manager twice' "
                                    "or 'Repeated tool failure on appointment lookup'.",
                    ),
                    "severity": types.Schema(
                        type=types.Type.STRING,
                        description="'high' = drop everything and join now (angry, "
                                    "complaint, emergency); 'normal' = supervisor should "
                                    "check in when free. Defaults to 'normal'.",
                    ),
                },
                required=["reason"],
            ),
        ),
        types.FunctionDeclaration(
            name="send_whatsapp_template",
            description=(
                "Fire one of the pre-configured WhatsApp templates to the "
                "caller (or to any phone you pass explicitly). Pick "
                "template_id from this exact list:\n"
                "  - clinic_location           (share the clinic's address)\n"
                "  - file_creation             (confirm a brand-new patient file)\n"
                "  - appointment_creation      (confirm a freshly-booked slot)\n"
                "  - appointment_reschedule    (confirm a moved slot)\n"
                "  - appointment_cancellation  (confirm a cancellation)\n"
                "language is 'en' or 'ar' — pick what the caller has been "
                "speaking. to_phone is OPTIONAL — leave it unset to use "
                "the caller's own WhatsApp number from the session. Any "
                "other named arg is interpolated into the template body "
                "as a {variable}. Run this AFTER the matching tool that "
                "produced the data (e.g. send appointment_creation only "
                "AFTER create_appointment returned its appointment_id), "
                "and only ONCE per event."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "template_id": types.Schema(type=types.Type.STRING),
                    "language":    types.Schema(type=types.Type.STRING,
                                                description="'en' or 'ar'"),
                    "to_phone":    types.Schema(type=types.Type.STRING,
                                                description="Optional — defaults to caller phone."),
                    "patient_name":      types.Schema(type=types.Type.STRING),
                    "patient_name_ar":   types.Schema(type=types.Type.STRING),
                    "file_number":       types.Schema(type=types.Type.STRING),
                    "appointment_id":    types.Schema(type=types.Type.STRING),
                    "appointment_date":  types.Schema(type=types.Type.STRING),
                    "appointment_time":  types.Schema(type=types.Type.STRING),
                    "previous_date":     types.Schema(type=types.Type.STRING),
                    "previous_time":     types.Schema(type=types.Type.STRING),
                    "clinic_name":       types.Schema(type=types.Type.STRING),
                    "clinic_name_ar":    types.Schema(type=types.Type.STRING),
                    "clinic_location":   types.Schema(type=types.Type.STRING),
                    "clinic_location_ar": types.Schema(type=types.Type.STRING),
                    "maps_link":         types.Schema(type=types.Type.STRING),
                },
                required=["template_id", "language"],
            ),
        ),
        types.FunctionDeclaration(
            name="end_call",
            description="Hang up the call. Use after the caller says goodbye and "
                        "you've spoken your closing line.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reason": types.Schema(type=types.Type.STRING)},
                required=["reason"],
            ),
        ),
    ]
    return [types.Tool(function_declarations=decls)]


# ============================================================================
# Tool implementations — invoked from CallSession.receive() on tool_call
# ============================================================================

def execute_tool(name: str, args: dict, ctx: dict) -> dict:
    """Dispatch a Gemini function call. `ctx` is the call's context dict;
    we use it to set `end_requested = True` for end_call, and to expose
    a `broadcast(event)` callable for tool_mutation events.
    """
    # Compact dispatch log — appears in the Debug page so you can see
    # at a glance which tools the agent invoked, with which args. We
    # strip very long values (like full patient lists) so the line
    # stays readable.
    try:
        compact_args = {
            k: (v if not isinstance(v, str) or len(v) < 80 else f"{v[:77]}…")
            for k, v in args.items()
        }
        logger.info("dispatch %s args=%s call_id=%s ids=%s",
                    name, compact_args, ctx.get("call_id"),
                    sorted(ctx.get("identified_patient_ids") or []))
    except Exception:
        pass
    result = _execute_tool_inner(name, args, ctx)
    # Result log — without this, errors like
    # "create_appointment: slot falls inside the clinic's break window"
    # are invisible on the Debug page even though the dispatch is
    # logged. Same compact-summary treatment as the dispatch line.
    try:
        if isinstance(result, dict):
            if result.get("error"):
                logger.warning("result %s ERROR: %s", name, result.get("error"))
            else:
                # Summarise success — pull only fields useful to a human
                # debugger. Skip 'text', 'turns', etc that would blow up
                # the line.
                summary = {
                    k: result[k] for k in
                    ("ok", "patient_id", "file_number",
                     "appointment_id", "date", "time", "previous",
                     "count", "is_today", "now", "buffer_minutes",
                     "to", "message_id", "language", "template_id",
                     "status", "found", "whatsapp_sent")
                    if k in result
                }
                # Add list-shape hints without dumping the full list.
                for k in ("clinics", "providers", "appointments"):
                    if k in result and isinstance(result[k], list):
                        summary[f"{k}_count"] = len(result[k])
                logger.info("result %s ok summary=%s", name, summary)
        else:
            logger.info("result %s (non-dict): %r", name, result)
    except Exception:
        pass
    return result


def _execute_tool_inner(name: str, args: dict, ctx: dict) -> dict:
    """The original dispatch logic — wrapped by execute_tool so we get
    consistent before/after logging without duplicating every return."""
    try:
        if name == "lookup_patient_by_phone":
            return _t_lookup_phone(args.get("phone") or "")
        if name == "lookup_patient_by_id_number":
            return _t_lookup_id(args.get("id_number") or "")
        if name == "lookup_patient_by_file_number":
            return _t_lookup_file(args.get("file_number") or "")
        if name == "list_clinics":
            return _t_list_clinics(args.get("specialty"))
        if name == "list_providers":
            return _t_list_providers(args.get("specialty"),
                                     args.get("clinic_id"),
                                     args.get("role"))
        if name == "list_free_slots":
            return _t_list_free_slots(args.get("date") or "", args.get("clinic_id"))
        if name == "create_patient":
            return _t_create_patient(args, ctx)
        if name == "create_appointment":
            return _t_create_appointment(args, ctx)
        if name == "list_patient_appointments":
            return _t_list_patient_appointments(args.get("patient_id") or "",
                                                bool(args.get("include_past")),
                                                ctx)
        if name == "cancel_appointment":
            return _t_cancel_appointment(args.get("appointment_id") or "",
                                         args.get("reason") or "", ctx)
        if name == "reschedule_appointment":
            return _t_reschedule_appointment(args.get("appointment_id") or "",
                                             args.get("new_date") or "",
                                             args.get("new_time") or "", ctx)
        if name == "flag_for_supervisor":
            return _t_flag_for_supervisor(args, ctx)
        if name == "send_whatsapp_template":
            return _t_send_whatsapp_template(args, ctx)
        if name == "end_call":
            return _t_end_call(args.get("reason") or "", ctx)
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


def _patient_for_agent(p: dict) -> dict:
    """Trim the patient record to fields the agent should actually use —
    keeps responses compact and avoids leaking internals."""
    return {
        "patient_id":     p.get("id"),
        "file_number":    p.get("file_number"),
        "name":           p.get("name"),
        "name_ar":        p.get("name_ar"),
        "id_number":      p.get("id_number"),
        "phone":          p.get("phone"),
        "date_of_birth":  p.get("date_of_birth"),
        "gender":         p.get("gender"),
        "city":           p.get("city"),
    }


def _t_lookup_phone(phone: str) -> dict:
    snap = load_snapshot()
    for p in snap.get("patients", []):
        if _phones_equal(p.get("phone", ""), phone):
            return {"found": True, "patient": _patient_for_agent(p)}
    return {"found": False, "patient": None}


def _t_lookup_id(id_number: str) -> dict:
    target = _DIGITS.sub("", id_number or "")
    snap = load_snapshot()
    for p in snap.get("patients", []):
        if _DIGITS.sub("", str(p.get("id_number") or "")) == target:
            return {"found": True, "patient": _patient_for_agent(p)}
    return {"found": False, "patient": None}


def _t_lookup_file(file_number: str) -> dict:
    target = (file_number or "").strip().upper()
    snap = load_snapshot()
    for p in snap.get("patients", []):
        if (p.get("file_number") or "").upper() == target:
            return {"found": True, "patient": _patient_for_agent(p)}
    return {"found": False, "patient": None}


def _clinic_for_agent(c: dict) -> dict:
    return {
        "clinic_id":    c.get("id"),
        "name":         c.get("name"),
        "name_ar":      c.get("name_ar"),
        "specialty":    c.get("specialty"),
        "specialty_ar": c.get("specialty_ar"),
        "location":     c.get("location"),
        "location_ar":  c.get("location_ar"),
        "head_provider_id": c.get("head_id"),
    }


def _provider_for_agent(p: dict) -> dict:
    return {
        "provider_id": p.get("id"),
        "name":        p.get("name"),
        "name_ar":     p.get("name_ar"),
        "role":        p.get("role"),
        "specialty":   p.get("specialty"),
        "specialty_ar": p.get("specialty_ar"),
    }


def _t_list_clinics(specialty: Optional[str]) -> dict:
    snap = load_snapshot()
    rows = [c for c in snap.get("clinics", []) if c.get("active", True)]
    if specialty:
        needle = specialty.strip().lower()
        rows = [
            c for c in rows
            if needle in (c.get("specialty") or "").lower()
            or needle in (c.get("specialty_ar") or "").lower()
            or needle in (c.get("name") or "").lower()
            or needle in (c.get("name_ar") or "").lower()
        ]
    return {
        "count":   len(rows),
        "clinics": [_clinic_for_agent(c) for c in rows],
        "note":    "These are the ONLY clinics that exist. Do not invent others.",
    }


def _t_list_providers(specialty: Optional[str],
                      clinic_id: Optional[str],
                      role: Optional[str]) -> dict:
    snap = load_snapshot()
    rows = [p for p in snap.get("providers", []) if p.get("active", True)]
    if role:
        rows = [p for p in rows if (p.get("role") or "").lower() == role.strip().lower()]
    if specialty:
        needle = specialty.strip().lower()
        rows = [
            p for p in rows
            if needle in (p.get("specialty") or "").lower()
            or needle in (p.get("specialty_ar") or "").lower()
            or needle in (p.get("name") or "").lower()
            or needle in (p.get("name_ar") or "").lower()
        ]
    if clinic_id:
        clinic = next((c for c in snap.get("clinics", [])
                       if c.get("id") == clinic_id), None)
        if clinic:
            clinic_spec = (clinic.get("specialty") or "").lower()
            head = clinic.get("head_id")
            rows = [
                p for p in rows
                if p.get("id") == head
                or (p.get("specialty") or "").lower() == clinic_spec
            ]
        else:
            rows = []
    return {
        "count":     len(rows),
        "providers": [_provider_for_agent(p) for p in rows],
        "note":      "These are the ONLY people on staff. Do not invent doctors.",
    }


def _t_list_free_slots(date: str, clinic_id: Optional[str]) -> dict:
    if not date or len(date) != 10:
        return {"error": "date must be YYYY-MM-DD"}
    snap = load_snapshot()
    clinics = snap.get("clinics", [])
    appts = snap.get("appointments", [])
    overrides = snap.get("slot_overrides", [])

    now = datetime.now()
    today_ymd = now.strftime("%Y-%m-%d")
    is_today = (date == today_ymd)
    earliest = now + timedelta(minutes=BOOKING_BUFFER_MIN)

    # Past date → no slots, ever.
    if date < today_ymd:
        return {"date": date, "clinics": [], "note": "Date is in the past."}

    wd = _sunday_indexed(*(int(x) for x in date.split("-")))
    target_clinics = [c for c in clinics
                      if (clinic_id is None or c.get("id") == clinic_id)]

    out_clinics: list[dict] = []
    for c in target_clinics:
        hours = (c.get("working_hours") or
                 [None]*7)
        if isinstance(hours, list) and len(hours) == 7:
            day = hours[wd] or _default_working_hours_for_weekday(wd)
        else:
            day = _default_working_hours_for_weekday(wd)
        all_slots = _slots_for_day(day)
        if not all_slots:
            out_clinics.append({
                "clinic_id":    c.get("id"),
                "clinic_name":  c.get("name"),
                "free_slots":   [],
                "note":         "Clinic closed on this day.",
            })
            continue
        booked = _booked_slots(appts, date, c.get("id"))
        blocked = _blocked_slots(overrides, date, c.get("id"))
        free: list[str] = []
        for s in all_slots:
            if _is_break(s, day): continue
            if s in booked or s in blocked: continue
            if is_today:
                slot_dt = datetime(*(int(x) for x in date.split("-")),
                                   _time_to_min(s) // 60, _time_to_min(s) % 60)
                if slot_dt < earliest:
                    continue
            free.append(s)
        out_clinics.append({
            "clinic_id":    c.get("id"),
            "clinic_name":  c.get("name"),
            "specialty":    c.get("specialty"),
            "free_slots":   free,
        })
    return {
        "date":   date,
        "is_today": is_today,
        "now":    now.strftime("%H:%M"),
        "buffer_minutes": BOOKING_BUFFER_MIN if is_today else 0,
        "clinics": out_clinics,
    }


_FILE_PATTERN = re.compile(r"^[A-C][1-9]\d{5}$")
_ID_PATTERN   = re.compile(r"^[12]\d{9}$")


def _t_create_patient(args: dict, ctx: dict) -> dict:
    snap = load_snapshot()
    patients = snap.get("patients", [])

    name = (args.get("name") or "").strip()
    # Whatever shape the caller said the phone in — "0501234567", "+966
    # 50 123 4567", bare "501234567" — normalize to the canonical Saudi
    # E.164 "+9665XXXXXXXX". Returns "" on an unrecognisable number so
    # we don't pollute the record with garbage; reception fixes at the
    # desk. The agent is told (in GUARDRAILS) to *ask* for the mobile,
    # but this tool is the final authority on its format.
    phone = _format_saudi_mobile(args.get("phone") or "")
    id_number = re.sub(r"\D", "", args.get("id_number") or "")
    dob = (args.get("date_of_birth") or "").strip()
    gender = (args.get("gender") or "").strip().lower()

    # ONLY `name` is strictly required. Everything else (phone, ID, DOB,
    # gender, city) is accepted when present and stored empty otherwise.
    # Rationale: the agent has been observed inventing a fake
    # file_number rather than admit "I couldn't complete the record",
    # which is the worse failure mode. Letting the create succeed with
    # just a name means there's at least a real, agent-issued
    # identifier to show the caller, and reception fills in the rest
    # on arrival.
    if not name:
        return {"error": "Missing required field — at minimum 'name' is required."}
    if id_number and not _ID_PATTERN.match(id_number):
        # Caller provided something but it's not in the Saudi format —
        # drop it rather than error; reception can fix at the desk.
        id_number = ""
    if gender not in ("male", "female"):
        gender = ""

    new_id = _next_id(patients, "PAT")
    file_number = _next_file_number(patients)
    today = datetime.now().strftime("%Y-%m-%d")
    record = {
        "id":             new_id,
        "file_number":    file_number,
        "id_number":      id_number,
        "name":           name,
        "name_ar":        (args.get("name_ar") or "").strip(),
        "gender":         gender,
        "date_of_birth":  dob,
        "phone":          phone,
        "email":          "",
        "city":           (args.get("city") or "").strip(),
        "city_ar":        (args.get("city_ar") or "").strip(),
        "registration_date":   today,
        "registration_source": "live_agent",
        "notes":          "Created during live agent call.",
    }
    patients.append(record)
    snap["patients"] = patients
    save_snapshot(snap)
    logger.info("create_patient SAVED id=%s file=%s name=%r phone=%r",
                new_id, file_number, name, phone)

    # Mirror into the identified-set on this call so subsequent
    # cancel/reschedule tools recognise the caller as the owner.
    ids = ctx.get("identified_patient_ids")
    if isinstance(ids, set):
        ids.add(new_id)
    # And keep ctx['caller_phone'] in sync so the auto-send below
    # (and any later send_whatsapp_template call) finds the number.
    if phone and not ctx.get("caller_phone"):
        ctx["caller_phone"] = phone

    broadcast = ctx.get("broadcast")
    if callable(broadcast):
        broadcast({
            "type":    "tool_mutation",
            "call_id": ctx.get("call_id"),
            "kind":    "patient_created",
            "patient": record,
        })

    # Auto-fire the file_creation WhatsApp template — used to be the
    # agent's job (persona step 8) but it forgot too often. Now we
    # send from the runtime on every successful create_patient.
    # Pick the first active clinic as a generic display value for
    # {clinic_name}; the operator can edit the template to remove
    # the clinic line if they prefer a fully generic body.
    first_clinic = next(
        (c for c in (snap.get("clinics") or [])
         if c.get("active", True)),
        None,
    )
    _auto_send_template("file_creation", ctx, variables={
        "to_phone":        phone,
        "patient_name":    name,
        "patient_name_ar": (args.get("name_ar") or "").strip(),
        "file_number":     file_number,
        "clinic_name":     (first_clinic or {}).get("name") or "Primewave Mate Clinics",
        "clinic_name_ar":  (first_clinic or {}).get("name_ar") or "عيادات برايم ميت",
    })

    return {
        "ok":           True,
        "patient_id":   new_id,
        "file_number":  file_number,
        "name":         name,
        "whatsapp_sent": True,  # Hint to the agent: don't re-send.
    }


def _t_create_appointment(args: dict, ctx: dict) -> dict:
    snap = load_snapshot()
    patients = snap.get("patients", [])
    clinics = snap.get("clinics", [])
    appts = snap.get("appointments", [])
    overrides = snap.get("slot_overrides", [])

    patient_id = args.get("patient_id")
    clinic_id = args.get("clinic_id")
    date = args.get("date")
    time_str = args.get("time")
    duration = int(args.get("duration_min") or SLOT_MIN)
    notes = (args.get("notes") or "").strip()

    if not (patient_id and clinic_id and date and time_str):
        return {"error": "patient_id, clinic_id, date, time are required"}

    patient = next((p for p in patients if p.get("id") == patient_id), None)
    if patient is None:
        return {"error": f"patient {patient_id} not found"}
    clinic = next((c for c in clinics if c.get("id") == clinic_id), None)
    if clinic is None:
        return {"error": f"clinic {clinic_id} not found"}

    # Re-verify the slot is actually free + future + outside the buffer.
    now = datetime.now()
    today_ymd = now.strftime("%Y-%m-%d")
    if date < today_ymd:
        return {"error": "cannot book an appointment in the past"}
    slot_dt = datetime(*(int(x) for x in date.split("-")),
                       _time_to_min(time_str) // 60, _time_to_min(time_str) % 60)
    if date == today_ymd and slot_dt < now + timedelta(minutes=BOOKING_BUFFER_MIN):
        return {"error": f"slot is within the {BOOKING_BUFFER_MIN}-minute booking buffer"}

    wd = _sunday_indexed(*(int(x) for x in date.split("-")))
    hours_list = clinic.get("working_hours")
    if isinstance(hours_list, list) and len(hours_list) == 7:
        day = hours_list[wd] or _default_working_hours_for_weekday(wd)
    else:
        day = _default_working_hours_for_weekday(wd)
    if not day.get("open"):
        return {"error": "clinic is closed on this day"}
    if _is_break(time_str, day):
        return {"error": "slot falls inside the clinic's break window"}

    # Hard slot-grid check: the requested time MUST be one of the slots
    # that list_free_slots would have returned for this clinic on this
    # day (30-minute grid, within open/close window). Catches off-grid
    # times like "09:17" or "16:45" at the tool layer so the agent
    # can't book them even if its prompt discipline slips. The list
    # below comes from the exact same generator list_free_slots uses,
    # so any discrepancy is structural, not heuristic.
    valid_slots = _slots_for_day(day)
    if time_str not in valid_slots:
        return {
            "error": (
                f"slot {time_str} is not on this clinic's "
                f"{SLOT_MIN}-minute grid for {date} "
                f"({day.get('open_time')}-{day.get('close_time')}). "
                f"Call list_free_slots(date={date!r}, clinic_id={clinic_id!r}) "
                f"and pick from what it returns."
            ),
        }

    booked = _booked_slots(appts, date, clinic_id)
    blocked = _blocked_slots(overrides, date, clinic_id)
    if time_str in booked: return {"error": "slot is already booked"}
    if time_str in blocked: return {"error": "slot is blocked by the clinic"}

    new_id = _next_id(appts, "APT")
    # ISO datetime stored as LOCAL wall-clock without a Z suffix — JS
    # new Date(s) parses this as local time, matching how the SPA's
    # Appointments / Calendar pages read it. (Adding Z would shift the
    # displayed time by the TZ offset.) Includes seconds so .slice(11,16)
    # always lands exactly on HH:MM.
    scheduled = f"{date}T{time_str}:00"
    record = {
        "id":              new_id,
        "patient_id":      patient.get("id"),
        "patient_name":    patient.get("name"),
        "patient_name_ar": patient.get("name_ar"),
        "patient_phone":   patient.get("phone"),
        "department_id":   clinic_id,
        "provider_id":     None,
        "scheduled_at":    scheduled,
        "duration_min":    duration,
        "status":          "scheduled",
        "notes":           notes or "Created by Live Agent",
    }
    appts.append(record)
    snap["appointments"] = appts
    save_snapshot(snap)

    broadcast = ctx.get("broadcast")
    if callable(broadcast):
        broadcast({
            "type":        "tool_mutation",
            "call_id":     ctx.get("call_id"),
            "kind":        "appointment_created",
            "appointment": record,
        })

    # Auto-fire appointment_creation WhatsApp template — runtime
    # guarantees the caller gets a written confirmation even when
    # the agent forgets to call send_whatsapp_template.
    _auto_send_template("appointment_creation", ctx, variables={
        "to_phone":           patient.get("phone") or "",
        "patient_name":       patient.get("name") or "",
        "patient_name_ar":    patient.get("name_ar") or "",
        "appointment_id":     new_id,
        "appointment_date":   date,
        "appointment_time":   time_str,
        "clinic_name":        clinic.get("name") or "",
        "clinic_name_ar":     clinic.get("name_ar") or "",
        "clinic_location":    clinic.get("location") or "",
        "clinic_location_ar": clinic.get("location_ar") or "",
    })

    return {
        "ok":             True,
        "appointment_id": new_id,
        "date":           date,
        "time":           time_str,
        "duration_min":   duration,
        "clinic_name":    clinic.get("name"),
        "patient_name":   patient.get("name"),
        "whatsapp_sent":  True,
    }


def _appt_for_agent(a: dict, clinics_by_id: Optional[dict] = None) -> dict:
    """Trim an appointment record for the agent — keeps the response
    compact and matches the field names the persona expects.

    Pass `clinics_by_id` so the agent gets the clinic's NAME inline,
    not just `clinic_id`. Without the name, the agent has to
    cross-reference the system instruction's roster block to figure
    out what "DEP-003" actually is, and that's where it occasionally
    picks the wrong clinic. When name is right next to the time in
    the same response, drift is much less likely.
    """
    clinic = (clinics_by_id or {}).get(a.get("department_id")) or {}
    return {
        "appointment_id":   a.get("id"),
        "patient_id":       a.get("patient_id"),
        "patient_name":     a.get("patient_name"),
        "patient_name_ar":  a.get("patient_name_ar"),
        "clinic_id":        a.get("department_id"),
        "clinic_name":      clinic.get("name"),
        "clinic_name_ar":   clinic.get("name_ar"),
        "clinic_location":  clinic.get("location"),
        "clinic_location_ar": clinic.get("location_ar"),
        "scheduled_at":     a.get("scheduled_at"),
        "date":             (a.get("scheduled_at") or "")[:10],
        "time":             (a.get("scheduled_at") or "")[11:16],
        "duration_min":     a.get("duration_min"),
        "status":           a.get("status"),
        "notes":            a.get("notes"),
    }


def _auto_send_template(template_id: str, ctx: dict, variables: dict,
                         language: Optional[str] = None) -> None:
    """Fire a WhatsApp template AS A SIDE-EFFECT of a successful mutation
    tool — the agent doesn't have to remember to call
    send_whatsapp_template separately. Errors are logged but never
    raised, so a WhatsApp failure can't roll back the underlying
    record write. Variables come from the tool's own result, plus we
    inject a sensible language default ('ar', matching the persona's
    Arabic-first stance) when the agent hasn't told us otherwise.
    """
    # WhatsApp Bot mode: the bot is ALREADY replying over WhatsApp with
    # its own message. Firing the template too would land the user with
    # two messages per mutation (template + bot reply). The bot is free
    # to call send_whatsapp_template itself if a templated copy is
    # actually desired.
    if ctx.get("skip_auto_whatsapp"):
        logger.info("auto-send WhatsApp template=%s SKIPPED (bot mode)", template_id)
        return
    args = {
        "template_id": template_id,
        "language":    (language or ctx.get("call_language") or "ar"),
    }
    args.update({k: v for k, v in variables.items() if v not in (None, "")})
    logger.info("auto-send WhatsApp template=%s to=%s lang=%s",
                template_id, args.get("to_phone"), args.get("language"))
    try:
        result = _t_send_whatsapp_template(args, ctx)
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "auto-send WhatsApp %s FAILED: %s",
                template_id, result.get("error"),
            )
        else:
            logger.info("auto-send WhatsApp %s OK (message_id=%s)",
                        template_id,
                        (result or {}).get("message_id") if isinstance(result, dict) else None)
    except Exception:
        logger.exception("auto-send WhatsApp %s crashed (non-fatal)",
                          template_id)


def _owned_by_caller(appointment: dict, ctx: dict) -> bool:
    """Return True if this appointment's patient_id is in the set of
    patient_ids the agent has verified on this call (via successful
    lookup_patient_* or create_patient). When the call is fresh and
    nobody has been identified yet, the set is empty — we DENY in that
    case rather than allow, so an unidentified caller can't reach into
    another patient's record by guessing IDs."""
    ids = ctx.get("identified_patient_ids") or set()
    if not ids:
        return False
    return appointment.get("patient_id") in ids


def _t_list_patient_appointments(patient_id: str, include_past: bool,
                                  ctx: Optional[dict] = None) -> dict:
    if not patient_id:
        return {"error": "patient_id is required"}
    # Authorisation: only the identified caller's appointments are
    # returnable. Without this, the agent could pass an arbitrary
    # patient_id and see another caller's bookings.
    if ctx is not None:
        ids = ctx.get("identified_patient_ids") or set()
        if not ids:
            return {"error": "Caller is not yet identified on this call. "
                             "Run lookup_patient_by_phone / _by_id_number / "
                             "_by_file_number first, or create_patient, "
                             "BEFORE listing appointments."}
        if patient_id not in ids:
            return {"error": f"patient_id {patient_id} is not the "
                             f"identified caller on this call. Refusing for "
                             f"privacy. Only the caller's own patient_id "
                             f"may be queried."}
    snap = load_snapshot()
    appts = snap.get("appointments", [])
    # Lookup table by id so each appointment row can carry its
    # clinic's name inline (otherwise the agent gets "clinic_id:
    # DEP-003" and has to cross-reference, occasionally wrong).
    clinics_by_id = {c.get("id"): c for c in (snap.get("clinics") or [])}
    today_ymd = datetime.now().strftime("%Y-%m-%d")
    rows = [a for a in appts if a.get("patient_id") == patient_id]
    if not include_past:
        rows = [
            a for a in rows
            if (a.get("scheduled_at") or "")[:10] >= today_ymd
            and a.get("status") not in ("cancelled", "no_show", "completed")
        ]
    rows.sort(key=lambda a: a.get("scheduled_at") or "")
    return {
        "count":        len(rows),
        "appointments": [_appt_for_agent(a, clinics_by_id) for a in rows],
    }


def _t_cancel_appointment(appointment_id: str, reason: str, ctx: dict) -> dict:
    if not appointment_id:
        return {"error": "appointment_id is required"}
    snap = load_snapshot()
    appts = snap.get("appointments", [])
    found = next((a for a in appts if a.get("id") == appointment_id), None)
    if found is None:
        return {"error": f"appointment {appointment_id} not found"}
    if not _owned_by_caller(found, ctx):
        return {"error": f"appointment {appointment_id} does NOT belong to "
                         f"the identified caller (patient_id "
                         f"{found.get('patient_id')!r}). REFUSED for privacy. "
                         f"Always call list_patient_appointments(patient_id) "
                         f"with the IDENTIFIED caller's patient_id first, and "
                         f"only cancel an appointment_id that came back from "
                         f"that list."}
    if found.get("status") == "cancelled":
        return {"error": f"appointment {appointment_id} is already cancelled"}

    found["status"] = "cancelled"
    found["notes"] = (
        (found.get("notes") or "") + f" | Cancelled by Live Agent: {reason or '(no reason)'}"
    ).strip(" |")
    save_snapshot(snap)

    broadcast = ctx.get("broadcast")
    if callable(broadcast):
        broadcast({
            "type":        "tool_mutation",
            "call_id":     ctx.get("call_id"),
            "kind":        "appointment_cancelled",
            "appointment": found,
        })

    # Auto-fire appointment_cancellation WhatsApp template.
    clinic_id = found.get("department_id")
    clinic = next((c for c in snap.get("clinics", [])
                   if c.get("id") == clinic_id), {}) or {}
    sched = found.get("scheduled_at") or ""
    _auto_send_template("appointment_cancellation", ctx, variables={
        "to_phone":        found.get("patient_phone") or "",
        "patient_name":    found.get("patient_name") or "",
        "patient_name_ar": found.get("patient_name_ar") or "",
        "appointment_id":  appointment_id,
        "appointment_date": sched[:10],
        "appointment_time": sched[11:16],
        "clinic_name":     clinic.get("name") or "",
        "clinic_name_ar":  clinic.get("name_ar") or "",
    })

    return {
        "ok":             True,
        "appointment_id": appointment_id,
        "status":         "cancelled",
        "reason":         reason or None,
        "whatsapp_sent":  True,
    }


def _t_reschedule_appointment(appointment_id: str, new_date: str,
                              new_time: str, ctx: dict) -> dict:
    if not (appointment_id and new_date and new_time):
        return {"error": "appointment_id, new_date, new_time are all required"}
    snap = load_snapshot()
    appts = snap.get("appointments", [])
    clinics = snap.get("clinics", [])
    overrides = snap.get("slot_overrides", [])

    target = next((a for a in appts if a.get("id") == appointment_id), None)
    if target is None:
        return {"error": f"appointment {appointment_id} not found"}
    if not _owned_by_caller(target, ctx):
        return {"error": f"appointment {appointment_id} does NOT belong to "
                         f"the identified caller (patient_id "
                         f"{target.get('patient_id')!r}). REFUSED for privacy. "
                         f"Always call list_patient_appointments(patient_id) "
                         f"with the IDENTIFIED caller's patient_id first, and "
                         f"only reschedule an appointment_id that came back "
                         f"from that list."}
    if target.get("status") in ("cancelled", "no_show", "completed"):
        return {"error": f"appointment {appointment_id} is {target.get('status')} — "
                         "cannot be rescheduled. Cancel and create a new one."}

    clinic_id = target.get("department_id")
    clinic = next((c for c in clinics if c.get("id") == clinic_id), None)
    if clinic is None:
        return {"error": f"clinic {clinic_id} no longer exists"}

    # Re-verify the new slot against working hours, break, buffer,
    # bookings + overrides — same gauntlet as create_appointment.
    now = datetime.now()
    today_ymd = now.strftime("%Y-%m-%d")
    if new_date < today_ymd:
        return {"error": "cannot reschedule to a past date"}
    try:
        slot_dt = datetime(*(int(x) for x in new_date.split("-")),
                           _time_to_min(new_time) // 60,
                           _time_to_min(new_time) % 60)
    except Exception:
        return {"error": f"invalid new_date / new_time ({new_date} {new_time})"}
    if new_date == today_ymd and slot_dt < now + timedelta(minutes=BOOKING_BUFFER_MIN):
        return {"error": f"new slot is within the {BOOKING_BUFFER_MIN}-minute booking buffer"}

    wd = _sunday_indexed(*(int(x) for x in new_date.split("-")))
    hours_list = clinic.get("working_hours")
    if isinstance(hours_list, list) and len(hours_list) == 7:
        day = hours_list[wd] or _default_working_hours_for_weekday(wd)
    else:
        day = _default_working_hours_for_weekday(wd)
    if not day.get("open"):
        return {"error": "clinic is closed on the requested day"}
    if _is_break(new_time, day):
        return {"error": "requested slot falls inside the clinic's break window"}
    # Hard slot-grid check — must be one of list_free_slots's outputs
    # for this clinic on this day (30-minute grid, within open window).
    # Stricter than the old open-window check: also rejects off-grid
    # times like "16:45" that fell inside the window but never showed
    # up in list_free_slots's result.
    valid_slots = _slots_for_day(day)
    if new_time not in valid_slots:
        return {
            "error": (
                f"requested time {new_time} is not on this clinic's "
                f"{SLOT_MIN}-minute grid for {new_date} "
                f"({day.get('open_time')}-{day.get('close_time')}). "
                f"Call list_free_slots(date={new_date!r}, clinic_id={clinic_id!r}) "
                f"and pick from what it returns."
            ),
        }

    booked = _booked_slots(
        [a for a in appts if a.get("id") != appointment_id], new_date, clinic_id,
    )
    blocked = _blocked_slots(overrides, new_date, clinic_id)
    if new_time in booked:  return {"error": "new slot is already booked"}
    if new_time in blocked: return {"error": "new slot is blocked by the clinic"}

    old_scheduled = target.get("scheduled_at")
    target["scheduled_at"] = f"{new_date}T{new_time}:00"
    target["status"] = "scheduled"
    target["notes"] = (
        (target.get("notes") or "") + f" | Rescheduled by Live Agent (was {old_scheduled})"
    ).strip(" |")
    save_snapshot(snap)

    broadcast = ctx.get("broadcast")
    if callable(broadcast):
        broadcast({
            "type":        "tool_mutation",
            "call_id":     ctx.get("call_id"),
            "kind":        "appointment_rescheduled",
            "appointment": target,
            "previous_scheduled_at": old_scheduled,
        })

    # Auto-fire appointment_reschedule WhatsApp template.
    _auto_send_template("appointment_reschedule", ctx, variables={
        "to_phone":           target.get("patient_phone") or "",
        "patient_name":       target.get("patient_name") or "",
        "patient_name_ar":    target.get("patient_name_ar") or "",
        "appointment_id":     appointment_id,
        "previous_date":      (old_scheduled or "")[:10],
        "previous_time":      (old_scheduled or "")[11:16],
        "appointment_date":   new_date,
        "appointment_time":   new_time,
        "clinic_name":        clinic.get("name") or "",
        "clinic_name_ar":     clinic.get("name_ar") or "",
        "clinic_location":    clinic.get("location") or "",
        "clinic_location_ar": clinic.get("location_ar") or "",
    })

    return {
        "ok":             True,
        "appointment_id": appointment_id,
        "date":           new_date,
        "time":           new_time,
        "previous":       old_scheduled,
        "clinic_name":    clinic.get("name"),
        "patient_name":   target.get("patient_name"),
        "whatsapp_sent":  True,
    }


def _t_end_call(reason: str, ctx: dict) -> dict:
    ctx["end_requested"] = True
    logger.info("clinic call %s: end_call requested — reason: %s",
                ctx.get("call_id"), reason or "(none)")
    return {"ok": True, "reason": reason}


def _t_flag_for_supervisor(args: dict, ctx: dict) -> dict:
    """Tell the CallSession to raise a supervisor flag. The session
    stores it on itself (so snapshot replay shows it to dashboards that
    connect later) and broadcasts a `supervisor_flag` WS event."""
    reason = (args.get("reason") or "").strip() or "(no reason given)"
    severity = (args.get("severity") or "").strip().lower()
    if severity not in ("low", "normal", "high"):
        severity = "normal"
    set_flag = ctx.get("set_flag")
    if not callable(set_flag):
        # Backward-safety: shouldn't happen in production, but log loudly
        # so a regression is obvious instead of silently swallowed.
        logger.warning("clinic call %s: flag_for_supervisor invoked but ctx[set_flag] missing",
                       ctx.get("call_id"))
        return {"error": "flag handler not wired"}
    set_flag({"reason": reason, "severity": severity, "source": "agent"})
    logger.info("clinic call %s: supervisor flag raised (%s) — %s",
                ctx.get("call_id"), severity, reason)
    return {"ok": True, "reason": reason, "severity": severity}


def _t_send_whatsapp_template(args: dict, ctx: dict) -> dict:
    """Fire a pre-configured WhatsApp template via WasenderApi.

    Pulls the template body from data/demos/clinic/whatsapp_templates.json
    (operator-editable on the SPA's WhatsApp Templates page) in the
    requested language, interpolates the named arg dict as {variables},
    sends the rendered text via the Wasender per-session API key, and
    broadcasts a `whatsapp_template_sent` mutation event so the
    Dashboard's activity feed shows what fired.
    """
    import httpx as _httpx_sync
    from .whatsapp_templates import render
    from .live_agent import load_escalation_config
    from .wasender import normalize_phone as _wa_norm, WASENDER_BASE_URL

    template_id = (args.get("template_id") or "").strip()
    language    = (args.get("language") or "ar").strip().lower()
    if not template_id:
        return {"error": "template_id is required"}

    # Variables — everything that isn't a control field goes through to
    # the interpolator.
    control = {"template_id", "language", "to_phone"}
    variables = {k: v for k, v in args.items() if k not in control}

    rendered = render(template_id, language, variables)
    if not rendered.get("ok"):
        return {"error": rendered.get("error") or "template render failed"}
    text = rendered["text"]

    # to_phone resolution order:
    #   1. explicit arg `to_phone`
    #   2. ctx['caller_phone'] (set by a successful lookup_patient_*)
    #   3. fallback: any identified patient's phone in the snapshot
    #      (covers the case where create_patient ran without a phone
    #      lookup beforehand, so caller_phone is still empty even
    #      though we have a patient_id).
    raw_phone = (args.get("to_phone") or "").strip() or (ctx.get("caller_phone") or "")
    if not raw_phone:
        ids = ctx.get("identified_patient_ids") or set()
        if ids:
            snap = load_snapshot()
            for p in snap.get("patients", []):
                if p.get("id") in ids and p.get("phone"):
                    raw_phone = str(p.get("phone")).strip()
                    break
    if not raw_phone:
        return {"error": "No phone to send to — caller_phone unknown on this "
                         "call and no identified patient has a phone on file. "
                         "Pass to_phone explicitly."}
    phone = _wa_norm(raw_phone)
    if not phone:
        return {"error": f"Phone {raw_phone!r} could not be parsed."}

    # Read the live API key (operator may have updated it between calls).
    cfg = load_escalation_config()
    api_key = str(cfg.get("wasender_api_key") or "").strip()
    if not api_key:
        return {"error": "WhatsApp API key not configured on Configuration page."}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    body = {"to": phone, "messageType": "text", "text": text}
    try:
        with _httpx_sync.Client(timeout=10.0) as client:
            r = client.post(f"{WASENDER_BASE_URL}/send-message",
                            headers=headers, json=body)
    except _httpx_sync.RequestError as e:
        return {"error": f"network: {e}"}

    try:
        payload = r.json()
    except Exception:
        payload = {}
    if not (200 <= r.status_code < 300):
        err = (
            (isinstance(payload, dict) and (
                payload.get("error") or payload.get("message") or payload.get("detail")
            ))
            or f"HTTP {r.status_code}"
        )
        return {"error": err}

    message_id = ""
    if isinstance(payload, dict):
        data = payload.get("data") or {}
        if isinstance(data, dict):
            message_id = str(data.get("message_id") or "")

    # Broadcast so the Dashboard activity feed shows the fire.
    broadcast = ctx.get("broadcast")
    if callable(broadcast):
        broadcast({
            "type":        "tool_mutation",
            "call_id":     ctx.get("call_id"),
            "kind":        "whatsapp_template_sent",
            "template_id": template_id,
            "language":    language,
            "to":          phone,
            "text":        text,
            "message_id":  message_id,
        })

    return {
        "ok":          True,
        "template_id": template_id,
        "language":    language,
        "to":          phone,
        "message_id":  message_id,
        "text":        text,
    }
