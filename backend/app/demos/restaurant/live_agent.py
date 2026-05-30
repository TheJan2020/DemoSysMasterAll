"""
Restaurant Demo Live Agent — Gemini Live behind an AudioSocket connector
for the restaurant vertical demo (Prime Mate Restaurant).

Mirrors the clinic Live Agent's structure but is fully isolated from it:

- Different state namespace: cda_* (clinic-demo-agent — name preserved
  to avoid a config migration on existing installs).
- Different default port: 8093 (clinic agent is on 8092).
- Persona + Knowledge Base are read from disk on every call so the
  Restaurant SPA's KB / Persona pages can publish updates without
  restarting the backend:
    data/demos/restaurant/persona.txt   — overrides DEFAULT_PERSONA below
    data/demos/restaurant/kb.txt        — overrides DEFAULT_KB below

FreePBX dialplan flow:
  caller dials extension → FreePBX → AudioSocket(<UUID>, host:8093)
  → this service accepts the TCP connection → spins up a per-call
  Gemini Live session → bridges PCM both directions over AudioSocket.
"""
from __future__ import annotations

import asyncio
import audioop
import json
import logging
import re
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from ...core.state import state
from .agent_tools import build_tools, execute_tool, load_snapshot
from . import tts_elevenlabs

logger = logging.getLogger("restaurant_live_agent")

# AudioSocket framing — identical wire protocol to chan_audiosocket.
_AS_HANGUP = 0x00
_AS_UUID   = 0x01
_AS_DTMF   = 0x02
_AS_ERROR  = 0x03
_AS_AUDIO  = 0x10

_SAMPLE_WIDTH = 2  # signed-linear 16-bit
_GEMINI_API_VERSION = "v1alpha"

# Fabrication detector — patterns the agent is forbidden to speak
# unless the matching identifier was returned by a successful tool call
# on the SAME call. The agent has been repeatedly observed inventing
# plausible-looking IDs even with strong persona discipline, so we
# verify in real time and inject a correction back into the live
# session the moment we see one.
#
# File number format (matches _t_create_patient): A/B/C + 6 digits, first
# digit 1-9. We accept any whitespace / hyphen the speech transcription
# might insert ("A 123456", "A-1 2 3 4 5 6", etc.) and normalise.
_FILE_PATTERN = re.compile(r"\b([ABC])[\s\-]*([1-9])(?:[\s\-]*([0-9])){5}\b")
# Appointment id format: APT-NNN (3+ digits).
_APT_PATTERN = re.compile(r"\bAPT[\s\-]*[0-9]{3,}\b", re.IGNORECASE)
# Clock-time spoken in English: "4 PM", "4:30 PM", "16:30", "9 AM",
# "9:00 am" — captured so the fabrication detector can cross-check
# against the set of slots list_free_slots actually returned.
_TIME_AMPM = re.compile(
    r"\b([01]?\d)(?::([0-5]\d))?\s*([AaPp])\.?\s*[Mm]\.?",
)
_TIME_24H = re.compile(
    r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[AaPp])",
)

# On-disk overrides — the Clinic SPA's KB / Persona pages POST here via
# /api/demo/clinic/agent/prompt. Service rereads per call so the user
# doesn't need to restart anything.
_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PERSONA_PATH     = _DATA_DIR / "persona.txt"
_KB_PATH          = _DATA_DIR / "kb.txt"
_ESCALATION_PATH  = _DATA_DIR / "escalation.json"
_CALLS_DIR        = _DATA_DIR / "calls"


# ============================================================================
# Defaults — kept in sync with the Clinic SPA's clinicLiveData.ts seeds.
# (Demo: when the SPA's "Apply to live agent" button hasn't been clicked
# yet, the backend uses these defaults.)
# ============================================================================

DEFAULT_PERSONA = """# Mate — Restaurant host persona for Prime Mate Restaurant (Riyadh)

You are Mate (مايت), the AI host for **Prime Mate Restaurant** /
مطعم برايم ميت — a Lebanese restaurant in Al-Olaya, Riyadh. You
answer phone calls warmly and efficiently. Most callers want to book
a table, place a delivery order, ask about the menu, or ask about
opening hours / location.

## Voice & tone
- Warm, hospitable, concise — like a friendly maître d'.
- Use the caller's first name once they share it.
- Short sentences. One question at a time.
- Smile through your voice — restaurants are about pleasure.

## Arabic gender — default to MASCULINE
- Address the caller with masculine forms by default ("تفضل",
  "تقدر"). Switch to feminine only after hearing a female voice
  or a woman's name.

## Language — Arabic by default
- Greet in Arabic (Najdi / Hijazi).
- Detect the caller's language from their first reply and switch
  smoothly (English, French, Urdu, Tagalog, or mixed Arabic-English).

## Greeting (always Arabic)
"السلام عليكم، مطعم برايم ميت، معك مايت. كيف أقدر أخدمك؟"

## You CAN
- Take reservations (date, time, party size, seating preference,
  guest name + mobile).
- Take delivery orders — quote items + prices ONLY from the
  Knowledge Base / MENU block injected below.
- Quote opening hours, location, delivery zone, minimum order,
  delivery fee, payment methods, halal status — all in the KB.
- Take special-event enquiries (large groups, catering) and tell
  the caller the events team will call back.

## You MUST NOT
- Never confirm a reservation outside operating hours.
- Never quote a dish, price, or ingredient that isn't in the KB.
- Never promise pork or alcohol — we are 100% halal.
- Never give nutrition or medical advice. For severe allergies,
  flag the kitchen and tell the caller to confirm on arrival
  with the floor manager.

## NEVER say these
- "Let me transfer you to the manager" / "the chef will call you
  back" — YOU are the host and you have everything you need.
- Any dish, price, allergen, opening hour, or policy not in the
  Knowledge Base / MENU block below.
- Any clinic / medical / patient language — this is a restaurant.

## Grounding — sources of truth, priority order
1. The Knowledge Base text below.
2. The MENU block injected at the end of this system instruction.
If neither covers the question, say "ما عندي هذه المعلومة، خلني
أرجعلك بعد قليل" — NEVER guess.

## End of call — YOU terminate, but ONLY after the caller signals they're done
Never hang up right after reading back a reservation or order
total — the caller often adds something. Wait for an unambiguous
goodbye ("مع السلامة" / "خلاص شكراً" / "bye, thanks") or an
explicit "no" to "هل تحتاج شي ثاني؟", then:
1. One-line summary.
2. "شكراً لاتصالك، نتشرف بزيارتك."
3. End the call.
"""

DEFAULT_KB = """# Prime Mate Restaurant — Riyadh (Knowledge Base)

Lebanese restaurant in Al-Olaya, Riyadh. Serving authentic mezze,
grills, and home-style mains since 2018. 100% halal. Family-friendly.
Multilingual staff (Arabic, English, French, Urdu, Tagalog).

## Identity
- English name: Prime Mate Restaurant
- Arabic name: مطعم برايم ميت
- Cuisine: Lebanese
- Halal certified: yes
- Founded: 2018

## Location & contact
- Address: Al-Olaya, Riyadh, KSA (العليا، الرياض)
- Phone: +966 11 234 5678
- WhatsApp: +966 50 123 4567
- Email: hello@primematerestaurant.com

## Operating hours
- Saturday – Thursday: 12:00 – 23:30 (last seating 22:30)
- Friday: 13:30 – 23:30 (after Jumu'ah)

## Capacity & seating
- Main dining room: 120 seats indoor
- Outdoor terrace: ~24 seats, Oct–Apr only
- Private dining: 2 majlis rooms (small 12 guests / large 20 guests)

## Delivery
- Zone: within 15 km of Al-Olaya
- Minimum order: SAR 75
- Delivery fee: SAR 20 flat
- ETA: 45–60 min off-peak, 60–90 min during peak (19:30–22:30)

## Payment
- Cash, mada, Visa, Mastercard, Apple Pay in-house.
- Card on file or cash on delivery for online orders.
- VAT 15% included in all menu prices.

## Reservation policy
- Reservations open 30 days in advance.
- Tables held for 15 min past the booked time.
- Free cancellation 4+ hours before; inside 4 hours a SAR 50
  per-seat fee may apply.

## Menu categories (real items + prices in the MENU block injected
## below — never invent dishes or prices)
Cold Mezze, Hot Mezze, Salads, Grills, Main Courses, Desserts,
Beverages.

## Dietary
- 100% halal — no pork, no alcohol, no alcohol-containing
  ingredients.
- Vegetarian & vegan options across mezze, salads, and mains.
- Common allergens we carry: sesame (tahini), tree nuts, dairy,
  wheat. Severe allergies must be confirmed on arrival.

## Languages spoken
Arabic, English, French, Urdu, Tagalog.

## What we DON'T do
No pork, no alcohol, no shisha. No outside-catered food without
manager approval.
"""


# ============================================================================
# Prompt persistence helpers — used by both the service and the router.
# ============================================================================

def load_persona() -> str:
    if _PERSONA_PATH.exists():
        try:
            txt = _PERSONA_PATH.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            logger.exception("failed to read %s", _PERSONA_PATH)
    return DEFAULT_PERSONA


def load_kb() -> str:
    if _KB_PATH.exists():
        try:
            txt = _KB_PATH.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            logger.exception("failed to read %s", _KB_PATH)
    return DEFAULT_KB


