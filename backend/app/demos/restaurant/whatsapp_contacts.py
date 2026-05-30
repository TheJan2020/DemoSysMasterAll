"""
Lightweight in-memory cache of WasenderApi's /contacts list.

Used for two things:

1. **Display name** — when an inbound message arrives from a contact
   whose `id` is an opaque LID (e.g. `233908264300569@lid`), the chat
   row would otherwise render as the raw digit string. The cache lets
   us substitute the `notify` push-name ("Ahmed", "خالد البندر") when
   Wasender has one on file.

2. **LID detection** — `is_lid_jid("…@lid")` returns True so callers
   (the chats endpoint, the send endpoint, the SPA) can branch:
   LID chats are read-only and shouldn't be patient-linked.

Privacy note: Wasender's `/contacts` does NOT expose a LID-to-phone
mapping on at least the per-session API key tier we tested. So we
cannot resolve a LID to a real phone number — only to a push name.
Reply / patient-link to LIDs is therefore impossible without the
caller separately sharing their phone in-band.

Cache: in-memory only, no disk persistence (the underlying data is
patient PII-adjacent and short-lived). Refreshes if the cache is
older than _TTL_SECONDS.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import httpx

from .live_agent import load_escalation_config
from .wasender import WASENDER_BASE_URL

logger = logging.getLogger("demo_restaurant.whatsapp_contacts")

_TTL_SECONDS = 600  # 10 min — contacts don't change often.
_REQUEST_TIMEOUT_S = 15.0

# Module-level cache.
_cache: dict[str, dict] = {}
_cache_loaded_at: float = 0.0


# ----------------------------------------------------------------------
# JID classification
# ----------------------------------------------------------------------

_LID_SUFFIX = "@lid"
_PHONE_SUFFIX = "@s.whatsapp.net"
_GROUP_SUFFIX = "@g.us"


def is_lid_jid(jid: str | None) -> bool:
    """Opaque WhatsApp privacy ID (no phone exposed)."""
    return bool(jid and isinstance(jid, str) and jid.endswith(_LID_SUFFIX))


def is_phone_jid(jid: str | None) -> bool:
    return bool(jid and isinstance(jid, str) and jid.endswith(_PHONE_SUFFIX))


def is_group_jid(jid: str | None) -> bool:
    return bool(jid and isinstance(jid, str) and jid.endswith(_GROUP_SUFFIX))


def digits_of(jid: str | None) -> str:
    """Bare digits — works for both phone JIDs and LIDs (just gives the
    integer ID portion). For an `@s.whatsapp.net` jid this is the actual
    phone; for a `@lid` jid it's the opaque LID integer (not useful for
    sending)."""
    if not jid:
        return ""
    return re.sub(r"\D", "", jid)


# ----------------------------------------------------------------------
# Cache fetch
# ----------------------------------------------------------------------

async def _fetch_contacts() -> list[dict]:
    """Pull /contacts from Wasender using the per-session API key. Never
    raises — returns [] on any failure (and logs)."""
    cfg = load_escalation_config()
    api_key = str(cfg.get("wasender_api_key") or "").strip()
    if not api_key:
        logger.info("contacts fetch skipped: wasender_api_key not set")
        return []
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            r = await client.get(
                f"{WASENDER_BASE_URL}/contacts",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept":        "application/json",
                },
            )
    except httpx.RequestError as e:
        logger.warning("contacts fetch network error: %s", e)
        return []
    if not (200 <= r.status_code < 300):
        logger.warning("contacts fetch HTTP %s: %s", r.status_code, r.text[:200])
        return []
    try:
        payload = r.json()
    except Exception:
        logger.warning("contacts fetch returned non-JSON: %s", r.text[:200])
        return []
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [c for c in items if isinstance(c, dict)]


async def _refresh_cache_if_stale() -> None:
    global _cache, _cache_loaded_at
    now = time.time()
    if (now - _cache_loaded_at) < _TTL_SECONDS and _cache:
        return
    items = await _fetch_contacts()
    if not items:
        # Don't wipe an existing populated cache on a transient failure —
        # we'd rather show slightly stale names than fall back to raw
        # digits.
        if not _cache:
            _cache_loaded_at = now  # avoid hammering on every call
        return
    new_cache: dict[str, dict] = {}
    for c in items:
        cid = c.get("id")
        if isinstance(cid, str) and cid:
            new_cache[cid] = c
    _cache = new_cache
    _cache_loaded_at = now
    logger.info("contacts cache refreshed: %d entries", len(new_cache))


# ----------------------------------------------------------------------
# Lookup helpers (these are the call surface for the rest of the app)
# ----------------------------------------------------------------------

async def lookup(jid: str | None) -> Optional[dict]:
    """Return the contact dict for this JID, or None. Triggers a cache
    refresh if stale."""
    if not jid:
        return None
    await _refresh_cache_if_stale()
    return _cache.get(jid)


async def display_name_for(jid: str | None) -> Optional[str]:
    """Friendly name to display in the chat list for this JID:
    notify (push name) → verifiedName → name. None if we have nothing
    or the contact isn't in the cache."""
    c = await lookup(jid)
    if not c:
        return None
    for k in ("notify", "verifiedName", "name"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ----------------------------------------------------------------------
# LID → phone canonicalisation
# ----------------------------------------------------------------------
# A single WhatsApp user can show up in Wasender twice on the same
# conversation — once as a phone JID (used when WE send a message TO
# them) and once as a LID (the opaque identifier WhatsApp uses when
# THEY message us with privacy mode on). The chat list otherwise
# splits this into two separate rows for the same person.
#
# We can stitch them back together when we have enough metadata to
# prove they're the same: the LID's push name (from /contacts) lines
# up with a patient's name on file (Arabic or English), AND we know
# that patient's phone. The patient's phone JID becomes the "canonical"
# JID; the LID chat is merged into it.
#
# Build the name → patient lookup once per request from the snapshot
# (see _build_name_index below), then pass it to canonical_jid_for()
# for each candidate JID.


def _name_key(s: str | None) -> str:
    """Normalise a name for case/whitespace-insensitive comparison."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def build_patient_name_index(patients: list[dict]) -> dict[str, str]:
    """{normalised_name: phone_jid} from the patient registry. Both the
    English and Arabic spellings index to the same patient's phone."""
    out: dict[str, str] = {}
    for p in patients or []:
        phone_digits = re.sub(r"\D", "", str(p.get("phone") or ""))
        if not phone_digits:
            continue
        phone_jid = f"{phone_digits}{_PHONE_SUFFIX}"
        for nm in (p.get("name"), p.get("name_ar")):
            k = _name_key(nm)
            if k:
                out[k] = phone_jid
    return out


async def canonical_jid_for(jid: str, name_index: dict[str, str]) -> str:
    """Return the phone-JID equivalent of this JID if we can prove it
    belongs to a known patient via the contact's push name; else the
    JID itself unchanged.

    Only kicks in for LID JIDs — phone JIDs and group JIDs are returned
    as-is."""
    if not is_lid_jid(jid):
        return jid
    if not name_index:
        return jid
    push_name = await display_name_for(jid)
    if not push_name:
        return jid
    canonical = name_index.get(_name_key(push_name))
    return canonical or jid


def cache_stats() -> dict:
    """For diagnostics — exposed via /whatsapp/raw_contacts or similar."""
    return {
        "count":     len(_cache),
        "loaded_at": _cache_loaded_at,
        "age_s":     (time.time() - _cache_loaded_at) if _cache_loaded_at else None,
    }
