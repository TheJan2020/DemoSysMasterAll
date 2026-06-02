"""
Primewave demo — contacts ledger.

Single-file JSON store under data/demos/primewave/contacts.json. The
schema mirrors the six fields the agent's `record_customer_info` tool
captures, plus Email and Lead Source which the operator manages by
hand (the agent doesn't ask for them on a phone call).

Each row:
    {id, name, phone, email, location, interest, project_phase,
     lead_source, created_at, updated_at}

interest, project_phase and lead_source are free-form strings — kept
that way so the operator can localise the labels in the SPA without a
backend deploy.

The first time the file is read on a clean install we drop in 20
Riyadh-flavoured dummy rows so the SPA's Contacts → List page isn't
empty for a fresh demo.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_primewave.contacts")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "primewave"
_PATH     = _DATA_DIR / "contacts.json"
_LOCK     = threading.Lock()

# Field whitelist — anything else in a POST is dropped on the floor.
# `client_id` is auto-managed (assigned on first persist, never patched
# from a write) so it's NOT in this list.
FIELDS = ["name", "phone", "email", "location",
          "interest", "project_phase", "lead_source"]

# Human-readable customer ID format. `next_client_id_seq()` finds the
# highest existing PW-### and returns the next one, so re-seeds and
# manual upserts never collide.
CLIENT_ID_PREFIX = "PW-"


def _client_id_seq(rid: str) -> int:
    """Extract the numeric tail of a PW-### id; -1 if it doesn't match."""
    if not isinstance(rid, str) or not rid.startswith(CLIENT_ID_PREFIX):
        return -1
    tail = rid[len(CLIENT_ID_PREFIX):]
    try:
        return int(tail)
    except ValueError:
        return -1


def _format_client_id(n: int) -> str:
    return f"{CLIENT_ID_PREFIX}{n:03d}"


# ---- Defaults / seed -------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _seed_rows() -> list[dict]:
    """20 dummy contacts, all in Riyadh districts. Mix of B2B / B2C,
    spread across project phases and lead sources so the SPA shows a
    realistic-looking demo right after install."""
    raw = [
        # (name, phone, email, district, interest, phase, source)
        ("Ahmed Al-Qahtani",    "+966501234567", "ahmed.q@example.sa",   "Al Olaya",      "B2B", "design",            "referral"),
        ("Sara Al-Mutairi",     "+966502345678", "sara.m@example.sa",    "Al Malqa",      "B2C", "under_construction","website"),
        ("Khalid Al-Saud",      "+966503456789", "khalid.s@example.sa",  "Al Yasmin",     "B2B", "finishing",         "exhibition"),
        ("Layla Al-Harbi",      "+966504567890", "layla.h@example.sa",   "Hittin",        "B2C", "operational",       "social_media"),
        ("Fahad Al-Otaibi",     "+966505678901", "fahad.o@example.sa",   "Al Nakheel",    "B2B", "design",            "google_ads"),
        ("Noura Al-Dosari",     "+966506789012", "noura.d@example.sa",   "Al Sahafa",     "B2C", "under_construction","walk_in"),
        ("Mohammed Al-Ghamdi",  "+966507890123", "m.ghamdi@example.sa",  "Al Olaya",      "B2B", "finishing",         "referral"),
        ("Reem Al-Shehri",      "+966508901234", "reem.s@example.sa",    "Al Wurud",      "B2C", "design",            "website"),
        ("Abdullah Al-Zahrani", "+966509012345", "abdullah.z@example.sa","King Abdullah", "B2B", "operational",       "exhibition"),
        ("Hessa Al-Qarni",      "+966500123456", "hessa.q@example.sa",   "Al Rabwah",     "B2C", "under_construction","social_media"),
        ("Bandar Al-Rashed",    "+966512345678", "bandar.r@example.sa",  "Al Olaya",      "B2B", "design",            "cold_call"),
        ("Maha Al-Khalifa",     "+966513456789", "maha.k@example.sa",    "Al Murabba",    "B2C", "finishing",         "referral"),
        ("Yousef Al-Sulaiman",  "+966514567890", "yousef.s@example.sa",  "Al Nakheel",    "B2B", "under_construction","website"),
        ("Aisha Al-Najjar",     "+966515678901", "aisha.n@example.sa",   "Diplomatic Q.", "B2C", "operational",       "google_ads"),
        ("Talal Al-Bukhari",    "+966516789012", "talal.b@example.sa",   "Al Sahafa",     "B2B", "design",            "exhibition"),
        ("Dalia Al-Faisal",     "+966517890123", "dalia.f@example.sa",   "Al Malqa",      "B2C", "under_construction","walk_in"),
        ("Saud Al-Mansouri",    "+966518901234", "saud.m@example.sa",    "Al Yasmin",     "B2B", "finishing",         "referral"),
        ("Hind Al-Anizi",       "+966519012345", "hind.a@example.sa",    "Hittin",        "B2C", "design",            "social_media"),
        ("Nawaf Al-Tamimi",     "+966520123456", "nawaf.t@example.sa",   "Al Wurud",      "B2B", "operational",       "website"),
        ("Ghaida Al-Asiri",     "+966521234567", "ghaida.a@example.sa",  "Al Olaya",      "B2C", "under_construction","cold_call"),
    ]
    now = _now()
    out: list[dict] = []
    for i, (name, phone, email, district, interest, phase, source) in enumerate(raw, start=1):
        out.append({
            "id":            uuid.uuid4().hex[:10],
            "client_id":     _format_client_id(i),
            "name":          name,
            "phone":         phone,
            "email":         email,
            "location":      f"Riyadh · {district}",
            "interest":      interest,
            "project_phase": phase,
            "lead_source":   source,
            "created_at":    now,
            "updated_at":    now,
        })
    return out


