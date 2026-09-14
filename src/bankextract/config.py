"""Chargement de la configuration (YAML + variables d'environnement)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path("config/banks.yaml")


class OtpConfig(BaseModel):
    """Source du code d'authentification forte."""

    provider: str = Field(default="manual", description="manual | sms_gateway | imap | totp")
    options: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 180
    poll_interval_seconds: float = 3.0


class BankConfig(BaseModel):
    """Paramètres d'un connecteur bancaire."""

    connector: str = Field(description="Identifiant du connecteur (ex. 'bna')")
    enabled: bool = True
    label: str = ""
    username_env: str | None = None
    password_env: str | None = None
    keyring_service: str | None = None
    otp: OtpConfig = Field(default_factory=OtpConfig)
    history_days: int = 90
    download_statements: bool = True
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.label or self.connector.upper()


class BrowserConfig(BaseModel):
    headless: bool = True
    slow_mo_ms: int = 0
    timeout_ms: int = 45_000
    locale: str = "fr-FR"
    timezone: str = "Africa/Algiers"
    user_agent: str | None = None
    profiles_dir: Path = Path("browser-profiles")
    screenshot_on_error: bool = True
    executable_path: str | None = Field(
        default=None, description="Chemin d'un Chromium déjà installé"
    )
    channel: str | None = Field(
        default=None,
        description=(
            "Navigateur à piloter : « chrome » ou « msedge » pour celui déjà "
            "installé sur le poste, vide pour le Chromium fourni."
        ),
    )
    interface_browser: str | None = Field(
        default=None,
        description=(
            "Navigateur où ouvrir l'interface : « chrome », « firefox », « edge »… "
            "vide pour celui par défaut du système."
        ),
    )


class PathsConfig(BaseModel):
    data_dir: Path = Path("data")
    downloads_dir: Path = Path("data/downloads")
    exports_dir: Path = Path("data/exports")
    logs_dir: Path = Path("logs")
    database_url: str = "sqlite:///data/bankextract.db"

    def ensure(self) -> None:
        for directory in (self.data_dir, self.downloads_dir, self.exports_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)


class Settings(BaseModel):
    """Configuration complète de l'application."""

    banks: dict[str, BankConfig] = Field(default_factory=dict)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    def enabled_banks(self) -> dict[str, BankConfig]:
        return {name: cfg for name, cfg in self.banks.items() if cfg.enabled}


def local_overlay_path(path: Path | str = DEFAULT_CONFIG_PATH) -> Path:
    """Fichier des réglages propres au poste, fusionné par-dessus le modèle.

    Il porte vos banques et vos numéros de compte ; il est exclu du dépôt, si
    bien que le modèle livré reste documenté et que rien de personnel n'est
    publié par mégarde.
    """
    config_path = Path(path)
    return config_path.with_name(f"{config_path.stem}.local{config_path.suffix}")


def _fusionner(base: dict[str, Any], surcouche: dict[str, Any]) -> dict[str, Any]:
    """Fusion en profondeur : la surcouche complète la base sans la remplacer."""
    resultat = dict(base)
    for cle, valeur in surcouche.items():
        ancienne = resultat.get(cle)
        if isinstance(ancienne, dict) and isinstance(valeur, dict):
            resultat[cle] = _fusionner(ancienne, valeur)
        else:
            resultat[cle] = valeur
    return resultat


def load_settings(path: Path | str = DEFAULT_CONFIG_PATH) -> Settings:
    """Charge `config/banks.yaml`, sa surcouche locale puis `.env`."""
    load_dotenv(override=False)

    config_path = Path(path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    surcouche_path = local_overlay_path(config_path)
    if surcouche_path.exists():
        surcouche = yaml.safe_load(surcouche_path.read_text(encoding="utf-8")) or {}
        raw = _fusionner(raw, surcouche)

    # Un Chromium préinstallé (image CI, poste verrouillé) prime sur le téléchargement.
    browser = raw.setdefault("browser", {})
    if not browser.get("executable_path") and os.getenv("BANKEXTRACT_CHROMIUM"):
        browser["executable_path"] = os.environ["BANKEXTRACT_CHROMIUM"]

    settings = Settings.model_validate(raw)
    settings.paths.ensure()
    return settings