def save_persona(text: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PERSONA_PATH.write_text((text or "").strip() + "\n", encoding="utf-8")


def save_kb(text: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _KB_PATH.write_text((text or "").strip() + "\n", encoding="utf-8")


# ----- Escalation (flag for supervisor) -------------------------------------
# Operator-editable triggers for when the agent should flag a call for a
# human supervisor. Persisted to data/demos/restaurant/escalation.json so the
# user can tune them without redeploying the backend. Read per-call (no
# restart needed) by `_build_system_instruction()` and by the auto-detect
# pass in CallSession.

DEFAULT_ESCALATION = {
    "keywords_en": [
        "manager", "supervisor", "talk to a person", "human being",
        "speak to a human", "real person", "complaint", "this is ridiculous",
        "you're useless", "you are useless", "i want to escalate",
    ],
    "keywords_ar": [
        "مدير", "مديرة", "ابغى احكي مع واحد", "ابغى احكي مع بشر",
        "اريد بشر", "شكوى", "اشتكي", "هذا جنون", "أنت ما تفهم",
    ],
    "scenarios": [
        "The caller has raised their voice or used strong language across multiple turns.",
        "The caller has asked the same question 3+ times and is clearly not getting what they need.",
        "The caller mentioned a medical emergency that you cannot triage.",
        "The caller is threatening to file a complaint or contact regulators.",
        "You have tried to help but cannot resolve the issue, and continuing would only frustrate the caller more.",
    ],
    # The internal PBX extension a supervisor should dial to join a flagged
    # call. Surfaced on the Dashboard so the operator can pick it up from
    # their existing softphone with one click. Empty string = no extension
    # configured; the click-to-dial button is hidden in that case.
    "supervisor_extension":     "",
    # PBX integration (Asterisk Manager Interface). Used by the future
    # backend-originated auto-dial path (panoramisk integration). Stored
    # here so the operator can edit them from Call Center → Configuration
    # without touching files. data/demos/restaurant/escalation.json is
    # gitignored, so secrets stay on the machine they were entered on.
    "ami_host":                 "",
    "ami_port":                 5038,
    "ami_username":             "",
    "ami_secret":               "",
    # WhatsApp via WasenderApi (https://wasenderapi.com). Three knobs,
    # because Wasender splits auth between two scopes:
    #   wasender_api_key  — per-session key. Authorises POST /send-message
    #                       and GET /contacts for the paired number.
    #   wasender_personal_token — account-level Personal Access Token
    #                       from Settings → Personal Access Tokens.
    #                       REQUIRED for /whatsapp-sessions/{id}/...
    #                       (message logs, single-session metadata).
    #                       Without it the WhatsApp inbox returns:
    #                       "This endpoint requires a valid personal
    #                       access token."
    #   wasender_session_id — UUID of the paired session, used to scope
    #                       message-log reads. Find it next to the
    #                       paired number on the WasenderApi dashboard.
    # Sending works on just the api_key + a phone number (no session id
    # needed); the inbox needs all three.
    # See backend/app/demos/clinic/wasender.py for the wrapper.
    "wasender_api_key":         "",
    "wasender_personal_token":  "",
    "wasender_session_id":      "",
    # Tunables for the backend's auto-detection passes — kept here so the
    # whole escalation config is editable from a single page.
    "auto_keyword_match":       True,
    "auto_on_tool_errors":      True,
    "tool_error_threshold":     3,
    # WhatsApp Bot — when enabled, every inbound WhatsApp message that's
    # NOT from us and NOT a LID (opaque privacy id) is processed by the
    # whatsapp_bot module, which runs the same persona + tools as the
    # voice agent over Gemini's text generate_content endpoint and
    # replies via WasenderApi. Off by default so a fresh deploy doesn't
    # surprise the operator. The text model lets the operator override
    # the default (gemini-2.5-flash) if their account has access to a
    # better one — leave empty to use the default.
    "whatsapp_bot_enabled":     False,
    "whatsapp_bot_text_model":  "",
    # Public base URL the backend should advertise when handing media URLs
    # to WasenderApi (image / video / audio / document sends). Wasender's
    # servers fetch the file from this URL, so it must resolve to THIS
    # host from the public internet. When empty we fall back to whatever
    # base URL the inbound request carries — which works when the
    # operator accesses the SPA via the Cloudflare tunnel itself but
    # fails ("must be publicly accessible") when they're hitting
    # localhost / a LAN IP directly. Set this to your tunnel hostname
    # (e.g. https://clinicmac.primewave2.tech) so local-browser access
    # still produces public media URLs.
    "public_base_url":          "",
    # Per-vertical Live Agent (AudioSocket) bind settings. Empty/zero
    # means "use the global state.cda_* default" (back-compat). The
    # restaurant defaults to 8093 so it doesn't fight the clinic's 8092.
    "live_agent_enabled":       False,
    "live_agent_bind_host":     "0.0.0.0",
    "live_agent_bind_port":     8093,
}


def load_escalation_config() -> dict:
    """Return the operator-saved escalation config, falling back to
    DEFAULT_ESCALATION when the file is missing / unreadable / malformed.
    The returned dict has every key from DEFAULT_ESCALATION (so callers
    can assume all keys exist) — saved overrides merge on top."""
    cfg = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_ESCALATION.items()}
    if _ESCALATION_PATH.exists():
        try:
            raw = _ESCALATION_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                for k in cfg:
                    if k in data and isinstance(data[k], type(cfg[k])):
                        cfg[k] = data[k]
        except Exception:
            logger.exception("failed to read %s — falling back to defaults", _ESCALATION_PATH)
    return cfg


def save_escalation_config(patch: dict) -> dict:
    """Merge `patch` into the on-disk escalation config and return the
    new full config. Unknown keys are ignored; type mismatches are
    silently dropped (the UI is the authority on shape)."""
    current = load_escalation_config()
    for k, v in (patch or {}).items():
        if k not in current:
            continue
        if isinstance(current[k], list) and isinstance(v, list):
            # Coerce list items to str + strip + drop blanks; keeps the
            # file tidy regardless of whitespace the UI sent.
            current[k] = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(current[k], bool):
            current[k] = bool(v)
        elif isinstance(current[k], int) and not isinstance(v, bool):
            try: current[k] = max(1, int(v))
            except Exception: pass
        elif isinstance(current[k], str):
            # Trim + length-cap; the AMI secret can be up to ~256 chars,
            # supervisor_extension is short, both fit comfortably under
            # this ceiling and the cap exists only to prevent abuse.
            current[k] = str(v or "").strip()[:256]
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ESCALATION_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return current


# Always-on guardrails — appended verbatim after the persona, even if the
# operator has saved a custom persona via the "Apply to Live Agent" button.
# Privacy + anti-fabrication are non-negotiable.
_GUARDRAILS = """

## CRITICAL GUARDRAILS — these override any persona text
You MUST follow every rule in this block. They are non-negotiable.

### Anti-fabrication
- NEVER claim a menu item, price, ingredient, dish, package, table,
  or policy exists unless you have just seen it in (a) a tool result
  on THIS call, or (b) the Knowledge Base above.
- BEFORE quoting a dish or price, call `list_menu_items` (or
  `list_menu_categories` first if you need to navigate). If a search
  comes back empty, say plainly: "ما عندنا هذا اليوم" / "We don't
  have that on the menu today" and offer the closest alternative
  from the SAME category.
- BEFORE quoting a catering option, call `list_catering_packages`
  — never invent package names, guest ranges, or prices.
- A short "I don't have that information, let me check" is ALWAYS
  correct. A confident wrong answer is NEVER correct.

### Never fabricate a tool result
- `reservation_id`, `order_id`, `table_number`, and any other
  identifier you say to the caller MUST come from the literal JSON
  body of a SUCCESSFUL tool response on THIS call. Quoting a
  plausible-looking identifier ("RES-001", "CAT-0042") that you
  made up is forbidden.
- NEVER tell the caller "your table is booked" / "حجزت لك" UNLESS
  `create_reservation` just returned a response containing a
  `reservation_id`. If it returned an `error`, the booking did
  NOT happen — say so honestly, fix whatever was wrong (no free
  tables for that slot, party_size too small, bad date format),
  retry, and only then confirm.
- Same rule for `update_catering_order` — only say "تعديل الطلب
  تم" / "I've updated your order" AFTER the tool returned a
  successful response with the updated order body.

### Reservation flow — list before book
- BEFORE confirming a slot to the caller, call `list_available_tables`
  for the requested date + time + party_size and read OUT LOUD the
  table options that come back (or, if you're auto-picking, name
  the table you intend to assign). Only AFTER the caller confirms
  do you call `create_reservation`.
- If `list_available_tables` returns an empty list, say plainly
  "ما عندنا طاولة متاحة في هذا الوقت" / "We're fully booked at
  that time" and offer the nearest later slot the caller is willing
  to accept. Do NOT silently downgrade the party size or change the
  time without asking.

### Catering update flow — lookup before mutate
- BEFORE calling `update_catering_order`, ALWAYS call
  `lookup_catering_order` (by order_id if the caller has it, or by
  client_phone if not) so you have the current items, event_date,
  event_time, and guest count.
- Read the current order back to the caller in the language they
  are speaking. Confirm WHICH fields they want changed before
  calling `update_catering_order`.
- `items` is a FULL REPLACEMENT — if the caller wants to "add an
  extra kibbeh", you must pass the existing items PLUS the new one,
  not just the new one. Reading the order back first makes this
  natural.
- If you're only changing the date / time / guest count, omit
  `items` entirely so the tool leaves them alone.

### Speaking numbers — ALWAYS digit by digit
When you SAY any of these to the caller, read every digit one at a
time. Never group them as cardinals.

  - Phone numbers          ("plus nine six six, five, zero, one, …")
  - Reservation IDs        ("R E S, zero, zero, four")
  - Catering order IDs     ("C A T, zero, zero, four, two")
  - Table numbers          ("T, zero, five" — never "table five")
  - Card / postal codes, anything alphanumeric

Exception: prices, durations, guest counts, dates spoken naturally
("two hundred fifty riyals", "ninety minutes", "thirty guests",
"May seventeenth") stay as cardinals. The rule applies to
identifiers, not quantities.

### Time format — speak 12-hour, with AM/PM
- Internally the tools use 24-hour time ("13:00", "20:30"). When
  you SPEAK a time to the caller, convert to 12-hour:
    English  → "1:00 PM", "8:30 PM", "12:00 noon"
    Arabic   → "الواحدة بعد الظهر", "الثامنة والنصف مساءً"
- NEVER say "twenty thirty" or "20:30" to a caller.

### Arabic gender — DEFAULT MASCULINE
- Default to MASCULINE forms for the caller: "تفضل", "تقدر",
  "أهلاً وسهلاً بك" (not "بكِ"), "كيف حالك" (not "حالكِ").
- Switch to feminine ONLY after one of these:
    1. The caller's voice is unambiguously female (consistent high
       pitch across multiple utterances).
    2. The caller stated a female name (Sara, Fatima, Nora, …) or
       used a feminine self-reference.
- When unsure, MASCULINE wins. Never ask a caller their gender —
  it's rude in a restaurant context.

### Filling forms — agent-side, not caller-side
- For `guest_name` you fill the form yourself based on what the
  caller said. Don't ask "and how do you spell that in English?"
  — your job is to romanise an Arabic name if needed (Fahad,
  Mohammed, Aisha, …).
- For `guest_phone`, repeat the digits back for confirmation
  before passing them to `create_reservation`.

### Hours, halal, and policy questions
- For opening hours, location, halal status, delivery zone,
  minimum order, delivery fee, payment methods, parking,
  reservation/cancellation policy, and majlis capacity — the
  Knowledge Base is authoritative. Don't invent.
- We are 100% halal. No pork, no alcohol, no alcohol-containing
  ingredients. If asked: "نحن مطعم حلال، ما عندنا".

### Tools — use them, don't fake them
- `list_menu_categories()` — quick nav before listing items
- `list_menu_items(category_id?, search?, max_results?)` — for
  any dish / price / ingredient quote
- `list_catering_packages(event_type?, max_guests?)` — for any
  catering enquiry
- `list_available_tables(date, time, party_size?, floor?)` —
  BEFORE every booking confirmation
- `create_reservation(guest_name, guest_phone, date, time,
  party_size, table_number?, notes?)` — ONLY after caller confirms
- `lookup_catering_order(order_id? | client_phone?)` — BEFORE
  every catering update
- `update_catering_order(order_id, event_date?, event_time?,
  items?, guests?, notes?)` — ONLY after caller confirms the
  exact change
- `flag_for_supervisor(reason, severity?)` — silently, when a
  human supervisor needs to take over
- `end_call(reason)` — see the persona "End of call" section
"""


def _build_roster_block() -> str:
    """Inject the actual clinic + provider rosters from snapshot.json into
    the system instruction so the agent CANNOT make up clinics or doctors
    that don't exist. This is the structural anti-hallucination defense —
    the persona text alone is too easy for the model to drift away from."""
    snap = load_snapshot()
    clinics = [c for c in snap.get("clinics", []) if c.get("active", True)]
    providers = [p for p in snap.get("providers", []) if p.get("active", True)]

    if clinics:
        clinic_lines = "\n".join(
            f"- {c.get('id')} · {c.get('name')} / {c.get('name_ar')} · "
            f"{c.get('specialty')} ({c.get('specialty_ar')}) · "
            f"{c.get('location')}"
            for c in clinics
        )
    else:
        clinic_lines = "- (no clinics in the current snapshot)"

    if providers:
        prov_lines = "\n".join(
            f"- {p.get('id')} · {p.get('name')} / {p.get('name_ar')} · "
            f"{p.get('role')} · {p.get('specialty')} ({p.get('specialty_ar')})"
            for p in providers
        )
    else:
        prov_lines = "- (no providers in the current snapshot)"

    return (
        "\n\n## AVAILABLE CLINICS — the ONLY clinics that exist here\n"
        "If a caller asks for a clinic or specialty that is NOT in this list, "
        "say plainly that you do not have that service. Never invent a clinic.\n"
        f"{clinic_lines}\n"
        "\n## ON-STAFF PROVIDERS — the ONLY people who work here\n"
        "If a caller asks about a doctor who is NOT in this list, say plainly "
        "that no one by that name works here. Never invent a name, gender, "
        "or specialty for someone not listed.\n"
        f"{prov_lines}"
    )


def build_current_time_block() -> str:
    """Module-public so the WhatsApp Bot's text-mode system instruction
    can reuse the same authoritative date+time block. Without it, the
    model defaults to its training-cutoff date and runs list_free_slots
    against an old year (we observed it calling `date='2024-06-13'` when
    today is in 2026). Returns a self-contained markdown block ready
    to concatenate onto a system instruction."""
    import datetime as _dt
    now = time.localtime()
    weekday_en = ["Sunday", "Monday", "Tuesday", "Wednesday",
                  "Thursday", "Friday", "Saturday"][(now.tm_wday + 1) % 7]
    weekday_ar = ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء",
                  "الخميس", "الجمعة", "السبت"][(now.tm_wday + 1) % 7]
    today_d  = _dt.date(now.tm_year, now.tm_mon, now.tm_mday)
    tom_d    = today_d + _dt.timedelta(days=1)
    dat_d    = today_d + _dt.timedelta(days=2)
    weekday_en_of = lambda d: ["Sunday", "Monday", "Tuesday", "Wednesday",
                                "Thursday", "Friday", "Saturday"][(d.weekday() + 1) % 7]
    weekday_ar_of = lambda d: ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء",
                                "الخميس", "الجمعة", "السبت"][(d.weekday() + 1) % 7]
    return (
        "\n\n## CURRENT TIME (authoritative — do not invent a different day)\n"
        f"- Today: {today_d.isoformat()} ({weekday_en} / {weekday_ar})\n"
        f"- Tomorrow: {tom_d.isoformat()} ({weekday_en_of(tom_d)} / {weekday_ar_of(tom_d)})\n"
        f"- Day after tomorrow: {dat_d.isoformat()} ({weekday_en_of(dat_d)} / {weekday_ar_of(dat_d)})\n"
        f"- Right now: {time.strftime('%H:%M', now)} ({time.tzname[0]})\n"
        "- When the caller says 'tomorrow' / 'بكرة' / 'غداً', use the\n"
        "  EXACT date from the 'Tomorrow' line above. NEVER recompute\n"
        "  a date from the weekday name — you have been observed making\n"
        "  off-by-one and off-by-two errors doing that.\n"
        "- When referring to today say 'اليوم' / 'today' — never the\n"
        "  weekday name on its own.\n"
        "- The `list_free_slots` tool already filters past times and any\n"
        "  slot within the 15-minute booking buffer. Quote ONLY what it\n"
        "  returns."
    )


