"""Code calculé localement (TOTP RFC 6238), pour les banques à token logiciel."""

from __future__ import annotations

import os
from datetime import datetime

import pyotp

from .base import OtpProvider


class TotpOtpProvider(OtpProvider):
    name = "totp"

    def wait_for_code(self, since: datetime | None = None, hint: str = "") -> str:
        secret = self.options.get("secret")
        if not secret and (env := self.options.get("secret_env")):
            secret = os.getenv(env)
        if not secret:
            raise ValueError(
                "Fournisseur TOTP : renseignez « secret » ou « secret_env » dans la configuration."
            )
        totp = pyotp.TOTP(
            secret.replace(" ", ""),
            digits=int(self.options.get("digits", 6)),
            interval=int(self.options.get("interval", 30)),
        )
        return totp.now()
