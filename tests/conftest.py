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