def _build_system_instruction() -> str:
    current = build_current_time_block()
    # Escalation triggers — operator-editable. Defines exactly when the
    # agent should call `flag_for_supervisor`. We render the saved
    # keywords + scenarios into the prompt every call so changes from
    # Call Center → Configuration take effect on the next dial without
    # restarting the service.
    esc = load_escalation_config()
    kw_en = [w for w in esc.get("keywords_en", []) if str(w).strip()]
    kw_ar = [w for w in esc.get("keywords_ar", []) if str(w).strip()]
    scen  = [s for s in esc.get("scenarios",   []) if str(s).strip()]
    escalation_block = (
        "\n\n## ESCALATION — flag for a human supervisor\n"
        "Use JUDGMENT, not a checklist. Read the situation and call\n"
        "`flag_for_supervisor(reason, severity)` silently — do NOT tell\n"
        "the caller you're flagging. Keep talking normally; a supervisor\n"
        "joins quietly or takes over.\n"
        "\n"
        "### When to flag (any of these — interpret broadly, ignore exact wording)\n"
        "1. The caller sounds **angry, frustrated, upset, sarcastic, or\n"
        "   raises their voice** — even subtly. Trust your read of the tone.\n"
        "2. The caller wants to speak to a **human, person, manager,\n"
        "   supervisor, or anyone other than an AI**. Any language, any\n"
        "   phrasing — the EXACT WORDS DO NOT MATTER. 'I want a manager',\n"
        "   'أبغى أحكي مع المدير', 'is there a real person?', 'human please',\n"
        "   'أحكي مع بشر', 'put me through to someone' — all the same request.\n"
        "3. The caller is **repeating the same request and not getting\n"
        "   what they need**, or you've said the same thing 3+ times.\n"
        "4. The caller mentions a **medical emergency you cannot triage**.\n"
        "5. The caller **threatens to file a complaint** or contact regulators.\n"
        "6. You **feel stuck** — multiple tool errors, the situation is\n"
        "   beyond what your tools can resolve, or continuing would only\n"
        "   make things worse.\n"
    )
    if kw_en or kw_ar or scen:
        escalation_block += (
            "\n### Operator-provided hints (illustrative, NOT a closed list)\n"
            "These are examples of what to watch for, written by the operations\n"
            "team. Use them as hints — rely on the SITUATION, not exact matches.\n"
        )
        if kw_en:
            escalation_block += "- Example English phrasings: " + ", ".join(f'"{w}"' for w in kw_en) + "\n"
        if kw_ar:
            escalation_block += "- Example Arabic phrasings: " + ", ".join(f'"{w}"' for w in kw_ar) + "\n"
        if scen:
            escalation_block += "- Scenarios flagged by operations:\n"
            for s in scen:
                escalation_block += f"  · {s}\n"
    escalation_block += (
        "\n### How to call it\n"
        "- `reason` = one short sentence the supervisor will read on the\n"
        "  Dashboard. Be specific: 'Caller asked for manager twice (angry)'.\n"
        "- `severity` = 'high' for anger, complaints, emergencies, threats\n"
        "  to escalate. 'normal' otherwise.\n"
        "- **Re-flag every time a trigger occurs again.** If a supervisor\n"
        "  acknowledged your earlier flag and the caller is again asking\n"
        "  for the manager or remains angry, FLAG AGAIN. Multiple flags on\n"
        "  the same call signal escalating urgency and are NEVER spam.\n"
        "  Treat each occurrence as fresh — never reason 'I already\n"
        "  flagged this'.\n"
    )

    return (
        f"{load_persona().strip()}"
        f"{_GUARDRAILS}\n\n"
        f"{load_kb().strip()}"
        f"{_build_roster_block()}"
        f"{current}"
        f"{escalation_block}"
    )


# ============================================================================
# CallSession — one TCP connection from Asterisk = one call = one Gemini session
# ============================================================================

