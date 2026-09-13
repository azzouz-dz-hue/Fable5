"""Contrat commun aux fournisseurs d'OTP."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

DEFAULT_CODE_PATTERN = r"\b(\d{4,8})\b"


class OtpTimeout(TimeoutError):
    """Aucun code reçu dans le délai imparti."""


def extract_code(text: str, pattern: str = DEFAULT_CODE_PATTERN) -> str | None:
    """Extrait le code d'un SMS ou d'un e-mail.

    Le premier groupe capturant est renvoyé s'il existe, sinon la correspondance
    entière — un motif sur mesure par banque reste ainsi facile à écrire.
    """
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


class OtpProvider(ABC):
    """Source d'un code à usage unique."""

    name: str = "base"

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        timeout_seconds: int = 180,
        poll_interval_seconds: float = 3.0,
    ) -> None:
        self.options = options or {}
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @property
    def automatic(self) -> bool:
        """Vrai si aucune intervention humaine n'est nécessaire."""
        return True

    @abstractmethod
    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        """Renvoie le code reçu après `since`, ou lève `OtpTimeout`."""

    def _poll(self, fetch, since: datetime, hint: str) -> str:
        """Boucle d'attente commune aux fournisseurs interrogeant une source."""
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            code = fetch(since)
            if code:
                return code
            time.sleep(self.poll_interval_seconds)
        raise OtpTimeout(
            f"[{self.name}] aucun code reçu en {self.timeout_seconds}s"
            + (f" ({hint})" if hint else "")
        )
