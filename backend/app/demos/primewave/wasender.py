"""
Minimal WasenderApi client (Primewave demo).

Identical wire format to the restaurant demo's `wasender.py` — copied
here so the primewave module is self-contained and doesn't reach into
another demo for backend code. The implementation is ~60 lines of
httpx and stateless, so the duplication cost is small.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("demo_primewave.wasender")

WASENDER_BASE_URL = "https://www.wasenderapi.com/api"
_DIGITS = re.compile(r"\D+")


@dataclass
class WasenderCredentials:
    """Two Bearer tokens — Wasender splits auth by endpoint scope.

    api_key:           per-session token (POST /send-message, /contacts).
    personal_token:    account-level PAT (/whatsapp-sessions/{id}/* logs).
    """
    api_key:        str
    personal_token: str = ""

    def can_send(self) -> bool:
        return bool(self.api_key)

    def can_read_inbox(self) -> bool:
        return bool(self.personal_token) or bool(self.api_key)


def normalize_phone(s: str) -> str:
    """Digits-only with country code, no '+'. Handles Saudi national
    formats by prepending 966 when needed."""
    d = _DIGITS.sub("", s or "")
    if not d:
        return ""
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 10 and d.startswith("05"):
        d = "966" + d[1:]
    if len(d) == 9 and d.startswith("5"):
        d = "966" + d
    return d


class WasenderClient:
    def __init__(self, creds: WasenderCredentials, *, timeout_s: float = 10.0):
        self.creds = creds
        self.timeout = timeout_s

    def _headers(self, *, prefer: str = "api_key") -> dict[str, str]:
        if prefer == "personal":
            token = self.creds.personal_token or self.creds.api_key
        else:
            token = self.creds.api_key or self.creds.personal_token
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    async def send_text(self, to: str, text: str) -> dict:
        if not self.creds.can_send():
            return {"ok": False, "status": 0, "error": "WhatsApp API key not configured"}
        phone = normalize_phone(to)
        if not phone:
            return {"ok": False, "status": 0,
                    "error": "Phone number could not be parsed — use digits with country code"}
        body = {"to": phone, "messageType": "text", "text": text}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{WASENDER_BASE_URL}/send-message",
                    headers=self._headers(prefer="api_key"),
                    json=body,
                )
        except httpx.RequestError as e:
            logger.warning("WasenderApi send_text network error: %s", e)
            return {"ok": False, "status": 0, "error": f"network: {e}"}

        try:
            payload = r.json()
        except Exception:
            payload = {"raw_text": (r.text or "")[:500]}

        if 200 <= r.status_code < 300:
            data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
            return {
                "ok":         True,
                "status":     r.status_code,
                "message_id": data.get("message_id"),
                "to":         phone,
                "raw":        payload,
            }
        err = (
            (isinstance(payload, dict) and (
                payload.get("error")
                or payload.get("message")
                or payload.get("detail")
            ))
            or f"HTTP {r.status_code}"
        )
        return {"ok": False, "status": r.status_code, "error": err, "raw": payload}

    async def list_messages(self, session_id: str, limit: int = 200) -> dict:
        if not self.creds.can_read_inbox():
            return {"ok": False, "items": [],
                    "error": "Neither API key nor Personal Access Token configured"}
        if not session_id:
            return {"ok": False, "items": [],
                    "error": "WhatsApp session ID not configured"}
        params = {"limit": str(max(1, min(500, int(limit))))}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(
                    f"{WASENDER_BASE_URL}/whatsapp-sessions/{session_id}/message-logs",
                    headers=self._headers(prefer="personal"),
                    params=params,
                )
        except httpx.RequestError as e:
            return {"ok": False, "items": [], "error": f"network: {e}"}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        if not (200 <= r.status_code < 300):
            err = (
                (isinstance(payload, dict) and (payload.get("message") or payload.get("error")))
                or f"HTTP {r.status_code}"
            )
            return {"ok": False, "items": [], "error": err, "status": r.status_code}
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = (
                payload.get("data")
                or payload.get("messages")
                or payload.get("items")
                or []
            )
            if isinstance(items, dict):
                items = items.get("data") or items.get("items") or []
        else:
            items = []
        return {"ok": True, "items": items, "raw": payload}

    async def ping(self) -> dict:
        """Cheap key-validity check. Hits /contacts (smallest endpoint
        that requires a valid api_key). Returns
        {ok, status, contact_count?, error?}."""
        if not self.creds.can_send():
            return {"ok": False, "status": 0, "error": "API key not configured"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(
                    f"{WASENDER_BASE_URL}/contacts",
                    headers=self._headers(prefer="api_key"),
                )
        except httpx.RequestError as e:
            return {"ok": False, "status": 0, "error": f"network: {e}"}
        if 200 <= r.status_code < 300:
            try:
                p = r.json()
                count = len(p.get("data") or p.get("contacts") or p) \
                    if isinstance(p, (list, dict)) else None
            except Exception:
                count = None
            return {"ok": True, "status": r.status_code, "contact_count": count}
        try:
            err = r.json().get("message") or r.json().get("error") or f"HTTP {r.status_code}"
        except Exception:
            err = f"HTTP {r.status_code}"
        return {"ok": False, "status": r.status_code, "error": err}


def build_client(api_key: Optional[str],
                 personal_token: Optional[str] = None) -> WasenderClient:
    return WasenderClient(WasenderCredentials(
        api_key=(api_key or "").strip(),
        personal_token=(personal_token or "").strip(),
    ))