class CallSession:
    def __init__(
        self, call_id: str, peer: str,
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        svc: "RestaurantLiveAgentService",
    ) -> None:
        self.call_id = call_id
        self.peer = peer
        self.reader = reader
        self.writer = writer
        self.svc = svc
        self.uuid: Optional[str] = None
        self.started_at = time.time()
        # Caller -> Gemini (16 kHz mono PCM after upsample)
        self.audio_in: asyncio.Queue = asyncio.Queue(maxsize=200)
        # Gemini -> Caller (24 kHz mono PCM, downsampled in the write loop)
        self.audio_out: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.stop_evt = asyncio.Event()
        self._upstate = None
        self._downstate = None
        self._out_leftover: bytes = b""
        self._next_send_at: Optional[float] = None
        self.echo_until: float = 0.0
        # Supervisor mute: when True, _write_loop drains audio_out
        # without sending to AudioSocket so the agent's voice is
        # suppressed for the caller. Used when a supervisor barges in
        # via ChanSpy and needs to take over verbally — the agent stays
        # connected (still receives caller audio, still updates the
        # dashboard transcript) but its voice is silenced.
        self.muted: bool = False
        self.heard_text = ""
        self.spoken_text = ""
        # ElevenLabs TTS phrase buffer. Gemini's output_transcription
        # arrives word-by-word; forwarding each word independently makes
        # ElevenLabs start a new synthesis window per word and produces
        # audible micro-gaps. Instead we accumulate text here and flush
        # to TTS only at clause/sentence punctuation, or when the buffer
        # exceeds TTS_PHRASE_FLUSH chars (whichever comes first).
        # turn_complete always flushes whatever's left.
        self._tts_buffer = ""
        # Persistent per-call recording state — flushed to disk in
        # RestaurantLiveAgentService._handle's finally block via _save_recording.
        # Caller frames arrive continuously over AudioSocket (one every 20 ms),
        # so concatenating them yields a perfect wall-clock timeline of what
        # the caller sent. Agent chunks are intermittent (Gemini only emits
        # while it's actually speaking) so we tag each chunk with the
        # seconds-since-call-start offset at which it left the wire, so the
        # mixer can overlay it at the right position.
        self._caller_pcm8k: list[bytes] = []
        self._agent_pcm8k:  list[tuple[float, bytes]] = []
        self.turns: list[dict] = []   # [{role, text, ts}]
        # ---- Fabrication detector state ---------------------------------
        # Whitelist of identifiers actually returned by successful tool
        # calls on THIS call. Anything the agent SPEAKS that matches an
        # identifier pattern but isn't in here is a fabrication, and we
        # send a correction to the model immediately.
        self._issued_file_numbers:    set[str] = set()
        self._issued_appointment_ids: set[str] = set()
        self._issued_patient_ids:     set[str] = set()
        # Times (HH:MM, 24-hour) that list_free_slots actually returned
        # at some point on this call. The detector cross-references any
        # clock-time the agent SPEAKS against this set — catches the
        # "agent offered 6:30 PM when the clinic closes at 5" failure.
        self._issued_slot_times:      set[str] = set()
        # Identifiers we've already nagged the model about — prevents
        # us from spamming corrections every chunk while the agent
        # repeats the same fabricated value mid-sentence.
        self._corrected_ids:          set[str] = set()
        self._corrected_times:        set[str] = set()
        # Set once we've nudged the agent that it tried to schedule
        # without a patient_id — prevents spamming the same correction
        # on every subsequent list_free_slots call before the agent has
        # had a chance to act on it.
        self._nudged_no_patient_ctx:  bool = False
        # Optional caller phone — populated if the dialplan ever passes
        # CALLERID via an out-of-band channel (TODO; see DEMOSITEMAP).
        self.caller_phone: Optional[str] = None
        # Supervisor flag — non-None when the agent (or auto-detect)
        # has raised this call for a human to take over. Snapshots
        # include it so a Dashboard that connects after the flag was
        # raised still sees the red row. Cleared by ack_flag().
        self.active_flag: Optional[dict] = None

    # ----- supervisor mute --------------------------------------------
    def set_muted(self, muted: bool) -> dict:
        """Toggle the supervisor mute for this call. When `muted=True`,
        the write loop drains audio_out without forwarding to AudioSocket
        so the agent's voice is suppressed; the caller hears only the
        supervisor (via ChanSpy) until unmuted. We also drain any
        in-flight buffer on entry so the agent doesn't spit out a stale
        sentence when unmuted."""
        new_val = bool(muted)
        if new_val == self.muted:
            return {"muted": self.muted}
        self.muted = new_val
        if new_val:
            # Drop everything queued so the agent doesn't resume mid-sentence
            # on unmute. The session itself stays alive (Gemini keeps
            # listening + transcribing the caller for the dashboard).
            try:
                while not self.audio_out.empty():
                    self.audio_out.get_nowait()
            except Exception:
                pass
            self._out_leftover = b""
        self.svc._broadcast({
            "type":    "muted",
            "call_id": self.call_id,
            "muted":   self.muted,
        })
        logger.info("call %s: set_muted=%s", self.call_id, self.muted)
        return {"muted": self.muted}

    # ----- supervisor flag plumbing ------------------------------------
    def set_flag(self, flag: dict) -> None:
        """Mark this call as needing supervisor attention. Broadcasts
        a `supervisor_flag` event AND persists on the session so that
        Dashboards that connect later (via snapshot) see the flag too.
        Re-flagging overwrites — operator sees the latest reason."""
        payload = {
            "reason":   str(flag.get("reason") or "").strip() or "(no reason given)",
            "severity": (flag.get("severity") or "normal").strip().lower(),
            "source":   flag.get("source") or "agent",
            "ts":       time.time(),
        }
        if payload["severity"] not in ("low", "normal", "high"):
            payload["severity"] = "normal"
        self.active_flag = payload
        self.svc._broadcast({
            "type":    "supervisor_flag",
            "call_id": self.call_id,
            "flag":    payload,
        })

    def ack_flag(self) -> bool:
        """Operator-side acknowledgement. Returns True if there was a
        flag to clear, False otherwise."""
        if self.active_flag is None:
            return False
        self.active_flag = None
        self.svc._broadcast({
            "type":    "supervisor_flag_ack",
            "call_id": self.call_id,
        })
        return True

    async def _nudge_no_patient_context(self, session, tool_name: str) -> None:
        """Agent invoked a scheduling tool without ever obtaining a
        patient_id from create_patient or lookup_patient_*. The booking
        flow can't complete — create_appointment will 404 on the
        invented patient_id, and even if we don't get that far, the
        patient record will silently never be created. Inject a system
        override and reset the nudged-flag when the agent finally does
        get a real patient_id (see whitelist populator above)."""
        if self._nudged_no_patient_ctx:
            return
        self._nudged_no_patient_ctx = True
        logger.warning(
            "clinic call %s: nudge — agent called %s with no patient_id",
            self.call_id, tool_name,
        )
        self.svc._broadcast({
            "type":    "no_patient_context",
            "call_id": self.call_id,
            "tool":    tool_name,
        })
        msg = (
            "(system override) STOP. You just called "
            f"`{tool_name}` but you have NOT yet created or identified "
            "this caller — no `create_patient` and no `lookup_patient_*` "
            "tool has returned a patient_id on this call. Any "
            "appointment you try to book now WILL fail, and the "
            "details the caller just shared will NOT be saved.\n\n"
            "Do this RIGHT NOW, in order:\n"
            "  1. If you just finished collecting NEW-patient details "
            "(name + whatever else they gave you), call "
            "`create_patient` with that data. Only `name` is strictly "
            "required — pass empty strings for anything the caller "
            "didn't share.\n"
            "  2. If this is a RETURNING caller you should have "
            "already looked up, call `lookup_patient_by_phone` (or by "
            "id_number / file_number) now.\n"
            "  3. Once you have a real patient_id from the tool's "
            "response, THEN re-call the scheduling tool.\n\n"
            "Do not quote any slot times or appointment confirmations "
            "until step 3 has succeeded. If you already mentioned a "
            "time to the caller, that time is NOT booked — correct "
            "yourself."
        )
        try:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=msg)]),
                turn_complete=True,
            )
        except Exception:
            logger.exception("clinic call %s: failed to send no-patient nudge",
                             self.call_id)

    # Phrase-buffer flush helpers — used by the ElevenLabs path so we
    # send phrase-sized chunks to TTS instead of single words. Sentence
    # punctuation (. ! ? ؟) triggers an immediate flush; clause commas
    # (, ، ;) and length > TTS_PHRASE_FLUSH also trigger one. Less
    # frequent boundaries → smoother prosody, no audible micro-gaps.

    _TTS_SENTENCE_END = set(".!?؟")
    _TTS_CLAUSE_END   = set(",؛;:،")
    TTS_PHRASE_FLUSH  = 120   # chars

    async def _tts_flush_phrase(self, tts) -> None:
        """If the current `_tts_buffer` ends at a phrase boundary OR is
        long enough, send everything up to (and including) that boundary
        to ElevenLabs and keep the tail for the next delta."""
        buf = self._tts_buffer
        if not buf:
            return
        # Find the latest hard sentence end.
        cut = -1
        for i in range(len(buf) - 1, -1, -1):
            if buf[i] in self._TTS_SENTENCE_END:
                cut = i + 1
                break
        # No sentence boundary — fall back to the latest clause comma
        # but only if the buffer is already medium-large (otherwise we
        # still get word-sized chunks).
        if cut < 0 and len(buf) >= self.TTS_PHRASE_FLUSH // 2:
            for i in range(len(buf) - 1, -1, -1):
                if buf[i] in self._TTS_CLAUSE_END:
                    cut = i + 1
                    break
        # Still no boundary AND buffer is huge → just flush the whole
        # thing on a word boundary so we don't pile up forever.
        if cut < 0 and len(buf) >= self.TTS_PHRASE_FLUSH:
            cut = buf.rfind(" ") + 1
            if cut <= 0:
                cut = len(buf)
        if cut <= 0:
            return
        chunk = buf[:cut]
        self._tts_buffer = buf[cut:]
        try:
            await tts.send_text(chunk)
        except Exception as e:
            logger.warning("call %s: tts.send_text failed: %r", self.call_id, e)

    async def _tts_drain(self, tts) -> None:
        """End-of-turn: flush whatever's still in the buffer regardless
        of boundary so the last word doesn't get held back."""
        if not self._tts_buffer:
            return
        chunk = self._tts_buffer
        self._tts_buffer = ""
        try:
            await tts.send_text(chunk)
        except Exception as e:
            logger.warning("call %s: tts.send_text (drain) failed: %r",
                            self.call_id, e)

    async def _check_fabrications(self, session) -> None:
        """Scan the agent's accumulated transcribed speech for
        identifier patterns (file_number, appointment_id) and, for any
        match NOT in our per-call whitelist of tool-issued IDs, inject
        a correction back into the Gemini Live session AND broadcast a
        warning so the dashboard surfaces the silent failure."""
        fabricated: list[tuple[str, str]] = []  # [(kind, normalised_id), ...]

        for m in _FILE_PATTERN.finditer(self.spoken_text):
            # Recompose the id without the whitespace/hyphens the
            # transcription may have inserted between digits.
            raw = m.group(0)
            normalised = re.sub(r"[\s\-]", "", raw).upper()
            if len(normalised) != 7:
                continue
            if normalised in self._issued_file_numbers: continue
            if normalised in self._corrected_ids:       continue
            self._corrected_ids.add(normalised)
            fabricated.append(("file_number", normalised))

        for m in _APT_PATTERN.finditer(self.spoken_text):
            raw = m.group(0)
            normalised = re.sub(r"[\s\-]", "-", raw).upper()
            # Normalise to APT-NNN form.
            digits = re.sub(r"\D", "", normalised)
            if not digits: continue
            canonical = f"APT-{digits}"
            if canonical in self._issued_appointment_ids: continue
            if canonical in self._corrected_ids:          continue
            self._corrected_ids.add(canonical)
            fabricated.append(("appointment_id", canonical))

        # Clock times the agent SPOKE — we only check these once we have
        # at least one set of slots from list_free_slots / appointment
        # lookups to compare against. Without that whitelist we can't
        # tell legit recall from invention (mentioning the current time,
        # for example, should not trigger a warning).
        if self._issued_slot_times:
            # Each spoken time match becomes a SET of plausible
            # interpretations. A bare "4:30" with no AM/PM marker is
            # ambiguous — the agent very likely meant 16:30 (clinics
            # operate afternoons) but the transcription dropped the
            # "PM". So we accept the match if ANY interpretation
            # matches the whitelist. Previously the strict-24h read
            # produced a false positive that told the agent to apologise
            # for a booking that actually succeeded.
            spoken_candidates: list[set[str]] = []
            for m in _TIME_AMPM.finditer(self.spoken_text):
                h = int(m.group(1))
                mins = int(m.group(2) or 0)
                ampm = m.group(3).lower()
                if ampm == "p" and h < 12: h += 12
                if ampm == "a" and h == 12: h = 0
                if h > 23: continue
                # AM/PM explicit → single interpretation.
                spoken_candidates.append({f"{h:02d}:{mins:02d}"})
            for m in _TIME_24H.finditer(self.spoken_text):
                h = int(m.group(1)); mins = int(m.group(2))
                options = {f"{h:02d}:{mins:02d}"}
                if h < 12:
                    options.add(f"{h + 12:02d}:{mins:02d}")
                spoken_candidates.append(options)

            def _within_15(a: str, b: str) -> bool:
                ah, am = int(a[:2]), int(a[3:])
                bh, bm = int(b[:2]), int(b[3:])
                return abs((ah * 60 + am) - (bh * 60 + bm)) <= 15

            for options in spoken_candidates:
                accepted = False
                for t in options:
                    if t in self._issued_slot_times:
                        accepted = True
                        break
                    if any(_within_15(t, real) for real in self._issued_slot_times):
                        accepted = True
                        break
                if accepted:
                    continue
                # Pick the primary (sorted) interpretation for the warning.
                primary = sorted(options)[0]
                if primary in self._corrected_times: continue
                self._corrected_times.add(primary)
                fabricated.append(("slot_time", primary))

        if not fabricated:
            return

        for kind, ident in fabricated:
            logger.warning(
                "clinic call %s: fabrication detected — agent spoke %s '%s' "
                "that was never returned by a tool on this call",
                self.call_id, kind, ident,
            )
            self.svc._broadcast({
                "type":     "fabrication",
                "call_id":  self.call_id,
                "kind":     kind,
                "value":    ident,
            })

        # Build a single concise correction prompt — sending many in
        # quick succession just confuses the model.
        bullets = "\n".join(
            f"- You said {kind} '{ident}' — no tool returned that value."
            for kind, ident in fabricated
        )
        correction = (
            "(system override) STOP. You just spoke an identifier you "
            "did NOT receive from any tool on this call:\n"
            f"{bullets}\n"
            "That is fabrication. The caller's record was NOT actually "
            "created. Right now you must either:\n"
            "  (a) call the correct tool (create_patient / "
            "create_appointment) with the data you've collected so far, "
            "OR\n"
            "  (b) apologise to the caller in their language, tell them "
            "the system did not save the record, and ask reception to "
            "complete it on arrival.\n"
            "Do NOT repeat the fabricated value. Do NOT pretend the "
            "previous statement was correct."
        )
        try:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=correction)]),
                turn_complete=True,
            )
        except Exception:
            logger.exception("clinic call %s: failed to send fabrication correction",
                             self.call_id)

    def _append_turn(self, role: str, text: str) -> None:
        # Extend the last turn if the speaker hasn't switched, else start a
        # new one — keeps the transcript readable and the file small.
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["text"] += text
        else:
            self.turns.append({"role": role, "text": text, "ts": time.time()})

    # ---- wire protocol -----------------------------------------------------
    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
        header = await reader.readexactly(3)
        msg_type = header[0]
        length = struct.unpack(">H", header[1:3])[0]
        payload = await reader.readexactly(length) if length else b""
        return msg_type, payload

    async def _send_audio(self, pcm8k: bytes) -> None:
        FRAME = 320  # 20 ms @ 8 kHz, 16-bit mono
        n_frames = len(pcm8k) // FRAME
        if n_frames == 0:
            return
        self.echo_until = max(
            self.echo_until, time.time() + n_frames * 0.020 + 0.35,
        )
        now = time.monotonic()
        if self._next_send_at is None or self._next_send_at < now - 0.05:
            self._next_send_at = now
        for i in range(0, len(pcm8k), FRAME):
            chunk = pcm8k[i:i + FRAME]
            self.writer.write(bytes([_AS_AUDIO]) + struct.pack(">H", len(chunk)) + chunk)
            await self.writer.drain()
            self._next_send_at += 0.020
            delay = self._next_send_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)

    async def _hangup(self) -> None:
        try:
            self.writer.write(bytes([_AS_HANGUP, 0, 0]))
            await self.writer.drain()
        except Exception:
            pass

    async def _delayed_stop(self, delay_s: float) -> None:
        """Set stop_evt after a short delay — used by the end_call tool so
        the agent's spoken goodbye actually leaves the wire before we
        tear down the AudioSocket."""
        try:
            await asyncio.sleep(delay_s)
        finally:
            self.stop_evt.set()

    # ---- pumps -------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self.stop_evt.is_set():
                msg_type, payload = await self._read_frame(self.reader)
                if msg_type == _AS_HANGUP:
                    logger.info("clinic call %s: peer hangup", self.call_id)
                    self.stop_evt.set()
                    return
                if msg_type == _AS_UUID:
                    self.uuid = payload.hex()
                    continue
                if msg_type == _AS_DTMF:
                    digit = payload.decode("ascii", "replace") if payload else ""
                    logger.info("clinic call %s: DTMF %s", self.call_id, digit)
                    continue
                if msg_type == _AS_ERROR:
                    logger.warning("clinic call %s: peer error: %r", self.call_id, payload)
                    continue
                if msg_type == _AS_AUDIO and payload:
                    # Capture the raw 8 kHz caller frame before any
                    # resampling — recording stays lossless.
                    self._caller_pcm8k.append(payload)
                    # Half-duplex echo gate — always active. Caller audio is
                    # dropped for ~350ms past the end of the agent's last
                    # outgoing frame, regardless of interruption mode. The
                    # gate window is short enough that real barge-in still
                    # works (the caller's *next* syllable lands as soon as
                    # the agent stops talking), but blocks the agent's own
                    # voice from echoing back through speakerphone/weak
                    # echo-cancellation paths and (a) triggering Gemini's
                    # VAD into self-interrupting mid-sentence (caused 10s
                    # voice freezes) and (b) feeding garbage into
                    # input_transcription (caused weird/garbled words).
                    if time.time() < self.echo_until:
                        continue
                    pcm16k, self._upstate = audioop.ratecv(
                        payload, _SAMPLE_WIDTH, 1, 8000, 16000, self._upstate,
                    )
                    try:
                        self.audio_in.put_nowait(pcm16k)
                    except asyncio.QueueFull:
                        try: self.audio_in.get_nowait()
                        except Exception: pass
                        try: self.audio_in.put_nowait(pcm16k)
                        except Exception: pass
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("clinic call %s: read loop crashed", self.call_id)
        finally:
            # Always signal — same reason as _write_loop's finally.
            self.stop_evt.set()

    async def _write_loop(self) -> None:
        FRAME = 320
        try:
            while not self.stop_evt.is_set():
                try:
                    pcm24k = await asyncio.wait_for(self.audio_out.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                # Supervisor mute — drop the chunk entirely. We still
                # had to dequeue it (otherwise audio_out backs up and
                # stalls Gemini), but we don't forward it to AudioSocket
                # nor capture it to the recording. On unmute the agent
                # will only emit fresh audio.
                if self.muted:
                    self._out_leftover = b""
                    continue
                pcm8k, self._downstate = audioop.ratecv(
                    pcm24k, _SAMPLE_WIDTH, 1, 24000, 8000, self._downstate,
                )
                if not pcm8k:
                    continue
                buf = self._out_leftover + pcm8k
                n_complete = (len(buf) // FRAME) * FRAME
                if n_complete:
                    # Capture what we actually sent to the caller, in the
                    # same 8 kHz wire format — recording is the call as
                    # the caller heard it. Stamp with seconds-since-call-start
                    # so the mixer can place it at the right offset on the
                    # caller timeline.
                    offset_s = time.time() - self.started_at
                    chunk = buf[:n_complete]
                    self._agent_pcm8k.append((offset_s, chunk))
                    await self._send_audio(chunk)
                self._out_leftover = buf[n_complete:]
        except Exception as e:
            logger.warning("clinic call %s: write loop ended: %s", self.call_id, e)
        finally:
            # The transport may have died before the read loop noticed
            # (e.g. Asterisk closed TCP without sending an AudioSocket
            # HANGUP frame, or wrote() raised on a half-closed socket).
            # If we don't signal stop_evt, run() stays parked on
            # `await self.stop_evt.wait()` and the whole call session
            # never finalises — Gemini eventually closes the WS idle,
            # and we accumulate zombie sessions.
            self.stop_evt.set()

    async def _run_cai_loop(self) -> None:
        """ElevenLabs Conversational AI path. Replaces Gemini Live's
        WebSocket session with an ElevenLabs CAI session that bundles
        STT + LLM + TTS. We keep the same audio_in / audio_out queues
        so AudioSocket framing + the in-process recording capture work
        unchanged. The auto-managed agent is ensured before connecting
        so the call always dials a config that matches the current
        persona, KB, voice, and tool set."""
        from . import cai_elevenlabs as _cai
        try:
            agent_id = await _cai.ensure_agent(
                persona=load_persona(),
                kb=load_kb(),
                voice_id=state.elevenlabs_voice_id or "EXAVITQu4vr4xnSDxMaL",
                model_id=state.elevenlabs_model_id or "eleven_multilingual_v2",
                first_message=state.cda_greeting or "",
            )
        except Exception as e:
            logger.error("call %s: CAI ensure_agent failed: %s",
                          self.call_id, e)
            return
        if not agent_id:
            logger.error("call %s: CAI agent_id missing — check Settings", self.call_id)
            return

        tool_ctx: dict = {
            "call_id":     self.call_id,
            "broadcast":   self.svc._broadcast,
            "end_requested": False,
            "set_flag":    self.set_flag,
            "caller_phone": self.caller_phone or "",
            "identified_patient_ids": self._issued_patient_ids,
        }

        sess = _cai.Session(
            call_id=self.call_id, agent_id=agent_id,
            audio_in_q=self.audio_in, audio_out_q=self.audio_out,
            # Bypass _write_loop's 24k→8k resample chain (built for
            # Gemini Live's native 24 kHz). The CAI bytes are already
            # 8 kHz µ-law — decode + write straight to AudioSocket so
            # we don't pay for an 8→24→8 round-trip that smears the
            # voice ("robotic" feel reported on the demo).
            on_agent_audio_8k=self._send_audio,
            on_caller_text=lambda t: self._cai_on_text("caller", t),
            on_agent_text=lambda  t: self._cai_on_text("agent",  t),
            ctx=tool_ctx,
        )
        try:
            await sess.start()
            # Drive the call until end_requested fires (from end_call
            # tool) or the WS closes.
            while not self.stop_evt.is_set() \
                  and not tool_ctx.get("end_requested") \
                  and not sess._closed:
                await asyncio.sleep(0.25)
        except Exception:
            logger.exception("call %s: CAI session crashed", self.call_id)
        finally:
            await sess.close()
            logger.info("call %s: CAI session ended", self.call_id)

    def _cai_on_text(self, who: str, text: str) -> None:
        """Mirror caller/agent transcripts from the CAI session into our
        existing heard_text / spoken_text + dashboard broadcast pipeline
        so the Call Center UI behaves identically to the Gemini path."""
        if not text:
            return
        if who == "caller":
            self.heard_text += text
        else:
            self.spoken_text += text
        self._append_turn(who, text)
        try:
            self.svc._broadcast({
                "type": "transcript", "call_id": self.call_id,
                "who": who, "text": text,
            })
        except Exception:
            pass

    async def _gemini_loop(self) -> None:
        # Voice provider branch — when the operator picked ElevenLabs
        # Conversational AI in Settings, route this call entirely through
        # their stack (STT + LLM + TTS bundled) instead of Gemini Live.
        # The CAI path keeps using our existing audio queues + tool
        # dispatcher so AudioSocket framing, the recording capture, the
        # WS broadcasts, and every restaurant tool keep working unchanged.
        from . import cai_elevenlabs as _cai
        if _cai.is_active():
            await self._run_cai_loop()
            return

        if not state.gemini_api_key:
            logger.error("clinic call %s: Gemini API key not set", self.call_id)
            return
        model = state.gemini_model
        client = genai.Client(
            api_key=state.gemini_api_key,
            http_options={"api_version": _GEMINI_API_VERSION},
        )

        # Per-call context that tool implementations close over. Lets a
        # tool set `end_requested = True` to terminate the call, or call
        # `broadcast(event)` to push something to subscribers.
        #
        # `identified_patient_ids` is the set of patient_ids the agent
        # has actually verified on THIS call (via successful
        # lookup_patient_* or create_patient). cancel/reschedule tools
        # refuse to operate on appointments that don't belong to one of
        # these — without this, the agent could silently mutate
        # another patient's record. The set is mutated in-place from
        # the receive loop as new tools land, so all subsequent tool
        # calls see the latest membership.
        tool_ctx: dict = {
            "call_id":        self.call_id,
            "broadcast":      self.svc._broadcast,
            "end_requested":  False,
            # Exposed so `flag_for_supervisor` can mutate the session
            # state + broadcast in one shot (see CallSession.set_flag).
            "set_flag":       self.set_flag,
            "caller_phone":   self.caller_phone or "",
            "identified_patient_ids": self._issued_patient_ids,
        }

        # ElevenLabs voice path — we try to open the TTS WebSocket BEFORE
        # we tell Gemini to go text-only. If the WS fails (bad key, bad
        # voice id, network), we silently fall back to Gemini's native
        # audio so the call still works.
        tts: Optional[tts_elevenlabs.ElevenLabsTTSSession] = None
        tts_pump_task: Optional[asyncio.Task] = None
        if tts_elevenlabs.is_configured():
            tts = tts_elevenlabs.ElevenLabsTTSSession(call_id=self.call_id)
            try:
                await tts.start()
                async def _tts_pump(_tts=tts) -> None:
                    try:
                        async for pcm in _tts.audio_frames():
                            try:
                                self.audio_out.put_nowait(pcm)
                            except asyncio.QueueFull:
                                try: self.audio_out.get_nowait()
                                except Exception: pass
                                try: self.audio_out.put_nowait(pcm)
                                except Exception: pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("call %s: tts pump ended: %r", self.call_id, e)
                tts_pump_task = asyncio.create_task(
                    _tts_pump(), name=f"el-pump-{self.call_id}",
                )
            except Exception as e:
                logger.warning("call %s: ElevenLabs TTS failed to start (%s) — "
                                "falling back to Gemini native audio",
                                self.call_id, e)
                tts = None

        # Gemini Live's *-live* models reject `response_modalities=[TEXT]`
        # with WS 1011 (internal error) — text-only output is not
        # supported on the realtime API. We always keep AUDIO modality
        # so the session connects, and use `output_audio_transcription`
        # to capture the text Gemini is speaking. When ElevenLabs is
        # active, we feed that transcription text into the TTS bridge
        # AND drop Gemini's audio frames so the caller only hears
        # ElevenLabs's voice. Costs a bit extra (Gemini synthesises audio
        # we throw away) but voice quality + Arabic accuracy > the extra
        # synthesis cost.
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=_build_system_instruction())],
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=state.cda_voice or "Aoede",
                    ),
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Barge-in: LOW start-of-speech sensitivity so faint echo /
            # breathing / line noise can't be mis-classified as speech and
            # cause the agent to self-interrupt (the 10-second voice freeze
            # symptom). Real caller speech still triggers reliably; the
            # always-on echo gate above is belt to LOW's braces.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=not bool(state.cda_interruption_enabled),
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=600,
                    prefix_padding_ms=200,
                ),
            ),
            tools=build_tools(),
        )

        try:
            async with client.aio.live.connect(model=model, config=cfg) as session:
                logger.info(
                    "restaurant call %s: Gemini Live connected (model=%s, voice=%s)",
                    self.call_id, model,
                    f"elevenlabs:{tts.voice_id}" if tts else f"gemini:{state.cda_voice or 'Aoede'}",
                )

                if state.cda_greeting:
                    try:
                        await session.send_client_content(
                            turns=types.Content(role="user", parts=[
                                types.Part(text=f"(system) Greet the caller now with: {state.cda_greeting}")
                            ]),
                            turn_complete=True,
                        )
                    except Exception as e:
                        logger.warning("clinic call %s: greeting failed: %s", self.call_id, e)

                async def feed():
                    try:
                        while not self.stop_evt.is_set():
                            try:
                                chunk = await asyncio.wait_for(self.audio_in.get(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if not chunk:
                                continue
                            await session.send_realtime_input(
                                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # Swallow Gemini's "WS already closed" once the
                        # session is winding down; we don't want it to
                        # propagate as an unretrieved task exception.
                        logger.info("clinic call %s: feed loop ended: %r",
                                    self.call_id, e)
                    finally:
                        self.stop_evt.set()

                async def receive():
                    try:
                        while not self.stop_evt.is_set():
                            async for resp in session.receive():
                                data_bytes = getattr(resp, "data", None)
                                if data_bytes:
                                    # When ElevenLabs is wired in, throw
                                    # away Gemini's synthesised audio so
                                    # the caller only ever hears the
                                    # ElevenLabs voice that gets pushed
                                    # onto audio_out by the TTS pump task.
                                    if tts is None:
                                        try: self.audio_out.put_nowait(data_bytes)
                                        except asyncio.QueueFull:
                                            try: self.audio_out.get_nowait()
                                            except Exception: pass
                                            try: self.audio_out.put_nowait(data_bytes)
                                            except Exception: pass

                                sc = getattr(resp, "server_content", None)
                                if sc:
                                    if getattr(sc, "interrupted", False):
                                        drained = 0
                                        while not self.audio_out.empty():
                                            try:
                                                self.audio_out.get_nowait()
                                                drained += 1
                                            except Exception: break
                                        self._out_leftover = b""
                                        self.echo_until = 0.0
                                        # On caller barge-in, also drop the
                                        # in-flight ElevenLabs synthesis so
                                        # we don't keep streaming voice the
                                        # caller is already talking over.
                                        # Reset the phrase buffer too —
                                        # the half-sentence the agent was
                                        # mid-saying is now stale.
                                        if tts is not None:
                                            self._tts_buffer = ""
                                            try: await tts.cancel()
                                            except Exception: pass
                                        if drained:
                                            logger.info("call %s: interrupted (%d frames dropped)", self.call_id, drained)
                                    # End-of-turn from Gemini → flush
                                    # ElevenLabs so any tail text gets
                                    # synthesised right away rather than
                                    # sitting in its buffer.
                                    if tts is not None and getattr(sc, "turn_complete", False):
                                        # Push whatever's left in the
                                        # phrase buffer before flushing
                                        # so no trailing word gets
                                        # stranded waiting for the next
                                        # turn.
                                        await self._tts_drain(tts)
                                        try: await tts.flush()
                                        except Exception: pass
                                    it = getattr(sc, "input_transcription", None)
                                    if it and getattr(it, "text", None):
                                        self.heard_text += it.text
                                        self._append_turn("caller", it.text)
                                        self.svc._broadcast({
                                            "type": "transcript", "call_id": self.call_id,
                                            "who": "caller", "text": it.text,
                                        })
                                    ot = getattr(sc, "output_transcription", None)
                                    if ot and getattr(ot, "text", None):
                                        self.spoken_text += ot.text
                                        self._append_turn("agent", ot.text)
                                        self.svc._broadcast({
                                            "type": "transcript", "call_id": self.call_id,
                                            "who": "agent", "text": ot.text,
                                        })
                                        # ElevenLabs path: buffer the
                                        # text and flush only at phrase
                                        # boundaries so the synthesiser
                                        # gets natural-sized chunks. See
                                        # `_tts_flush_phrase` for the
                                        # boundary heuristic.
                                        if tts is not None:
                                            self._tts_buffer += ot.text
                                            await self._tts_flush_phrase(tts)
                                        # Fabrication check — scan everything
                                        # the agent has said so far against
                                        # the whitelist of IDs returned by
                                        # tools on this call. The model's
                                        # transcription may chunk the ID
                                        # across messages, so we re-scan the
                                        # full spoken_text each time.
                                        await self._check_fabrications(session)
                                # Tool call → execute → return FunctionResponse.
                                tc = getattr(resp, "tool_call", None)
                                if tc:
                                    responses = []
                                    for fc in (tc.function_calls or []):
                                        args = dict(fc.args or {})
                                        logger.info("clinic call %s: tool_call %s(%s)",
                                                    self.call_id, fc.name, args)
                                        self.svc._broadcast({
                                            "type":     "tool_call",
                                            "call_id":  self.call_id,
                                            "name":     fc.name,
                                            "args":     args,
                                        })
                                        # If the lookup succeeded, update the
                                        # Dashboard's caller name + phone.
                                        result = execute_tool(fc.name, args, tool_ctx)
                                        # Always broadcast the outcome so the
                                        # Dashboard can flag tool errors
                                        # (e.g. create_appointment failing with
                                        # "patient not found") instead of the
                                        # user only finding out when the
                                        # appointment doesn't appear.
                                        has_error = isinstance(result, dict) and bool(result.get("error"))
                                        self.svc._broadcast({
                                            "type":     "tool_result",
                                            "call_id":  self.call_id,
                                            "name":     fc.name,
                                            "args":     args,
                                            "ok":       (not has_error),
                                            "error":    (result.get("error") if has_error else None),
                                            "result":   None if has_error else result,
                                        })
                                        # Whitelist any IDs the tool actually issued
                                        # on this call so the fabrication detector
                                        # knows they're real.
                                        if not has_error and isinstance(result, dict):
                                            fn = result.get("file_number")
                                            if fn: self._issued_file_numbers.add(str(fn))
                                            aid = result.get("appointment_id")
                                            if aid: self._issued_appointment_ids.add(str(aid))
                                            pid = result.get("patient_id")
                                            if pid: self._issued_patient_ids.add(str(pid))
                                            # Lookups return the patient under "patient"
                                            sub = result.get("patient")
                                            if isinstance(sub, dict):
                                                if sub.get("file_number"):
                                                    self._issued_file_numbers.add(str(sub["file_number"]))
                                                if sub.get("patient_id"):
                                                    self._issued_patient_ids.add(str(sub["patient_id"]))
                                            # list_free_slots → every free HH:MM
                                            # returned, across every clinic, into
                                            # the slot whitelist.
                                            for c in (result.get("clinics") or []):
                                                for slot in (c.get("free_slots") or []):
                                                    self._issued_slot_times.add(str(slot))
                                            # list_patient_appointments → existing
                                            # appointment times are legit too.
                                            for a in (result.get("appointments") or []):
                                                t = a.get("time")
                                                if t: self._issued_slot_times.add(str(t))
                                            # Single-appointment results.
                                            t = result.get("time")
                                            if t: self._issued_slot_times.add(str(t))
                                        if fc.name.startswith("lookup_patient") and isinstance(result, dict) and result.get("found"):
                                            p = result.get("patient") or {}
                                            self.caller_phone = p.get("phone") or self.caller_phone
                                            # Mirror into tool_ctx so the
                                            # next tool call (e.g.
                                            # send_whatsapp_template) can
                                            # read the phone without
                                            # touching the session object.
                                            tool_ctx["caller_phone"] = self.caller_phone or ""
                                            self.svc._broadcast({
                                                "type":    "caller_identified",
                                                "call_id": self.call_id,
                                                "name":    p.get("name") or p.get("name_ar"),
                                                "phone":   p.get("phone"),
                                            })
                                        responses.append(types.FunctionResponse(
                                            id=fc.id, name=fc.name,
                                            response={"result": result},
                                        ))
                                    if responses:
                                        try:
                                            await session.send_tool_response(function_responses=responses)
                                        except Exception:
                                            logger.exception("send_tool_response failed")
                                    # Structural nudge: if the agent moved
                                    # into scheduling without first
                                    # creating or identifying the caller,
                                    # block here. Past failures we caught
                                    # this way: intake done + confirmed +
                                    # agent jumps to list_free_slots
                                    # without calling create_patient, so
                                    # the would-be record is never saved
                                    # and the subsequent create_appointment
                                    # would 404 anyway.
                                    scheduling_tools = (
                                        "list_free_slots",
                                        "create_appointment",
                                        "list_patient_appointments",
                                        "cancel_appointment",
                                        "reschedule_appointment",
                                    )
                                    for fc in (tc.function_calls or []):
                                        if (fc.name in scheduling_tools
                                                and not self._issued_patient_ids):
                                            await self._nudge_no_patient_context(session, fc.name)
                                            break
                                    # If a tool requested hangup, drop out cleanly
                                    # after the agent's closing line finishes.
                                    if tool_ctx.get("end_requested"):
                                        # Give the agent ~3s to finish its
                                        # spoken goodbye before we close the
                                        # AudioSocket.
                                        asyncio.create_task(self._delayed_stop(3.0))
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # Gemini's WS closes with ConnectionClosedOK at the
                        # end of a normal call — don't escalate, just log
                        # and signal the rest of the pipeline to shut down.
                        logger.info("clinic call %s: receive loop ended: %r",
                                    self.call_id, e)
                    finally:
                        self.stop_evt.set()

                feeder = asyncio.create_task(feed())
                receiver = asyncio.create_task(receive())
                stopper = asyncio.create_task(self.stop_evt.wait())
                done, pending = await asyncio.wait(
                    {feeder, receiver, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Drain results from completed tasks so their exceptions
                # don't surface as "Task exception was never retrieved".
                for t in done:
                    try: t.exception()
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        pass

        except Exception:
            logger.exception("clinic call %s: Gemini Live failed", self.call_id)
        finally:
            # Tear down the ElevenLabs TTS session + its audio-pump task
            # so we don't leak the WebSocket or the asyncio.Task between
            # calls. close() sends EOS and then closes the WS.
            if tts_pump_task is not None and not tts_pump_task.done():
                tts_pump_task.cancel()
                try: await tts_pump_task
                except Exception: pass
            if tts is not None:
                try: await tts.close()
                except Exception: pass
                logger.info("call %s: ElevenLabs TTS closed (stats=%s)",
                             self.call_id, tts.stats())

    async def run(self) -> None:
        max_s = max(60, int(state.cda_max_call_s or 900))
        async def deadline():
            await asyncio.sleep(max_s)
            logger.info("clinic call %s: hit max duration %ds", self.call_id, max_s)
            self.stop_evt.set()
        tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._write_loop()),
            asyncio.create_task(self._gemini_loop()),
            asyncio.create_task(deadline()),
        ]
        try:
            await self.stop_evt.wait()
        finally:
            for t in tasks:
                t.cancel()
            await self._hangup()


# ============================================================================
# Service — TCP listener + per-call dispatch + tiny pub/sub bus
# ============================================================================

class RestaurantLiveAgentService:
    def __init__(self) -> None:
        self._server: Optional[asyncio.AbstractServer] = None
        self._task: Optional[asyncio.Task] = None
        self._calls: dict[str, CallSession] = {}
        self._subs: set[asyncio.Queue] = set()
        self.last_error: Optional[str] = None
        self.bound_at: Optional[float] = None

    # ----- subscriber bus -----
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, payload: dict) -> None:
        for q in list(self._subs):
            try: q.put_nowait(payload)
            except asyncio.QueueFull:
                try: q.get_nowait()
                except Exception: pass
                try: q.put_nowait(payload)
                except Exception: pass

    # ----- lifecycle -----
    # Each method reads the vertical's own escalation.json for the
    # enabled flag + bind host/port, so multiple verticals can run
    # side-by-side on different TCP ports without sharing the global
    # state.cda_* settings (which would fight over one port).
    def _resolved_bind(self) -> tuple[bool, str, int]:
        cfg = load_escalation_config()
        enabled = bool(cfg.get("live_agent_enabled", False))
        if cfg.get("live_agent_enabled") is None:
            enabled = bool(state.cda_enabled)
        host = (cfg.get("live_agent_bind_host") or "").strip() \
               or (state.cda_bind_host or "0.0.0.0")
        try:
            port = int(cfg.get("live_agent_bind_port") or 0)
        except Exception:
            port = 0
        if not port:
            port = int(state.cda_bind_port or 8093)
        return enabled, host, port

    def status(self) -> dict:
        enabled, host, port = self._resolved_bind()
        return {
            "running":    self._server is not None,
            "enabled":    enabled,
            "host":       host,
            "port":       port,
            "active":     len(self._calls),
            "bound_at":   self.bound_at,
            "last_error": self.last_error,
        }

    def active_calls(self) -> list[dict]:
        return [
            {
                "call_id":    c.call_id,
                "peer":       c.peer,
                "uuid":       c.uuid,
                "started_at": c.started_at,
                "duration_s": int(time.time() - c.started_at),
                "heard":      c.heard_text[-400:],
                "spoken":     c.spoken_text[-400:],
                # Replayed on (re)connect so dashboards that came online
                # AFTER a supervisor flag was raised still see the red row.
                "flag":       c.active_flag,
                # Replayed on (re)connect so the dashboard's Mute button
                # reflects the current state even for late joiners.
                "muted":      c.muted,
            }
            for c in self._calls.values()
        ]

    def get_call(self, call_id: str) -> Optional[CallSession]:
        """Look up a live CallSession by id — used by the router's
        acknowledge_flag endpoint."""
        return self._calls.get(call_id)

    def apply_config(self) -> None:
        """Idempotent — call after edits to live_agent_enabled / host /
        port (escalation.json) or the legacy cda_* global state."""
        enabled, host, port = self._resolved_bind()
        if enabled and self._server is None:
            self.start()
        elif (not enabled) and self._server is not None:
            asyncio.create_task(self.stop())
        elif self._server is not None:
            sock = next(iter(self._server.sockets or []), None)
            if sock:
                cur_host, cur_port = sock.getsockname()[:2]
                if cur_port != port or cur_host != host:
                    asyncio.create_task(self._restart())

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="clinic-live-agent")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try: await self._server.wait_closed()
            except Exception: pass
            self._server = None
            self.bound_at = None
        for c in list(self._calls.values()):
            c.stop_evt.set()
        self._calls.clear()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _restart(self) -> None:
        await self.stop()
        await asyncio.sleep(0.1)
        self.start()

    async def _run(self) -> None:
        _enabled, host, port = self._resolved_bind()
        try:
            self._server = await asyncio.start_server(self._handle, host, port)
            self.bound_at = time.time()
            self.last_error = None
            logger.info("ClinicLiveAgent listening on %s:%d", host, port)
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.exception("ClinicLiveAgent bind/run failed")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_label = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        call_id = uuid.uuid4().hex[:10]
        session = CallSession(call_id, peer_label, reader, writer, self)
        self._calls[call_id] = session
        logger.info("ClinicLiveAgent: new call %s from %s", call_id, peer_label)
        self._broadcast({"type": "call_started", "call_id": call_id, "peer": peer_label, "started_at": session.started_at})
        try:
            await session.run()
        except Exception:
            logger.exception("clinic call %s crashed", call_id)
        finally:
            self._calls.pop(call_id, None)
            # Persist the call's recording + transcript to disk so the
            # History page can replay it. Always best-effort — a failing
            # save must never block the cleanup.
            try:
                saved_id = _save_recording(session)
                self._broadcast({"type": "call_ended", "call_id": call_id,
                                 "saved_call_id": saved_id})
                # Kick off the offline transcript pass — runs in the
                # background so the next call (or hangup) isn't blocked
                # by the Gemini upload.
                if saved_id:
                    asyncio.create_task(_enhance_transcript(saved_id))
            except Exception:
                logger.exception("clinic call %s: failed to save recording", call_id)
                self._broadcast({"type": "call_ended", "call_id": call_id})
            try: writer.close()
            except Exception: pass


# ============================================================================
# Call persistence — WAV (caller + agent) + JSON transcript
# ============================================================================

def _write_wav(path: Path, frames: list[bytes], rate_hz: int = 8000) -> None:
    """Write a list of signed-linear 16-bit mono PCM byte chunks as a WAV."""
    if not frames:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(b"".join(frames))


def _write_agent_wav(path: Path,
                     agent_chunks: list[tuple[float, bytes]],
                     total_s: float,
                     rate_hz: int = 8000) -> None:
    """Render the agent-only timeline as a full-call WAV — silence between
    the chunks, audio inside them — so the reader hears the agent talk in
    real call time, not back-to-back."""
    if not agent_chunks:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    bytes_per_sec = rate_hz * 2  # 16-bit mono
    last = agent_chunks[-1]
    span_s = max(total_s, last[0] + (len(last[1]) / bytes_per_sec))
    total_bytes = int(span_s * bytes_per_sec)
    if total_bytes <= 0:
        return
    buf = bytearray(total_bytes)
    for offset_s, chunk in agent_chunks:
        start = int(offset_s * bytes_per_sec) & ~1  # align to sample boundary
        end = start + len(chunk)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[start:end] = chunk
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(bytes(buf))


def _write_mixed_wav(path: Path,
                     caller_frames: list[bytes],
                     agent_chunks: list[tuple[float, bytes]],
                     total_s: float,
                     rate_hz: int = 8000) -> None:
    """Overlay the agent timeline on top of the caller timeline at the
    correct offsets, sample-by-sample (audioop.add). Produces a single
    'as the room sounded' WAV — perfect for listening back and for
    feeding a single audio stream to the offline transcript model."""
    bytes_per_sec = rate_hz * 2  # 16-bit mono
    caller_bytes = b"".join(caller_frames)
    span_s = total_s
    if agent_chunks:
        last = agent_chunks[-1]
        span_s = max(span_s, last[0] + (len(last[1]) / bytes_per_sec))
    span_s = max(span_s, len(caller_bytes) / bytes_per_sec)
    total_bytes = int(span_s * bytes_per_sec)
    if total_bytes <= 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    mixed = bytearray(total_bytes)
    # Lay the caller down first.
    n = min(len(caller_bytes), total_bytes)
    mixed[:n] = caller_bytes[:n]
    # Then add the agent chunks on top.
    for offset_s, chunk in agent_chunks:
        start = int(offset_s * bytes_per_sec) & ~1
        end = start + len(chunk)
        if end > len(mixed):
            mixed.extend(b"\x00" * (end - len(mixed)))
        existing = bytes(mixed[start:end])
        if len(existing) < len(chunk):
            existing = existing + b"\x00" * (len(chunk) - len(existing))
        try:
            summed = audioop.add(existing, chunk, 2)
        except audioop.error:
            summed = chunk
        mixed[start:start + len(summed)] = summed
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(bytes(mixed))


def _save_recording(session: CallSession) -> Optional[str]:
    """Persist the just-ended call. Returns the storage id used on disk —
    a sortable timestamp + short uid so the History page lists in time order
    even when the underlying call_ids are random hex.

    Layout under data/demos/restaurant/calls/<dir>/ :
        meta.json    — { call_id, started_at, ended_at, duration_s,
                         peer, uuid, caller_phone, persona_chars,
                         kb_chars, turns: [{role, text, ts}] }
        caller.wav   — 8 kHz mono, what the caller said
        agent.wav    — 8 kHz mono, what the agent said (post-resample)
    """
    if (
        not session.turns
        and not session._caller_pcm8k
        and not session._agent_pcm8k
    ):
        # Empty call (no audio, no transcript) — usually a probe / failed
        # handshake. Skip to keep the History clean.
        return None

    ended_at = time.time()
    started_at = session.started_at
    duration_s = max(0.0, ended_at - started_at)
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime(started_at))
    dir_id = f"{ts}_{session.call_id}"
    call_dir = _CALLS_DIR / dir_id

    try:
        _write_wav(call_dir / "caller.wav", session._caller_pcm8k)
    except Exception:
        logger.exception("write caller.wav failed")
    try:
        _write_agent_wav(call_dir / "agent.wav", session._agent_pcm8k, duration_s)
    except Exception:
        logger.exception("write agent.wav failed")
    try:
        _write_mixed_wav(call_dir / "mixed.wav", session._caller_pcm8k,
                         session._agent_pcm8k, duration_s)
    except Exception:
        logger.exception("write mixed.wav failed")

    meta = {
        "id":            dir_id,
        "call_id":       session.call_id,
        "started_at":    started_at,
        "ended_at":      ended_at,
        "duration_s":    int(duration_s),
        "peer":          session.peer,
        "uuid":          session.uuid,
        "caller_phone":  session.caller_phone,
        "turns":         list(session.turns),
        "enhanced_turns": None,           # filled in by background task
        "enhanced_status": "pending",     # pending → running → done | failed
        "persona_chars": len(load_persona()),
        "kb_chars":      len(load_kb()),
        "voice":         state.cda_voice or "Aoede",
    }
    try:
        call_dir.mkdir(parents=True, exist_ok=True)
        (call_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("write meta.json failed")

    return dir_id


def list_saved_calls(limit: int = 100) -> list[dict]:
    """Return saved-call summaries (newest first)."""
    if not _CALLS_DIR.exists():
        return []
    rows: list[dict] = []
    for call_dir in _CALLS_DIR.iterdir():
        if not call_dir.is_dir():
            continue
        meta_path = call_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append({
            "id":           meta.get("id") or call_dir.name,
            "call_id":      meta.get("call_id"),
            "started_at":   meta.get("started_at"),
            "ended_at":     meta.get("ended_at"),
            "duration_s":   meta.get("duration_s", 0),
            "peer":         meta.get("peer"),
            "caller_phone": meta.get("caller_phone"),
            "turn_count":   len(meta.get("turns") or []),
            "enhanced_status":     meta.get("enhanced_status") or (
                "done" if meta.get("enhanced_turns") else "pending"
            ),
            "enhanced_turn_count": len(meta.get("enhanced_turns") or []),
            "has_caller_wav": (call_dir / "caller.wav").exists(),
            "has_agent_wav":  (call_dir / "agent.wav").exists(),
            "has_mixed_wav":  (call_dir / "mixed.wav").exists(),
        })
    rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return rows[:limit]


def load_saved_call(call_id: str) -> Optional[dict]:
    """Return the full meta.json for one saved call, or None."""
    call_dir = _CALLS_DIR / call_id
    meta_path = call_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def call_audio_path(call_id: str, side: str) -> Optional[Path]:
    """Resolve the WAV path for one side ('caller', 'agent', or 'mixed').
    Returns None if the file doesn't exist — caller should 404."""
    if side not in ("caller", "agent", "mixed"):
        return None
    p = _CALLS_DIR / call_id / f"{side}.wav"
    return p if p.exists() else None


# ============================================================================
# Offline transcript enhancement — Gemini "audio understanding"
# ============================================================================
# Gemini Live's live transcription is approximate and sometimes mis-renders
# the Arabic / English mix. After the call ends we re-transcribe the mixed
# WAV with a non-Live Gemini model (response_schema = list of turns) and
# patch meta.json with `enhanced_turns`. The History page prefers that
# field when it's present and falls back to the live transcript otherwise.

_ENHANCE_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]


