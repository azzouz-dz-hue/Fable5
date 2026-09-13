"""Accès aux identifiants bancaires, sans jamais les écrire dans le dépôt.

Ordre de recherche : trousseau système (keyring) → variable d'environnement.
Le trousseau est préférable : rien ne traîne dans un fichier ni dans l'historique
du shell.
"""

from __future__ import annotations

import getpass
import logging
import os
from dataclasses import dataclass

from .config import BankConfig

logger = logging.getLogger(__name__)

try:  # keyring n'est pas disponible sur toutes les plateformes (serveurs sans D-Bus)
    import keyring

    _KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover - dépend de l'environnement
    keyring = None  # type: ignore[assignment]
    _KEYRING_AVAILABLE = False


class CredentialsError(RuntimeError):
    """Identifiants introuvables ou incomplets."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # évite toute fuite dans les logs et les traces
        return f"Credentials(username={self.username!r}, password='***')"


def _keyring_get(service: str, key: str) -> str | None:
    if not _KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(service, key)
    except Exception:  # pragma: no cover - backend indisponible
        return None


def get_credentials(
    bank_name: str, config: BankConfig, *, interactive: bool = False
) -> Credentials:
    """Résout les identifiants d'une banque.

    `interactive=True` autorise une saisie au clavier en dernier recours, utile
    pour une première exécution manuelle.
    """
    service = config.keyring_service or f"bankextract:{bank_name}"

    username = _keyring_get(service, "username")
    password = _keyring_get(service, "password")

    if not username and config.username_env:
        username = os.getenv(config.username_env)
    if not password and config.password_env:
        password = os.getenv(config.password_env)

    if not username:
        username = os.getenv(f"BANKEXTRACT_{bank_name.upper()}_USERNAME")
    if not password:
        password = os.getenv(f"BANKEXTRACT_{bank_name.upper()}_PASSWORD")

    if interactive and not username:
        username = input(f"Identifiant {config.display_name} : ").strip()
    if interactive and not password:
        password = getpass.getpass(f"Mot de passe {config.display_name} : ")

    if not username or not password:
        raise CredentialsError(
            f"Identifiants manquants pour « {bank_name} ». "
            f"Enregistrez-les avec : bankextract login {bank_name}"
        )
    return Credentials(username=username, password=password)


def store_credentials(bank_name: str, config: BankConfig, creds: Credentials) -> str:
    """Écrit les identifiants dans le trousseau système."""
    if not _KEYRING_AVAILABLE:
        raise CredentialsError(
            "Aucun trousseau système disponible. Utilisez les variables "
            "d'environnement décrites dans .env.example."
        )
    service = config.keyring_service or f"bankextract:{bank_name}"
    keyring.set_password(service, "username", creds.username)
    keyring.set_password(service, "password", creds.password)
    return service


def delete_credentials(bank_name: str, config: BankConfig) -> None:
    if not _KEYRING_AVAILABLE:
        return
    service = config.keyring_service or f"bankextract:{bank_name}"
    for key in ("username", "password"):
        try:
            keyring.delete_password(service, key)
        except Exception:  # pragma: no cover - entrée déjà absente
            logger.debug("Rien à supprimer pour %s/%s", service, key)