# ---- IO --------------------------------------------------------------------

def _load_locked() -> list[dict]:
    if not _PATH.exists():
        rows = _seed_rows()
        _save_locked(rows)
        logger.info("primewave contacts: seeded %d rows on first read", len(rows))
        return rows
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("primewave contacts.json corrupt — restoring seed")
        rows = _seed_rows()
        _save_locked(rows)
        return rows
    if isinstance(data, dict):
        rows = data.get("contacts") or data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    rows = [r for r in rows if isinstance(r, dict)]
    # Backfill — any contact missing a client_id gets the next free PW-###.
    # Idempotent: if all rows already have one, the file isn't touched.
    if rows and any(not r.get("client_id") for r in rows):
        used = {r.get("client_id") for r in rows if r.get("client_id")}
        next_n = max([_client_id_seq(c) for c in used] + [0]) + 1
        changed = False
        for r in rows:
            if not r.get("client_id"):
                while _format_client_id(next_n) in used:
                    next_n += 1
                r["client_id"] = _format_client_id(next_n)
                used.add(r["client_id"])
                next_n += 1
                changed = True
        if changed:
            logger.info("primewave contacts: backfilled %d client_id(s)",
                        sum(1 for _ in rows))
            _save_locked(rows)
    return rows


def _save_locked(rows: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"contacts": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(_PATH)


# ---- Public API ------------------------------------------------------------

def list_contacts() -> list[dict]:
    """Return all stored contacts (newest-first by updated_at)."""
    with _LOCK:
        rows = _load_locked()
    return sorted(rows, key=lambda r: r.get("updated_at") or 0, reverse=True)


def get_contact(contact_id: str) -> Optional[dict]:
    with _LOCK:
        for r in _load_locked():
            if r.get("id") == contact_id:
                return r
    return None


def upsert_contact(payload: dict, contact_id: Optional[str] = None) -> dict:
    """Create a new contact if contact_id is None, else patch the
    existing one. Returns the resulting row."""
    fields = {k: str(payload.get(k) or "").strip() for k in FIELDS}
    with _LOCK:
        rows = _load_locked()
        now = _now()
        if contact_id:
            for r in rows:
                if r.get("id") == contact_id:
                    for k, v in fields.items():
                        if v != "":
                            r[k] = v
                    r["updated_at"] = now
                    _save_locked(rows)
                    return dict(r)
            # contact_id supplied but not found — fall through to create.
        # Allocate the next unused PW-### for the new row.
        used = {r.get("client_id") for r in rows if r.get("client_id")}
        n = max([_client_id_seq(c) for c in used] + [0]) + 1
        while _format_client_id(n) in used:
            n += 1
        row = {
            "id":         uuid.uuid4().hex[:10],
            "client_id":  _format_client_id(n),
            **fields,
            "created_at": now,
            "updated_at": now,
        }
        rows.append(row)
        _save_locked(rows)
        return dict(row)


def delete_contact(contact_id: str) -> bool:
    with _LOCK:
        rows = _load_locked()
        kept = [r for r in rows if r.get("id") != contact_id]
        if len(kept) == len(rows):
            return False
        _save_locked(kept)
        return True


def reset_contacts() -> list[dict]:
    """Wipe and re-seed with the built-in 20-row Riyadh dummy set."""
    with _LOCK:
        rows = _seed_rows()
        _save_locked(rows)
    return rows
