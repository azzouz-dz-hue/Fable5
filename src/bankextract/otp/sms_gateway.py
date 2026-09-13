"""Lecture automatique de l'OTP via une passerelle SMS HTTP.

Compatible avec les applications Android qui exposent les SMS reçus en JSON
(SMS Gateway for Android, SMSSync, Termux:API derrière un petit serveur…).
La passerelle n'a pas besoin d'un format précis : `messages_path`, `text_field`
et `date_field` décrivent où lire, quel que soit le fournisseur.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import OtpProvider, extract_code


class SmsGatewayOtpProvider(OtpProvider):
    """Interroge périodiquement une passerelle SMS jusqu'à trouver le code."""

    name = "sms_gateway"

    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        url = self.options.get("url")
        if not url:
            raise ValueError(
                "Passerelle SMS : « url » est obligatoire "
                "(ex. http://192.168.1.20:8080/messages)."
            )
        since = since or datetime.now(timezone.utc)
        # Petite marge : un SMS peut être horodaté juste avant notre appel.
        since = since - timedelta(seconds=int(self.options.get("lookback_seconds", 30)))

        sender_filter = str(self.options.get("sender", "")).upper()
        pattern = self.options.get("code_pattern") or r"\b(\d{4,8})\b"
        timeout = float(self.options.get("request_timeout", 10))

        with httpx.Client(timeout=timeout, headers=self._headers(), verify=True) as client:

            def fetch(_since: datetime) -> str | None:
                response = client.get(url, params=self.options.get("params") or None)
                response.raise_for_status()
                for message in self._messages(response.json()):
                    code = self._match(message, _since, sender_filter, pattern)
                    if code:
                        return code
                return None

            return self._poll(fetch, since, hint or "SMS bancaire")

    def _headers(self) -> dict[str, str]:
        headers = dict(self.options.get("headers") or {})
        token_env = self.options.get("token_env")
        if token_env and (token := os.getenv(token_env)):
            headers.setdefault("Authorization", f"Bearer {token}")
        return headers

    def _messages(self, payload: Any) -> list[dict]:
        """Extrait la liste des SMS, que la passerelle renvoie une liste ou un objet."""
        path = self.options.get("messages_path")
        node = payload
        if path:
            for key in str(path).split("."):
                if not isinstance(node, dict):
                    return []
                node = node.get(key, [])
        if isinstance(node, dict):
            node = node.get("messages", node.get("data", []))
        return [m for m in node if isinstance(m, dict)] if isinstance(node, list) else []

    def _match(
        self, message: dict, since: datetime, sender_filter: str, pattern: str
    ) -> str | None:
        text_field = self.options.get("text_field", "body")
        sender_field = self.options.get("sender_field", "address")
        date_field = self.options.get("date_field", "date")

        text = str(message.get(text_field) or message.get("message") or "")
        if not text:
            return None

        if sender_filter:
            sender = str(message.get(sender_field) or "").upper()
            if sender_filter not in sender:
                return None

        received = _parse_timestamp(message.get(date_field))
        if received and received < since:
            return None  # SMS antérieur à la demande : ce n'est pas notre code

        return extract_code(text, pattern)


def _parse_timestamp(value: Any) -> datetime | None:
    """Lit un horodatage en epoch (s ou ms) ou en ISO 8601, ramené en UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