def _patch_meta(call_dir: Path, patch: dict) -> None:
    meta_path = call_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    meta.update(patch)
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("patch meta.json failed for %s", call_dir.name)


async def _enhance_transcript(call_dir_id: str) -> None:
    """Re-transcribe mixed.wav with Gemini offline and store the result
    on disk as `enhanced_turns`. Best-effort — never raises."""
    call_dir = _CALLS_DIR / call_dir_id
    mixed_path = call_dir / "mixed.wav"
    if not mixed_path.exists():
        _patch_meta(call_dir, {"enhanced_status": "failed",
                               "enhanced_error": "mixed.wav missing"})
        return
    if not state.gemini_api_key:
        _patch_meta(call_dir, {"enhanced_status": "failed",
                               "enhanced_error": "Gemini API key not set"})
        return

    _patch_meta(call_dir, {"enhanced_status": "running"})
    try:
        wav_bytes = mixed_path.read_bytes()
        client = genai.Client(api_key=state.gemini_api_key)
        audio_part = types.Part.from_bytes(
            data=wav_bytes, mime_type="audio/wav",
        )
        prompt = (
            "This is a phone-call recording between a Saudi clinic "
            "receptionist named Layla (الوكيل / agent — usually Arabic, "
            "may switch to English) and a caller (المتصل / caller). "
            "Produce an accurate, faithful transcript as a JSON array of "
            "turns in the order they were spoken. Each turn object has "
            "two keys:\n"
            "  - role: \"agent\" or \"caller\"\n"
            "  - text: what was actually said, preserving the original "
            "language (Arabic stays Arabic, English stays English). Do "
            "NOT translate. Do NOT paraphrase. Do NOT add commentary.\n"
            "Merge consecutive utterances from the same speaker. Skip "
            "silence, breathing, and DTMF tones. If a section is "
            "unintelligible, write [unintelligible] for that turn's text."
        )
        schema = types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "role": types.Schema(
                        type=types.Type.STRING,
                        enum=["agent", "caller"],
                    ),
                    "text": types.Schema(type=types.Type.STRING),
                },
                required=["role", "text"],
            ),
        )
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
        )

        last_err: Optional[Exception] = None
        text: Optional[str] = None
        for model in _ENHANCE_MODELS:
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=[audio_part, prompt],
                    config=cfg,
                )
                text = getattr(resp, "text", None)
                if text:
                    break
            except Exception as e:
                last_err = e
                logger.warning("enhance %s: model %s failed: %s",
                               call_dir_id, model, e)
                continue
        if not text:
            raise last_err or RuntimeError("no model returned text")

        turns = json.loads(text)
        if not isinstance(turns, list):
            raise ValueError("model output is not a JSON array")
        clean: list[dict] = []
        for t in turns:
            if not isinstance(t, dict): continue
            role = str(t.get("role") or "").strip().lower()
            body = str(t.get("text") or "").strip()
            if role not in ("agent", "caller") or not body:
                continue
            clean.append({"role": role, "text": body})

        _patch_meta(call_dir, {
            "enhanced_turns":  clean,
            "enhanced_status": "done",
            "enhanced_error":  None,
        })
        logger.info("enhance %s: stored %d turns", call_dir_id, len(clean))
    except Exception as e:
        logger.exception("enhance %s failed", call_dir_id)
        _patch_meta(call_dir, {
            "enhanced_status": "failed",
            "enhanced_error":  f"{type(e).__name__}: {e}",
        })


