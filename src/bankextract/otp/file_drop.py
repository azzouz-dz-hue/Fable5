"""Lecture de l'OTP dans un fichier déposé par un outil tiers.

Passerelle de secours : n'importe quel script (macro, automatisation Android,
webhook) écrit le code dans un fichier, le connecteur le consomme.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .base import OtpProvider, extract_code


class FileDropOtpProvider(OtpProvider):
    name = "file"

    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        path = Path(self.options.get("path", "data/otp.txt"))
        since = since or datetime.now(timezone.utc)
        pattern = self.options.get("code_pattern") or r"\b(\d{4,8})\b"
        consume = bool(self.options.get("consume", True))

        def fetch(_since: datetime) -> str | None:
            if not path.exists():
                return None
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < _since:
                return None  # fichier laissé par une exécution précédente
            code = extract_code(path.read_text(encoding="utf-8", errors="replace"), pattern)
            if code and consume:
                path.unlink(missing_ok=True)
            return code

        return self._poll(fetch, since, hint or f"code déposé dans {path}")
