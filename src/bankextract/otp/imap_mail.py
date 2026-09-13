"""Lecture automatique de l'OTP dans une boîte e-mail (IMAP).

Utile pour les banques qui envoient le code par courriel plutôt que par SMS.
"""

from __future__ import annotations

import email
import imaplib
import os
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from .base import OtpProvider, extract_code


class ImapOtpProvider(OtpProvider):
    name = "imap"

    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        host = self.options.get("host")
        user = self.options.get("user") or os.getenv(self.options.get("user_env", ""))
        password = os.getenv(self.options.get("password_env", "")) or self.options.get("password")
        if not (host and user and password):
            raise ValueError(
                "Fournisseur IMAP : « host », « user »/« user_env » et « password_env » "
                "sont obligatoires."
            )

        since = since or datetime.now(timezone.utc)
        mailbox = self.options.get("mailbox", "INBOX")
        sender_filter = str(self.options.get("from", "")).lower()
        pattern = self.options.get("code_pattern") or r"\b(\d{4,8})\b"
        port = int(self.options.get("port", 993))

        def fetch(_since: datetime) -> str | None:
            with imaplib.IMAP4_SSL(host, port) as client:
                client.login(user, password)
                client.select(mailbox)
                # IMAP filtre à la journée : on affine ensuite sur l'horodatage réel.
                criteria = ["SINCE", (_since - timedelta(days=1)).strftime("%d-%b-%Y")]
                if sender_filter:
                    criteria += ["FROM", sender_filter]
                status, data = client.search(None, *criteria)
                if status != "OK" or not data or not data[0]:
                    return None
                for uid in reversed(data[0].split()):
                    code = self._read_message(client, uid, _since, pattern)
                    if code:
                        return code
            return None

        return self._poll(fetch, since, hint or "code par e-mail")

    def _read_message(self, client, uid: bytes, since: datetime, pattern: str) -> str | None:
        status, payload = client.fetch(uid, "(RFC822)")
        if status != "OK" or not payload or not isinstance(payload[0], tuple):
            return None
        message = email.message_from_bytes(payload[0][1])

        received = email.utils.parsedate_to_datetime(message.get("Date", ""))
        if received:
            if not received.tzinfo:
                received = received.replace(tzinfo=timezone.utc)
            if received < since - timedelta(seconds=60):
                return None

        subject = str(make_header(decode_header(message.get("Subject", ""))))
        return extract_code(f"{subject}\n{_body(message)}", pattern)


def _body(message) -> str:
    """Concatène les parties texte d'un message, multipart compris."""
    if not message.is_multipart():
        return _decode(message)
    return "\n".join(
        _decode(part)
        for part in message.walk()
        if part.get_content_type() in ("text/plain", "text/html")
    )


def _decode(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")