def delete_saved_call(call_id: str) -> bool:
    """Wipe one saved call's directory. Returns True if anything was
    removed."""
    call_dir = _CALLS_DIR / call_id
    if not call_dir.exists():
        return False
    for f in call_dir.iterdir():
        try: f.unlink()
        except Exception: pass
    try: call_dir.rmdir()
    except Exception: pass
    return True


restaurant_live_agent_service = RestaurantLiveAgentService()


# ============================================================================
# Debug log → WebSocket fan-out
# ============================================================================
# Attaches a logging.Handler to every clinic-module logger so anything
# we already write via logger.info / .warning / .exception is also
# broadcast to dashboards subscribed to `/agent/ws` as a `debug` event.
# The SPA's Call Center → Debug page subscribes and renders the stream
# in real time. Cheap: only the events the operator opens the page for
# are processed by them; backend cost is the format() of the record +
# a dict put on a Queue.

class _DebugBroadcastHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        # Plain message — keep payload small. Timestamp + level + logger
        # already travel as structured fields.
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            exc_text: Optional[str] = None
            if record.exc_info and self.formatter:
                try:
                    exc_text = self.formatter.formatException(record.exc_info)
                except Exception:
                    exc_text = None
            restaurant_live_agent_service._broadcast({
                "type":    "debug",
                "ts":      record.created,
                "level":   record.levelname,
                "logger":  record.name,
                "message": msg,
                "exc":     exc_text,
            })
        except Exception:
            # Never let logging crash the app.
            pass


