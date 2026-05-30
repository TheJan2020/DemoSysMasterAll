"""
Restaurant Live Agent — function-call tools + data snapshot.

Tools exposed to Gemini Live, all tightly bound to the restaurant
demo's on-disk data layer:

  - list_menu_categories()
      List F&B menu categories so the agent knows what's on offer.
  - list_menu_items(category_id?, search?, max_results?)
      Return menu items (with prices, descriptions) the caller can
      order — never invent dishes or prices.
  - list_catering_packages(event_type?, max_guests?)
      Return catering packages for events (weddings, corporate, etc.).
  - list_available_tables(date, time, party_size?, floor?)
      Show tables that are NOT booked at that (date, time) and have
      enough seats. Backed by reservations.json + layout.json.
  - create_reservation(guest_name, guest_phone, date, time, party_size,
                        table_number?, notes?)
      Book a table. table_number is optional — if omitted we pick the
      smallest available table that fits party_size.
  - lookup_catering_order(order_id? | client_phone?)
      Find the caller's catering order so we can follow up / adjust.
  - update_catering_order(order_id, event_date?, event_time?,
                            items?, guests?, notes?)
      Adjust an existing catering order. `items` REPLACES the line
      items (so the agent should read the order back first and only
      mutate what the caller asked to change).
  - flag_for_supervisor(reason, severity?)
      Raise a red flag for a human supervisor (silent — don't
      mention out loud).
  - end_call(reason)
      Hang up after the caller has said goodbye.

When a tool MUTATES (create_reservation, update_catering_order), the
caller-side `tool_mutation` broadcast in live_agent.py picks it up and
fans it out to any connected WS dashboards.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from google.genai import types

from . import catering_orders as catering_mod
from . import catering_packages as catering_pkg_mod
from . import layout as layout_mod
from . import menu as menu_mod
from . import reservations as reservations_mod

logger = logging.getLogger("restaurant_agent_tools")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_SNAPSHOT_PATH = _DATA_DIR / "snapshot.json"

# Tables count as "blocked" by any reservation whose start time falls
# within this window of the requested time (in minutes either side).
RES_OVERLAP_WINDOW_MIN = 90

# Cap list_menu_items output so the agent's context doesn't blow up
# on big menus.
MENU_ITEMS_MAX = 60


# ============================================================================
# Snapshot read/write — small shared blob the live_agent uses to remember
# the caller name/phone across the call. Kept compatible with the original
# clinic shape so router.py and live_agent.py keep working unchanged.
# ============================================================================

def _empty_snapshot() -> dict:
    return {
        "caller_name":  "",
        "caller_phone": "",
        "updated_at":   None,
        # Legacy keys preserved so router.py search code that still
        # touches them doesn't KeyError. Always empty for restaurant.
        "patients":      [],
        "appointments":  [],
        "clinics":       [],
        "providers":     [],
        "slot_overrides": [],
    }


def load_snapshot() -> dict:
    if not _SNAPSHOT_PATH.exists():
        return _empty_snapshot()
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("snapshot.json corrupt — returning empty")
        return _empty_snapshot()


def save_snapshot(snap: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap["updated_at"] = datetime.utcnow().isoformat() + "Z"
    tmp = _SNAPSHOT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    tmp.replace(_SNAPSHOT_PATH)


# ============================================================================
# Phone helpers — copied from the clinic version so existing snapshot
# matching (caller phone vs catering client_phone) keeps working.
# ============================================================================

_DIGITS = re.compile(r"\D+")


def _normalize_phone(s: str) -> str:
    return _DIGITS.sub("", s or "")


def _phones_equal(a: str, b: str) -> bool:
    na, nb = _normalize_phone(a), _normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Saudi (or any) country-code-prefixed number → match last 9 digits.
    if len(na) >= 9 and len(nb) >= 9 and na[-9:] == nb[-9:]:
        return True
    return False


# ============================================================================
# Tool declarations for Gemini Live
# ============================================================================

def build_tools() -> list[types.Tool]:
    """Single Tool with every function declaration the restaurant agent
    can call. Stays in the same shape as the clinic version so
    live_agent.py's session loop doesn't need to know which vertical
    it's serving."""
    decls: list[types.FunctionDeclaration] = [
        types.FunctionDeclaration(
            name="list_menu_categories",
            description=(
                "List the F&B menu categories (Cold Mezze, Hot Mezze, "
                "Salads, Grills, Mains, Desserts, Beverages …). Use this "
                "to navigate the caller into a section before listing "
                "individual items. Returns id + EN/AR names + sort order."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT, properties={},
            ),
        ),
        types.FunctionDeclaration(
            name="list_menu_items",
            description=(
                "List menu items. Filter by category_id and/or a free-text "
                "search across EN/AR names + descriptions. ALWAYS call "
                "this before claiming a dish exists or quoting a price — "
                "never invent menu data. Returns id, category, EN/AR "
                "name + description, price, currency, and active flag."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "category_id": types.Schema(
                        type=types.Type.STRING,
                        description="Optional — restrict to one category id "
                                    "(e.g. 'CAT-COLD'). Run list_menu_categories "
                                    "first if unsure.",
                    ),
                    "search": types.Schema(
                        type=types.Type.STRING,
                        description="Optional fuzzy substring match against "
                                    "EN name, AR name, EN description, AR "
                                    "description. Case-insensitive.",
                    ),
                    "max_results": types.Schema(
                        type=types.Type.INTEGER,
                        description=f"Optional cap (default {MENU_ITEMS_MAX}). "
                                    "Helps keep responses short.",
                    ),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="list_catering_packages",
            description=(
                "List catering packages we offer. Filter by event_type "
                "(wedding, corporate, birthday, conference, ramadan_iftar, "
                "graduation, family_gathering, other) and/or expected "
                "guest count. Returns id, EN/AR name, description, event "
                "type, service style (buffet/plated/family_style), guest "
                "range, per-guest price, tier, and an items summary."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "event_type": types.Schema(
                        type=types.Type.STRING,
                        description="Optional. Use the exact tag list above.",
                    ),
                    "max_guests": types.Schema(
                        type=types.Type.INTEGER,
                        description="Optional. Only return packages whose "
                                    "guest range includes this count.",
                    ),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="list_available_tables",
            description=(
                "Show tables that are free for booking on a given date + "
                "time. Filters: party_size (table must have at least that "
                "many seats), floor ('indoor' / 'outdoor' / a specific "
                "floor id). A table is considered taken if any existing "
                f"reservation starts within {RES_OVERLAP_WINDOW_MIN} min "
                "of the requested time. Returns table id, number, seats, "
                "floor, and shape."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "date":       types.Schema(type=types.Type.STRING,
                                                description="YYYY-MM-DD. Reservations "
                                                            "are only allowed today or "
                                                            "tomorrow."),
                    "time":       types.Schema(type=types.Type.STRING,
                                                description="HH:MM (24-hour, restaurant local)."),
                    "party_size": types.Schema(type=types.Type.INTEGER,
                                                description="Optional. Minimum seats required."),
                    "floor":      types.Schema(type=types.Type.STRING,
                                                description="Optional. 'indoor', 'outdoor', "
                                                            "or a specific floor id like 'majlis'."),
                },
                required=["date", "time"],
            ),
        ),
        types.FunctionDeclaration(
            name="create_reservation",
            description=(
                "Book a table. If table_number is omitted, the smallest "
                "available table that fits party_size is auto-picked. "
                "Returns the new reservation id + table_number assigned. "
                "ALWAYS confirm the slot with the caller BEFORE calling — "
                "read the date, time, party size, and table number back "
                "in their language."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "guest_name":   types.Schema(type=types.Type.STRING),
                    "guest_phone":  types.Schema(type=types.Type.STRING,
                                                  description="Saudi mobile in any shape; "
                                                              "stored as the caller gave it."),
                    "date":         types.Schema(type=types.Type.STRING,
                                                  description="YYYY-MM-DD (today or tomorrow only)."),
                    "time":         types.Schema(type=types.Type.STRING,
                                                  description="HH:MM (24-hour)."),
                    "party_size":   types.Schema(type=types.Type.INTEGER),
                    "table_number": types.Schema(type=types.Type.STRING,
                                                  description="Optional explicit table number."),
                    "notes":        types.Schema(type=types.Type.STRING,
                                                  description="Optional — special requests, "
                                                              "seating preference, etc."),
                },
                required=["guest_name", "date", "time", "party_size"],
            ),
        ),
        types.FunctionDeclaration(
            name="lookup_catering_order",
            description=(
                "Find a catering order by id (e.g. 'CAT-0007') or by the "
                "client's phone. ALWAYS call this BEFORE update_catering_order "
                "so you have the real order_id, current items, and current "
                "event_date/event_time. Returns one order or null."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "order_id":     types.Schema(type=types.Type.STRING,
                                                  description="Format CAT-NNNN."),
                    "client_phone": types.Schema(type=types.Type.STRING,
                                                  description="Saudi mobile in any shape — "
                                                              "matches loosely on the last 9 digits."),
                },
            ),
        ),
        types.FunctionDeclaration(
            name="update_catering_order",
            description=(
                "Adjust an existing catering order — change items, event "
                "date/time, guest count, or notes. `items` REPLACES the "
                "line-item list (it's not an append) — so read the order "
                "back first (lookup_catering_order) and pass the FULL "
                "intended item list. Any field omitted is left unchanged. "
                "Returns the updated order."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "order_id":   types.Schema(type=types.Type.STRING),
                    "event_date": types.Schema(type=types.Type.STRING,
                                                description="YYYY-MM-DD."),
                    "event_time": types.Schema(type=types.Type.STRING,
                                                description="HH:MM."),
                    "guests":     types.Schema(type=types.Type.INTEGER),
                    "items": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "name_en":    types.Schema(type=types.Type.STRING),
                                "name_ar":    types.Schema(type=types.Type.STRING),
                                "qty":        types.Schema(type=types.Type.INTEGER),
                                "unit_price": types.Schema(type=types.Type.NUMBER),
                            },
                        ),
                        description="Full replacement list. Omit to leave items unchanged.",
                    ),
                    "notes":      types.Schema(type=types.Type.STRING),
                },
                required=["order_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="flag_for_supervisor",
            description=(
                "Raise a red flag for a human supervisor to take over this "
                "call. The Call Center Dashboard surfaces the flagged row "
                "in red with the reason you provide. Use this when the "
                "caller is angry, frustrated, asks for a manager / human, "
                "or whenever continuing alone would only make things "
                "worse. KEEP TALKING after calling this tool — do NOT "
                "mention 'I'm flagging this' out loud. A supervisor will "
                "join silently or take over."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "reason":   types.Schema(type=types.Type.STRING),
                    "severity": types.Schema(
                        type=types.Type.STRING,
                        description="'high' = drop everything (angry, complaint); "
                                    "'normal' = check when free. Default 'normal'.",
                    ),
                },
                required=["reason"],
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
# Tool dispatch
# ============================================================================

def execute_tool(name: str, args: dict, ctx: dict) -> dict:
    """Dispatch a Gemini function call. `ctx` is the call's context dict —
    we use it to set `end_requested` for end_call and to expose the
    supervisor-flag setter."""
    try:
        compact_args = {
            k: (v if not isinstance(v, str) or len(v) < 80 else f"{v[:77]}…")
            for k, v in args.items()
        }
        logger.info("dispatch %s args=%s call_id=%s",
                     name, compact_args, ctx.get("call_id"))
    except Exception:
        pass
    result = _execute_tool_inner(name, args, ctx)
    try:
        if isinstance(result, dict):
            if result.get("error"):
                logger.warning("result %s ERROR: %s", name, result.get("error"))
            else:
                summary = {k: result[k] for k in
                            ("ok", "found", "id", "reservation_id", "order_id",
                              "count", "table_number", "currency")
                            if k in result}
                for k in ("items", "categories", "packages", "tables"):
                    if k in result and isinstance(result[k], list):
                        summary[f"{k}_count"] = len(result[k])
                logger.info("result %s ok summary=%s", name, summary)
    except Exception:
        pass
    return result


def _execute_tool_inner(name: str, args: dict, ctx: dict) -> dict:
    try:
        if name == "list_menu_categories":
            return _t_list_menu_categories()
        if name == "list_menu_items":
            return _t_list_menu_items(
                args.get("category_id"),
                args.get("search"),
                args.get("max_results"),
            )
        if name == "list_catering_packages":
            return _t_list_catering_packages(
                args.get("event_type"),
                args.get("max_guests"),
            )
        if name == "list_available_tables":
            return _t_list_available_tables(
                args.get("date") or "",
                args.get("time") or "",
                args.get("party_size"),
                args.get("floor"),
            )
        if name == "create_reservation":
            return _t_create_reservation(args, ctx)
        if name == "lookup_catering_order":
            return _t_lookup_catering_order(
                args.get("order_id"),
                args.get("client_phone"),
            )
        if name == "update_catering_order":
            return _t_update_catering_order(args, ctx)
        if name == "flag_for_supervisor":
            return _t_flag_for_supervisor(args, ctx)
        if name == "end_call":
            return _t_end_call(args.get("reason") or "", ctx)
        return {"error": f"unknown tool: {name}"}
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


# ============================================================================
# Menu
# ============================================================================

def _t_list_menu_categories() -> dict:
    menu = menu_mod.load_menu()
    out = []
    for c in (menu.get("categories") or []):
        if c.get("active") is False:
            continue
        out.append({
            "category_id": c.get("id"),
            "name_en":     c.get("name_en"),
            "name_ar":     c.get("name_ar"),
            "sort_order":  c.get("sort_order"),
        })
    out.sort(key=lambda r: r.get("sort_order") or 99)
    return {"ok": True, "count": len(out), "categories": out}


def _t_list_menu_items(category_id: Optional[str],
                        search: Optional[str],
                        max_results: Optional[int]) -> dict:
    menu = menu_mod.load_menu()
    items = list(menu.get("items") or [])
    if category_id:
        items = [i for i in items if i.get("category_id") == category_id]
    q = (search or "").strip().lower()
    if q:
        def hit(i: dict) -> bool:
            for k in ("name_en", "name_ar", "description_en", "description_ar"):
                v = (i.get(k) or "").lower()
                if q in v:
                    return True
            return False
        items = [i for i in items if hit(i)]
    items = [i for i in items if i.get("active") is not False]
    items.sort(key=lambda r: (r.get("category_id") or "", r.get("name_en") or ""))
    cap = max(1, min(MENU_ITEMS_MAX, int(max_results or MENU_ITEMS_MAX)))
    capped = items[:cap]
    out = [{
        "item_id":        i.get("id"),
        "category_id":    i.get("category_id"),
        "name_en":        i.get("name_en"),
        "name_ar":        i.get("name_ar"),
        "description_en": i.get("description_en"),
        "description_ar": i.get("description_ar"),
        "price":          i.get("price"),
        "currency":       i.get("currency") or "SAR",
    } for i in capped]
    return {
        "ok":          True,
        "count":       len(out),
        "total_match": len(items),
        "items":       out,
        "truncated":   len(items) > cap,
    }


# ============================================================================
# Catering packages
# ============================================================================

def _t_list_catering_packages(event_type: Optional[str],
                                 max_guests: Optional[int]) -> dict:
    pkgs = list(catering_pkg_mod.load_packages() or [])
    pkgs = [p for p in pkgs if p.get("active") is not False]
    et = (event_type or "").strip().lower() or None
    if et:
        pkgs = [p for p in pkgs if (p.get("event_type") or "") == et]
    if max_guests is not None:
        try:
            mg = int(max_guests)
            pkgs = [p for p in pkgs
                     if int(p.get("min_guests") or 1) <= mg <= int(p.get("max_guests") or 99999)]
        except Exception:
            pass
    pkgs.sort(key=lambda r: (r.get("sort_order") or 99, r.get("per_guest_price") or 0))
    out = [{
        "package_id":      p.get("id"),
        "code":            p.get("code"),
        "name_en":         p.get("name_en"),
        "name_ar":         p.get("name_ar"),
        "description_en":  p.get("description_en"),
        "description_ar":  p.get("description_ar"),
        "event_type":      p.get("event_type"),
        "service_style":   p.get("service_style"),
        "min_guests":      p.get("min_guests"),
        "max_guests":      p.get("max_guests"),
        "per_guest_price": p.get("per_guest_price"),
        "currency":        p.get("currency") or "SAR",
        "tier":            p.get("tier"),
        "popular":         p.get("popular"),
        "item_count":      len(p.get("items") or []),
    } for p in pkgs]
    return {"ok": True, "count": len(out), "packages": out}


# ============================================================================
# Reservations — availability + create
# ============================================================================

def _hhmm_to_minutes(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", s):
        return None
    h, m = s.split(":")
    h_i, m_i = int(h), int(m)
    if not (0 <= h_i < 24 and 0 <= m_i < 60):
        return None
    return h_i * 60 + m_i


def _table_is_blocked(table_number: str, date: str, time_min: int,
                       reservations: list[dict]) -> bool:
    """A table is blocked if any reservation for the same (date, table_number)
    starts within RES_OVERLAP_WINDOW_MIN of the requested time."""
    for r in reservations:
        if (r.get("date") or "") != date:
            continue
        if (r.get("table_number") or "").upper() != table_number.upper():
            continue
        if (r.get("status") or "").lower() not in ("pending", "seated"):
            continue
        rmin = _hhmm_to_minutes(r.get("time") or "")
        if rmin is None:
            continue
        if abs(rmin - time_min) < RES_OVERLAP_WINDOW_MIN:
            return True
    return False


def _t_list_available_tables(date: str, time: str,
                              party_size: Optional[int],
                              floor: Optional[str]) -> dict:
    if not date:
        return {"error": "date is required (YYYY-MM-DD)"}
    if not time:
        return {"error": "time is required (HH:MM)"}
    tmin = _hhmm_to_minutes(time)
    if tmin is None:
        return {"error": "time must be HH:MM (24-hour)"}

    layout = layout_mod.load_layout()
    tables = list(layout.get("tables") or [])
    # Filter by floor / party size.
    fl = (floor or "").strip().lower() or None
    if fl:
        tables = [t for t in tables
                   if (t.get("floor_id") or "indoor").lower() == fl]
    if party_size is not None:
        try:
            ps = int(party_size)
            tables = [t for t in tables if int(t.get("seats") or 0) >= ps]
        except Exception:
            pass

    # Filter out tables blocked by an existing reservation.
    res_for_date = reservations_mod.reservations_for(date)
    free = [t for t in tables
             if not _table_is_blocked(t.get("number") or "", date, tmin, res_for_date)]

    # Sort: smallest fitting table first (don't seat a 2-top at a 12-top).
    free.sort(key=lambda t: (int(t.get("seats") or 0), t.get("number") or ""))
    out = [{
        "table_id":   t.get("id"),
        "number":     t.get("number"),
        "seats":      t.get("seats"),
        "floor_id":   t.get("floor_id") or "indoor",
        "shape":      t.get("shape"),
    } for t in free]
    return {"ok": True, "count": len(out), "tables": out,
            "date": date, "time": time}


def _t_create_reservation(args: dict, ctx: dict) -> dict:
    guest_name  = (args.get("guest_name") or "").strip()
    guest_phone = (args.get("guest_phone") or "").strip()
    date        = (args.get("date") or "").strip()
    time_s      = (args.get("time") or "").strip()
    try:
        party_size = int(args.get("party_size") or 0)
    except Exception:
        party_size = 0
    table_number = (args.get("table_number") or "").strip()
    notes        = (args.get("notes") or "").strip()

    if not guest_name:
        return {"error": "guest_name is required"}
    if party_size < 1:
        return {"error": "party_size must be at least 1"}
    if _hhmm_to_minutes(time_s) is None:
        return {"error": "time must be HH:MM (24-hour)"}

    # Auto-pick a table if the caller didn't specify one.
    if not table_number:
        availability = _t_list_available_tables(date, time_s, party_size, None)
        if "error" in availability:
            return availability
        tables = availability.get("tables") or []
        if not tables:
            return {"error": "no free tables for that slot — try another time"}
        table_number = tables[0].get("number") or ""

    patch = {
        "guest_name":   guest_name,
        "guest_phone":  guest_phone,
        "date":         date,
        "time":         time_s,
        "duration_min": 90,
        "party_size":   party_size,
        "table_number": table_number,
        "notes":        notes,
        "status":       "pending",
    }
    try:
        res = reservations_mod.add_reservation(patch)
    except reservations_mod._Refused as e:
        return {"error": str(e)}

    # Broadcast mutation so any live dashboard refreshes.
    bc = ctx.get("broadcast")
    if callable(bc):
        try:
            bc({"type": "tool_mutation", "what": "reservation_created",
                 "reservation_id": res.get("id"), "table_number": res.get("table_number")})
        except Exception:
            logger.exception("broadcast tool_mutation failed")

    return {
        "ok":             True,
        "reservation_id": res.get("id"),
        "table_number":   res.get("table_number"),
        "date":           res.get("date"),
        "time":           res.get("time"),
        "party_size":     res.get("party_size"),
        "guest_name":     res.get("guest_name"),
    }


# ============================================================================
# Catering orders — lookup + update
# ============================================================================

def _order_for_agent(o: dict) -> dict:
    """Trim a catering order to fields the agent should see / read back."""
    return {
        "order_id":       o.get("id"),
        "client_name":    o.get("client_name"),
        "client_phone":   o.get("client_phone"),
        "event_date":     o.get("event_date"),
        "event_time":     o.get("event_time"),
        "event_type":     o.get("event_type"),
        "package":        o.get("package"),
        "venue":          o.get("venue"),
        "guests":         o.get("guests"),
        "items":          [{
            "name_en":    i.get("name_en"),
            "name_ar":    i.get("name_ar"),
            "qty":        i.get("qty"),
            "unit_price": i.get("unit_price"),
        } for i in (o.get("items") or [])],
        "subtotal":       o.get("subtotal"),
        "vat":            o.get("vat"),
        "delivery_fee":   o.get("delivery_fee"),
        "total":          o.get("total"),
        "currency":       o.get("currency") or "SAR",
        "status":         o.get("status"),
        "payment_status": o.get("payment_status"),
        "notes":          o.get("notes"),
    }


def _t_lookup_catering_order(order_id: Optional[str],
                                client_phone: Optional[str]) -> dict:
    oid   = (order_id or "").strip().upper()
    phone = (client_phone or "").strip()
    if not oid and not phone:
        return {"error": "either order_id or client_phone is required"}
    orders = catering_mod.load_orders() or []
    if oid:
        for o in orders:
            if (o.get("id") or "").upper() == oid:
                return {"found": True, "order": _order_for_agent(o)}
        return {"found": False, "order": None}
    # Phone lookup — return the most recent match (newest first).
    matches = [o for o in orders if _phones_equal(o.get("client_phone") or "", phone)]
    if not matches:
        return {"found": False, "order": None}
    matches.sort(key=lambda o: int(o.get("created_at") or 0), reverse=True)
    return {
        "found":      True,
        "match_count": len(matches),
        "order":      _order_for_agent(matches[0]),
    }


def _t_update_catering_order(args: dict, ctx: dict) -> dict:
    order_id = (args.get("order_id") or "").strip()
    if not order_id:
        return {"error": "order_id is required"}

    patch: dict = {}
    if args.get("event_date") is not None:
        patch["event_date"] = (args.get("event_date") or "").strip()
    if args.get("event_time") is not None:
        patch["event_time"] = (args.get("event_time") or "").strip()
    if args.get("guests") is not None:
        try:
            patch["guests"] = max(0, int(args["guests"]))
        except Exception:
            pass
    if args.get("notes") is not None:
        patch["notes"] = (args.get("notes") or "").strip()
    if "items" in args and isinstance(args["items"], list):
        clean_items = []
        for it in args["items"]:
            if not isinstance(it, dict):
                continue
            clean_items.append({
                "name_en":    (it.get("name_en") or "").strip(),
                "name_ar":    (it.get("name_ar") or "").strip(),
                "qty":        max(0, int(it.get("qty") or 0)),
                "unit_price": round(max(0.0, float(it.get("unit_price") or 0)), 2),
            })
        patch["items"] = clean_items

    if not patch:
        return {"error": "no fields to update — pass at least one of "
                          "event_date, event_time, guests, items, notes"}

    try:
        updated = catering_mod.update_order(order_id, patch)
    except catering_mod._Refused as e:
        return {"error": str(e)}
    if not updated:
        return {"error": f"order {order_id} not found"}

    bc = ctx.get("broadcast")
    if callable(bc):
        try:
            bc({"type": "tool_mutation", "what": "catering_order_updated",
                 "order_id": order_id, "fields": list(patch.keys())})
        except Exception:
            logger.exception("broadcast tool_mutation failed")

    return {"ok": True, "order": _order_for_agent(updated),
            "changed_fields": list(patch.keys())}


# ============================================================================
# Supervisor + end-call — preserved verbatim from the clinic version so the
# CallSession's `set_flag` / `end_requested` hooks keep working.
# ============================================================================

def _t_flag_for_supervisor(args: dict, ctx: dict) -> dict:
    reason   = (args.get("reason") or "").strip() or "(no reason given)"
    severity = (args.get("severity") or "").strip().lower()
    if severity not in ("low", "normal", "high"):
        severity = "normal"
    set_flag = ctx.get("set_flag")
    if not callable(set_flag):
        logger.warning("call %s: flag_for_supervisor invoked but ctx[set_flag] missing",
                        ctx.get("call_id"))
        return {"error": "flag handler not wired"}
    set_flag({"reason": reason, "severity": severity, "source": "agent"})
    logger.info("call %s: supervisor flag raised (%s) — %s",
                 ctx.get("call_id"), severity, reason)
    return {"ok": True, "reason": reason, "severity": severity}


def _t_end_call(reason: str, ctx: dict) -> dict:
    ctx["end_requested"] = True
    logger.info("call %s: end_call requested — reason: %s",
                 ctx.get("call_id"), reason or "(none)")
    return {"ok": True, "reason": reason}
