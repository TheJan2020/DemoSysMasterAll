"""
Caller context lookup — shared by the Live Agent (voice) and the
WhatsApp bot. Given a caller phone (digits only, country-code first),
this returns a structured block summarising who they are and what
we've talked to them about before:

  - contact:           row from contacts.json (None if unknown)
  - past_voice_calls:  recent voice-call rows for this phone
  - past_whatsapp:     summary of past WhatsApp bot conversation

The agent uses this to recognise returning callers, skip questions
we've already answered, and personalise the greeting.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("demo_primewave.caller_context")

# How many past voice calls / how many recent WhatsApp turns to summarise.
PAST_CALLS_LIMIT = 5
PAST_WA_TURNS_PREVIEW = 6


def _digits(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _phone_match(a: str, b: str) -> bool:
    """Phones match if either is a suffix of the other (handles leading
    country code variations: 966591697226 vs 591697226 etc.)."""
    a = _digits(a); b = _digits(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith(b) or b.endswith(a)


def lookup(phone: str) -> dict:
    """Return a dict the system-instruction builder can format.

    Shape:
      {
        "phone":            "<digits>",
        "contact":          {...} | None,
        "past_voice_calls": [{...}, ...],
        "past_whatsapp":    {turn_count, customer_data, recent_text:"..."} | None,
      }
    """
    phone = _digits(phone)
    out = {
        "phone":            phone,
        "contact":          None,
        "past_voice_calls": [],
        "past_whatsapp":    None,
    }
    if not phone:
        return out

    # ---- Contact lookup (contacts.json) ----------------------------------
    try:
        from . import contacts as contacts_mod
        for c in contacts_mod.list_contacts():
            if _phone_match(c.get("phone"), phone):
                out["contact"] = c
                break
    except Exception:
        logger.exception("primewave caller_context: contacts lookup failed")

    # ---- Past voice calls (calls/<dir>/meta.json) ------------------------
    # Search by phone in customer_data OR in the new caller_phone field.
    try:
        from .live_agent import list_saved_calls
        for row in list_saved_calls(limit=200):
            cd_phone = (row.get("customer_data") or {}).get("phone") or ""
            if (_phone_match(row.get("caller_phone"), phone)
                    or _phone_match(cd_phone, phone)):
                out["past_voice_calls"].append({
                    "id":            row.get("id"),
                    "started_at":    row.get("started_at"),
                    "duration_s":    row.get("duration_s") or 0,
                    "customer_data": row.get("customer_data") or {},
                    "turn_count":    row.get("turn_count") or 0,
                })
                if len(out["past_voice_calls"]) >= PAST_CALLS_LIMIT:
                    break
    except Exception:
        logger.exception("primewave caller_context: past-calls lookup failed")

    # ---- Past WhatsApp conversation -------------------------------------
    try:
        from . import whatsapp_bot
        for conv in whatsapp_bot.list_conversations():
            if _phone_match(conv.get("phone"), phone):
                # Pull a few recent user/assistant turns for short context.
                # We don't expose the full state to the agent — just the
                # tail — to keep tokens bounded.
                history = []
                # The list_conversations summary doesn't include the raw
                # history (it's just a counter). Fetch directly.
                import threading
                from . import whatsapp_bot as bot_mod
                with bot_mod._LOCK:
                    state_blob = bot_mod._load_locked()
                full = (state_blob.get("conversations") or {}).get(
                    conv["phone"], {})
                turns = full.get("history") or []
                tail = turns[-PAST_WA_TURNS_PREVIEW:]
                for t in tail:
                    history.append({
                        "role": t.get("role"),
                        "text": (t.get("text") or "").strip(),
                    })
                out["past_whatsapp"] = {
                    "turn_count":    conv.get("turn_count") or 0,
                    "customer_data": conv.get("customer_data") or {},
                    "recent":        history,
                }
                break
    except Exception:
        logger.exception("primewave caller_context: past-whatsapp lookup failed")

    return out


def format_for_prompt(ctx: dict, *, channel: str = "voice") -> str:
    """Render the context block as natural-language text the model can
    follow as part of its system instruction. Returns an empty string
    when there's nothing useful to inject (unknown caller, no history).
    """
    if not ctx or not ctx.get("phone"):
        return ""
    contact = ctx.get("contact")
    past_calls = ctx.get("past_voice_calls") or []
    past_wa = ctx.get("past_whatsapp")
    if not contact and not past_calls and not past_wa:
        return ""

    lines = ["", "## RETURNING CALLER — known to us"]

    if contact:
        c = contact
        bits = [
            ("Client ID", c.get("client_id")),
            ("Name",      c.get("name")),
            ("Phone",     c.get("phone")),
            ("Email",     c.get("email")),
            ("Location",  c.get("location")),
            ("Interest",  c.get("interest")),
            ("Phase",     c.get("project_phase")),
            ("Source",    c.get("lead_source")),
        ]
        lines.append("Contact record:")
        for k, v in bits:
            if v: lines.append(f"  - {k}: {v}")

    if past_calls:
        lines.append(f"Past voice calls: {len(past_calls)} (most recent first)")
        for i, c in enumerate(past_calls[:3], start=1):
            cd = c.get("customer_data") or {}
            cd_str = ", ".join(f"{k}={v}" for k, v in cd.items()) or "—"
            lines.append(f"  {i}. {c.get('duration_s') or 0}s · captured: {cd_str}")

    if past_wa:
        wa = past_wa
        cd = wa.get("customer_data") or {}
        cd_str = ", ".join(f"{k}={v}" for k, v in cd.items()) or "—"
        lines.append(f"Past WhatsApp conversation: "
                     f"{wa.get('turn_count') or 0} turns · captured: {cd_str}")
        if wa.get("recent"):
            lines.append("Recent WhatsApp turns:")
            for t in wa["recent"][-4:]:
                who = "caller" if t.get("role") == "user" else "you"
                snippet = (t.get("text") or "").replace("\n", " ")[:120]
                lines.append(f"  [{who}] {snippet}")

    # Behaviour rules for the model when context is present.
    chan = "voice call" if channel == "voice" else "WhatsApp chat"
    lines += [
        "",
        f"BEHAVIOUR — this is a {chan} from a known caller:",
        "  - Greet them BY NAME if we have one. Do NOT ask 'who's calling?' "
        "or 'what's your name?'.",
        "  - Treat fields above as already captured — call "
        "`record_customer_info` only for NEW or CORRECTED info.",
        "  - If there's past conversation history above, acknowledge it "
        "naturally (e.g. 'متابعة لمحادثتنا السابقة عن …' / 'Following up "
        "on our earlier chat about …').",
        "  - Stay polite and concise. Don't dump the full history at them.",
    ]
    return "\n".join(lines)