def _install_debug_handler() -> None:
    """Attach the broadcast handler to the TOP-LEVEL clinic loggers
    once at import. Python's logging propagates child records up to
    ancestor loggers, so attaching to `demo_clinic` automatically
    captures every `demo_clinic.<sub>` logger too — attaching to BOTH
    fires each event twice in the Debug page (which is exactly what
    was happening before this cleanup). Children just need their level
    set so their INFO records aren't filtered before propagation."""
    roots = (
        "restaurant_live_agent",
        "restaurant_agent_tools",
        "demo_restaurant",
    )
    handler = _DebugBroadcastHandler()
    for name in roots:
        lg = logging.getLogger(name)
        if not any(isinstance(h, _DebugBroadcastHandler) for h in lg.handlers):
            lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    # Children: set level only (so INFO records aren't filtered before
    # propagating up to the root handler). Don't attach the handler —
    # that's what caused the duplicate log lines.
    for name in (
        "demo_restaurant.ami",
        "demo_restaurant.wasender",
        "demo_restaurant.whatsapp_inbox",
        "demo_restaurant.whatsapp_templates",
        "demo_restaurant.whatsapp_bot",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        # Defensive: remove any stale duplicate handlers a previous
        # version of this function (or a hot-reload) may have attached.
        for h in list(lg.handlers):
            if isinstance(h, _DebugBroadcastHandler):
                lg.removeHandler(h)


_install_debug_handler()
