"""Saisie du code au clavier — repli universel, fonctionne avec toutes les banques."""

from __future__ import annotations

from datetime import datetime

from .base import OtpProvider


class ManualOtpProvider(OtpProvider):
    name = "manual"

    @property
    def automatic(self) -> bool:
        return False

    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        prompt = hint or "Code reçu par SMS"
        return input(f"→ {prompt} : ").strip()
