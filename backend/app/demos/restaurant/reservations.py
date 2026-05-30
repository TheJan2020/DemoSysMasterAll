"""
Restaurant reservations — today + tomorrow only.

Storage:
    data/demos/restaurant/reservations.json

Tracked by git; `.tmp` from atomic writes is gitignored.

Side-effects on the layout
- Creating a reservation flips the assigned table's status to "reserved"
  and writes a human-readable note into its `server_name` field
  ("Reservation: <guest> · <time>"), mirroring how the seed already
  represents reservations on the floor.
- Cancelling / deleting a reservation flips the table back to "free"
  IFF the table is currently "reserved" AND has no other live
  reservation pointing at it. This keeps Layout, Overview, and
  Reservations in sync without a separate sync job.

Restricted to TODAY + TOMORROW
- The add / update endpoints reject any other date so the demo stays
  bounded. The frontend offers exactly those two date chips.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import layout as layout_mod

logger = logging.getLogger("demo_restaurant.reservations")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_RES_PATH = _DATA_DIR / "reservations.json"

_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# Date helpers — "today" and "tomorrow" as YYYY-MM-DD in local time.
# ----------------------------------------------------------------------

def _today_str() -> str:
    t = time.localtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _tomorrow_str() -> str:
    t = time.localtime(time.time() + 86400)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def allowed_dates() -> list[str]:
    return [_today_str(), _tomorrow_str()]


def _is_allowed_date(d: str) -> bool:
    return d in allowed_dates()


def _is_today(d: str) -> bool:
    return d == _today_str()


# ----------------------------------------------------------------------
# Seed — a handful of reservations spread across today + tomorrow so the
# operator sees the page populated after Reset Dummy Data.
# ----------------------------------------------------------------------

def _seed_reservations() -> list[dict]:
    today, tom = _today_str(), _tomorrow_str()
    return [
        {"id": "RES-0001", "table_number": "T05", "date": today, "time": "20:00",
         "duration_min": 120, "guest_name": "Mr. Khalid Al-Otaibi",
         "guest_phone": "+966 50 123 4567", "party_size": 2,
         "notes": "Anniversary — quiet corner please.",
         "status": "pending"},
        {"id": "RES-0002", "table_number": "T13", "date": today, "time": "21:00",
         "duration_min": 120, "guest_name": "Mrs. Aisha Al-Harbi",
         "guest_phone": "+966 55 987 6543", "party_size": 4,
         "notes": "Outdoor with shade if possible.",
         "status": "pending"},
        {"id": "RES-0003", "table_number": "T06", "date": tom,   "time": "19:30",
         "duration_min": 90,  "guest_name": "Salem family",
         "guest_phone": "+966 53 222 1111", "party_size": 6,
         "notes": "Kids menu for two.",
         "status": "pending"},
        {"id": "RES-0004", "table_number": "T18", "date": tom,   "time": "21:30",
         "duration_min": 150, "guest_name": "Al-Bandar group (VIP)",
         "guest_phone": "+966 56 333 7788", "party_size": 8,
         "notes": "Pre-order Mansaf + Ouzi.",
         "status": "pending"},
    ]


def _default_payload() -> dict:
    return {"reservations": _seed_reservations()}


# ----------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------

def load_reservations() -> list[dict]:
    """Return every reservation, sorted by (date, time)."""
    with _LOCK:
        rows = _load_locked().get("reservations") or []
    rows.sort(key=lambda r: (r.get("date") or "", r.get("time") or ""))
    return rows


def reservations_for(date: Optional[str] = None) -> list[dict]:
    """Filter to a specific date string (or every allowed date when
    `date` is None)."""
    rows = load_reservations()
    if date:
        return [r for r in rows if r.get("date") == date]
    return rows


def _load_locked() -> dict:
    if not _RES_PATH.exists():
        return _default_payload()
    try:
        data = json.loads(_RES_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("reservations.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(data, dict):
        return _default_payload()
    rows = data.get("reservations")
    if not isinstance(rows, list):
        return _default_payload()
    return {"reservations": [r for r in rows if isinstance(r, dict)]}


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _RES_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(_RES_PATH)
    return payload


# ----------------------------------------------------------------------
# Layout sync
# ----------------------------------------------------------------------

def _table_by_number(number: str) -> Optional[dict]:
    """Look up a table in the live layout by its display number (T05)."""
    if not number:
        return None
    n = number.strip().upper()
    for t in (layout_mod.load_layout().get("tables") or []):
        if str(t.get("number") or "").upper() == n:
            return t
    return None


def _set_table_reserved(table_id: str, note: str) -> None:
    """Mark the layout table as 'reserved' with a human-friendly note
    in `server_name`. Mirrors the seed convention."""
    layout_mod.update_table(table_id, {
        "status":      "reserved",
        "server_name": note,
        "guests":      0,
        "ordered_items": [],
        "opened_at":   None,
    })


def _release_table_if_orphaned(table_number: str, all_rows: list[dict]) -> None:
    """If no live TODAY-reservation still points at this table number,
    flip the layout row back to 'free'. Tomorrow's reservations don't
    keep the table reserved today — they only sit on the Reservations
    page. Skips occupied tables — those were taken by walk-ins after the
    reservation was cancelled."""
    if not table_number:
        return
    t = _table_by_number(table_number)
    if not t or (t.get("status") or "") != "reserved":
        return
    other = [
        r for r in all_rows
        if (r.get("table_number") or "").upper() == table_number.upper()
        and (r.get("status") or "") in ("pending", "seated")
        and _is_today(r.get("date") or "")
    ]
    if other:
        return
    layout_mod.update_table(t["id"], {
        "status":      "free",
        "server_name": "",
    })


def _note_for(res: dict) -> str:
    return (f"Reservation: {res.get('guest_name') or '—'} · "
            f"{res.get('time') or '?'}")


# ----------------------------------------------------------------------
# ID generation + coercion
# ----------------------------------------------------------------------

def _next_id(rows: list[dict]) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith("RES-"):
            try:
                n = max(n, int(rid[4:]))
            except Exception:
                pass
    return f"RES-{n + 1:04d}"


_VALID_STATUS = {"pending", "seated", "cancelled"}


def _coerce_reservation(r: dict, existing: list[dict]) -> dict:
    status = (str(r.get("status") or "pending").strip().lower())
    if status not in _VALID_STATUS:
        status = "pending"
    party = max(1, int(r.get("party_size") or 1))
    duration = max(15, int(r.get("duration_min") or 90))
    return {
        "id":            str(r.get("id") or _next_id(existing)).strip(),
        "table_number":  str(r.get("table_number") or "").strip()[:16],
        "date":          str(r.get("date") or "").strip()[:10],
        "time":          str(r.get("time") or "").strip()[:5],
        "duration_min":  duration,
        "guest_name":    str(r.get("guest_name") or "").strip()[:200],
        "guest_phone":   str(r.get("guest_phone") or "").strip()[:32],
        "party_size":    party,
        "notes":         str(r.get("notes") or "").strip()[:500],
        "status":        status,
    }


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------

class _Refused(ValueError):
    """Raised by the validator when the request is malformed."""


def _validate(patch: dict) -> None:
    if not (patch.get("guest_name") or "").strip():
        raise _Refused("guest_name is required")
    if not (patch.get("table_number") or "").strip():
        raise _Refused("table_number is required")
    if not _is_allowed_date(patch.get("date") or ""):
        raise _Refused("date must be today or tomorrow (YYYY-MM-DD)")
    if not (patch.get("time") or "").strip():
        raise _Refused("time (HH:MM) is required")
    # Table must exist in the current layout.
    if not _table_by_number(patch.get("table_number") or ""):
        raise _Refused(f"table {patch.get('table_number')} not found in layout")


def add_reservation(patch: dict) -> dict:
    _validate(patch)
    with _LOCK:
        payload = _load_locked()
        res = _coerce_reservation(patch, payload["reservations"])
        payload["reservations"].append(res)
        _save_locked(payload)
    # Side-effect: ONLY today's reservations mark their table as
    # reserved in the layout. Tomorrow's stay free on the floor — they
    # only show up on the Reservations page until "today" rolls forward.
    t = _table_by_number(res["table_number"])
    if t and res["status"] == "pending" and _is_today(res.get("date") or ""):
        _set_table_reserved(t["id"], _note_for(res))
    return res


def update_reservation(res_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        idx = next((i for i, r in enumerate(payload["reservations"])
                    if r.get("id") == res_id), None)
        if idx is None:
            return None
        merged = {**payload["reservations"][idx],
                  **{k: v for k, v in patch.items() if v is not None}}
        merged["id"] = res_id
        if "date" in patch and not _is_allowed_date(merged.get("date") or ""):
            raise _Refused("date must be today or tomorrow (YYYY-MM-DD)")
        old_table = payload["reservations"][idx].get("table_number")
        new = _coerce_reservation(merged, payload["reservations"])
        payload["reservations"][idx] = new
        _save_locked(payload)
        rows_snapshot = list(payload["reservations"])

    # Re-sync layout. If the table changed, release the old one first.
    if old_table and old_table.upper() != (new.get("table_number") or "").upper():
        _release_table_if_orphaned(old_table, rows_snapshot)

    t = _table_by_number(new["table_number"])
    if t:
        if new["status"] == "pending" and _is_today(new.get("date") or ""):
            _set_table_reserved(t["id"], _note_for(new))
        else:
            # Either the status is no longer 'pending', or the date has
            # moved off today → release the table if no other TODAY
            # reservation still holds it.
            _release_table_if_orphaned(new["table_number"], rows_snapshot)
    return new


def delete_reservation(res_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        row = next((r for r in payload["reservations"]
                    if r.get("id") == res_id), None)
        if not row:
            return False
        payload["reservations"] = [
            r for r in payload["reservations"] if r.get("id") != res_id
        ]
        _save_locked(payload)
        rows_snapshot = list(payload["reservations"])

    _release_table_if_orphaned(row.get("table_number") or "", rows_snapshot)
    return True


def reset_reservations() -> list[dict]:
    """Restore the seed AND re-sync the layout — every seeded
    reservation flips its table to 'reserved'; non-seeded tables that
    were previously reserved by user-added reservations get released."""
    with _LOCK:
        payload = _default_payload()
        _save_locked(payload)
        rows_snapshot = list(payload["reservations"])

    # First, release any layout tables that were reserved but no
    # TODAY reservation in the new seed still backs them. Tomorrow's
    # seed entries don't keep the floor blocked today.
    for t in (layout_mod.load_layout().get("tables") or []):
        if (t.get("status") or "") != "reserved":
            continue
        num = (t.get("number") or "").upper()
        keep = any(
            (r.get("table_number") or "").upper() == num
            and (r.get("status") or "") in ("pending", "seated")
            and _is_today(r.get("date") or "")
            for r in rows_snapshot
        )
        if not keep:
            layout_mod.update_table(t["id"], {"status": "free", "server_name": ""})

    # Then, mark today's seed entries as reserved on the floor; tomorrow's
    # stay free until the date rolls forward.
    for r in rows_snapshot:
        t = _table_by_number(r.get("table_number") or "")
        if t and r.get("status") == "pending" and _is_today(r.get("date") or ""):
            _set_table_reserved(t["id"], _note_for(r))

    return load_reservations()
