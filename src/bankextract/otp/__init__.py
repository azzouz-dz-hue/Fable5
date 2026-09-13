"""Fournisseurs de code d'authentification forte (OTP).

Un connecteur ne sait pas d'où vient le code : il appelle `provider.wait_for_code()`.
Cela permet de passer de la saisie manuelle à la passerelle SMS sans toucher
au code de la banque.
"""

from __future__ import annotations

from .base import OtpProvider, OtpTimeout, extract_code
from .file_drop import FileDropOtpProvider
from .imap_mail import ImapOtpProvider
from .manual import ManualOtpProvider
from .sms_gateway import SmsGatewayOtpProvider
from .totp import TotpOtpProvider

_REGISTRY: dict[str, type[OtpProvider]] = {
    "manual": ManualOtpProvider,
    "sms_gateway": SmsGatewayOtpProvider,
    "imap": ImapOtpProvider,
    "totp": TotpOtpProvider,
    "file": FileDropOtpProvider,
}


def build_otp_provider(config) -> OtpProvider:
    """Instancie le fournisseur décrit par `OtpConfig`."""
    provider_cls = _REGISTRY.get(config.provider)
    if provider_cls is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Fournisseur OTP inconnu : « {config.provider} ». Connus : {known}")
    return provider_cls(
        options=config.options,
        timeout_seconds=config.timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
    )


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "FileDropOtpProvider",
    "ImapOtpProvider",
    "ManualOtpProvider",
    "OtpProvider",
    "OtpTimeout",
    "SmsGatewayOtpProvider",
    "TotpOtpProvider",
    "available_providers",
    "build_otp_provider",
    "extract_code",
]
