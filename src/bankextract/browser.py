"""Pilotage du navigateur (Playwright) pour les portails e-banking.

Le contexte est *persistant* : les cookies « appareil de confiance » survivent
d'une exécution à l'autre, ce qui évite un OTP à chaque lancement sur les
banques qui le proposent.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import BrowserConfig

logger = logging.getLogger(__name__)


class BrowserMissingError(RuntimeError):
    """Chromium n'est pas installé sur le poste."""


def browsers_root() -> Path:
    """Dossier durable où sont rangés les navigateurs téléchargés.

    La valeur « 0 » est ignorée : elle demande à Playwright de les ranger dans
    son propre paquet, ce qui ne convient pas ici (voir `ensure_browsers_path`).
    """
    impose = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if impose and impose != "0":
        return Path(impose)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def ensure_browsers_path() -> Path:
    """Impose un emplacement durable pour les navigateurs, et le renvoie.

    Sans cela, Playwright détecte l'exécutable compilé et force
    « PLAYWRIGHT_BROWSERS_PATH=0 », c'est-à-dire un rangement à l'intérieur de
    son propre paquet. Or ce paquet est extrait dans un dossier temporaire
    recréé à chaque lancement : le navigateur téléchargé serait perdu à la
    fermeture, et introuvable au lancement suivant.

    Fixer nous-mêmes la variable fait converger les trois opérations qui en
    dépendent : le téléchargement, la détection et le démarrage du navigateur.
    """
    racine = browsers_root()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(racine)
    return racine


#: Noms de l'exécutable selon la plateforme. Le dossier qui le contient, lui,
#: change au fil des versions de Playwright — « chrome-linux » hier,
#: « chrome-linux64 » aujourd'hui, « chrome-win64 » sous Windows — d'où une
#: recherche par nom de fichier plutôt que par chemin exact.
NOMS_EXECUTABLE = ("chrome.exe", "chrome", "headless_shell.exe", "headless_shell", "Chromium")

#: Une fois trouvé, le navigateur ne disparaît pas : inutile de refouiller le
#: disque à chaque rafraîchissement de l'interface.
_navigateur_trouve = False


def browser_installed(config: BrowserConfig | None = None) -> bool:
    """Dit si un Chromium utilisable est présent, sans démarrer Playwright.

    Démarrer Playwright pour le savoir coûterait une seconde et lancerait un
    processus Node : trop cher pour une vérification faite à chaque
    rafraîchissement de l'interface.
    """
    # Un chemin explicitement configuré fait foi : Playwright l'utilisera tel
    # quel et échouera s'il est faux, même si un autre navigateur traîne ailleurs.
    if config and config.executable_path:
        return Path(config.executable_path).exists()

    global _navigateur_trouve
    if _navigateur_trouve:
        return True

    racine = browsers_root()
    if not racine.is_dir():
        return False

    for dossier in racine.glob("chromium*"):
        for nom in NOMS_EXECUTABLE:
            if any(chemin.is_file() for chemin in dossier.rglob(nom)):
                _navigateur_trouve = True
                return True
    return False


def reset_browser_cache() -> None:
    """Oublie le résultat mémorisé — utile aux tests."""
    global _navigateur_trouve
    _navigateur_trouve = False


def explain_missing_browser(exc: Exception) -> Exception:
    """Traduit l'erreur technique de Playwright en consigne actionnable."""
    message = str(exc)
    if "Executable doesn't exist" in message or "playwright install" in message:
        return BrowserMissingError(
            "Le navigateur nécessaire n'est pas encore installé.\n"
            "Lancez une fois : bankextract setup"
        )
    return exc

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BrowserSession:
    """Enveloppe un contexte Playwright et les dossiers associés."""

    def __init__(self, context: BrowserContext, page: Page, config: BrowserConfig, bank: str):
        self.context = context
        self.page = page
        self.config = config
        self.bank = bank

    def goto(self, url: str, **kwargs) -> None:
        self.page.goto(url, wait_until=kwargs.pop("wait_until", "domcontentloaded"), **kwargs)

    def screenshot(self, name: str, directory: Path | None = None) -> Path | None:
        """Capture l'écran — indispensable pour diagnostiquer un sélecteur cassé."""
        target_dir = directory or Path("logs/screenshots")
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = target_dir / f"{self.bank}-{name}-{stamp}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:  # pragma: no cover - page déjà fermée
            logger.warning("Capture d'écran impossible : %s", exc)
            return None

    def download_to(self, trigger, destination_dir: Path, filename: str | None = None) -> Path:
        """Exécute `trigger()` et enregistre le téléchargement déclenché.

        `trigger` est un appelable qui provoque le téléchargement (un clic, en
        général) ; Playwright attend l'événement à notre place.
        """
        destination_dir.mkdir(parents=True, exist_ok=True)
        with self.page.expect_download(timeout=self.config.timeout_ms) as info:
            trigger()
        download = info.value
        target = destination_dir / (filename or download.suggested_filename or "releve.pdf")
        download.save_as(str(target))
        logger.info("Téléchargé : %s", target)
        return target


@contextmanager
def browser_session(config: BrowserConfig, bank: str) -> Iterator[BrowserSession]:
    """Ouvre un navigateur pour une banque et garantit sa fermeture."""
    ensure_browsers_path()
    profile_dir = config.profiles_dir / bank
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_args: dict = {
        "headless": config.headless,
        "slow_mo": config.slow_mo_ms,
        "locale": config.locale,
        "timezone_id": config.timezone,
        "user_agent": config.user_agent or DEFAULT_USER_AGENT,
        "accept_downloads": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if config.executable_path:
        launch_args["executable_path"] = config.executable_path

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), **launch_args
            )
        except Exception as exc:
            raise explain_missing_browser(exc) from exc
        context.set_default_timeout(config.timeout_ms)
        page = context.pages[0] if context.pages else context.new_page()
        session = BrowserSession(context=context, page=page, config=config, bank=bank)
        try:
            yield session
        finally:
            context.close()


@contextmanager
def ephemeral_browser(config: BrowserConfig, bank: str = "test") -> Iterator[BrowserSession]:
    """Navigateur sans profil persistant — utilisé par les tests."""
    ensure_browsers_path()
    launch_args: dict = {"headless": config.headless, "slow_mo": config.slow_mo_ms}
    if config.executable_path:
        launch_args["executable_path"] = config.executable_path

    with sync_playwright() as playwright:
        try:
            browser: Browser = playwright.chromium.launch(**launch_args)
        except Exception as exc:
            raise explain_missing_browser(exc) from exc
        context = browser.new_context(
            locale=config.locale,
            timezone_id=config.timezone,
            user_agent=config.user_agent or DEFAULT_USER_AGENT,
            accept_downloads=True,
        )
        context.set_default_timeout(config.timeout_ms)
        page = context.new_page()
        try:
            yield BrowserSession(context=context, page=page, config=config, bank=bank)
        finally:
            context.close()
            browser.close()
