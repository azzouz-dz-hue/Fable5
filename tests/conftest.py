"""Fixtures partagées."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "fake_bank"))


def _find_chromium() -> str | None:
    """Cherche un Chromium utilisable ; None si Playwright doit se débrouiller."""
    if path := os.getenv("BANKEXTRACT_CHROMIUM"):
        return path if Path(path).exists() else None
    for candidate in ("/opt/pw-browsers/chromium", shutil.which("chromium"),
                      shutil.which("chromium-browser"), shutil.which("google-chrome")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


CHROMIUM = _find_chromium()


def _navigateur_disponible() -> bool:
    """Un navigateur est-il utilisable, à un endroit ou à un autre ?

    Sous Windows, Playwright range le sien dans le dossier de l'utilisateur :
    aucun chemin connu d'avance ne le désigne, et le chercher par chemin faisait
    sauter en silence tous les tests qui pilotent un navigateur — la moitié de
    la suite passait alors pour vérifiée sans l'être.
    """
    if CHROMIUM is not None:
        return True
    sys.path.insert(0, str(ROOT / "src"))
    from bankextract.browser import browser_installed

    return browser_installed()


NAVIGATEUR_DISPONIBLE = _navigateur_disponible()


@pytest.fixture(autouse=True)
def _oublier_le_navigateur_memorise():
    """Le résultat mémorisé ne doit pas franchir la frontière entre deux tests.

    Il vaut pour la durée d'une exécution réelle — un navigateur installé ne
    disparaît pas — mais un test qui déplace le dossier des navigateurs verrait
    sinon la réponse du test précédent.
    """
    from bankextract.browser import reset_browser_cache

    reset_browser_cache()
    yield
    reset_browser_cache()


@pytest.fixture
def settings(tmp_path):
    """Configuration isolée : rien n'est écrit hors du dossier temporaire."""
    from bankextract.config import BrowserConfig, PathsConfig, Settings

    config = Settings(
        browser=BrowserConfig(
            headless=True,
            executable_path=CHROMIUM,
            profiles_dir=tmp_path / "profiles",
            timeout_ms=20_000,
            screenshot_on_error=False,
        ),
        paths=PathsConfig(
            data_dir=tmp_path / "data",
            downloads_dir=tmp_path / "data" / "downloads",
            exports_dir=tmp_path / "data" / "exports",
            logs_dir=tmp_path / "logs",
            database_url=f"sqlite:///{tmp_path}/test.db",
        ),
    )
    config.paths.ensure()
    return config


@pytest.fixture
def fake_bank(tmp_path):
    """Démarre le faux portail e-banking et renvoie (url, fichier_otp)."""
    from server import start_fake_bank

    otp_file = tmp_path / "otp.txt"
    server, url = start_fake_bank(otp_file)
    try:
        yield url, otp_file
    finally:
        server.shutdown()
        server.server_close()
