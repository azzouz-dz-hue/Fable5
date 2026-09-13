"""Installation du navigateur nécessaire à l'extraction.

Playwright s'installe habituellement par « python -m playwright install ».
C'est impossible depuis l'exécutable compilé : `sys.executable` y désigne
`bankextract.exe`, qui ne connaît pas l'option `-m`. On appelle donc
directement le pilote Node fourni par Playwright, ce qui fonctionne aussi bien
depuis les sources que depuis l'exécutable.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Le téléchargement pèse environ 150 Mo ; sur une liaison lente il faut du temps.
DELAI_SECONDES = 1800


class InstallationNavigateurError(RuntimeError):
    """Le navigateur n'a pas pu être installé."""


def commande_installation(navigateur: str = "chromium") -> list[str]:
    """Construit l'appel au pilote Playwright, sans passer par Python."""
    from playwright._impl._driver import compute_driver_executable

    cible = compute_driver_executable()
    # Selon les versions, la fonction renvoie (node, cli.js) ou un seul chemin.
    base = list(cible) if isinstance(cible, (tuple, list)) else [str(cible)]
    return [*base, "install", navigateur]


def environnement_pilote() -> dict:
    """Environnement du pilote, avec l'emplacement durable des navigateurs.

    C'est le même que celui utilisé au lancement : sans quoi le téléchargement
    atterrirait à un endroit où personne ne viendrait le chercher.
    """
    from playwright._impl._driver import get_driver_env

    from .browser import ensure_browsers_path

    racine = ensure_browsers_path()
    environnement = get_driver_env()
    environnement["PLAYWRIGHT_BROWSERS_PATH"] = str(racine)
    return environnement


def installer_navigateur(
    navigateur: str = "chromium",
    journal: Callable[[str], None] | None = None,
    delai: int = DELAI_SECONDES,
) -> str:
    """Télécharge le navigateur ; lève `InstallationNavigateurError` en cas d'échec.

    `journal` reçoit les lignes de progression, pour affichage dans l'interface.
    """
    noter = journal or (lambda ligne: logger.info(ligne))
    commande = commande_installation(navigateur)
    logger.debug("Installation du navigateur : %s", " ".join(commande))

    try:
        issue = subprocess.run(
            commande,
            capture_output=True,
            text=True,
            timeout=delai,
            env=environnement_pilote(),
        )
    except FileNotFoundError as exc:
        raise InstallationNavigateurError(
            "Le composant d'installation de Playwright est introuvable. "
            "Réinstallez le logiciel."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallationNavigateurError(
            f"Téléchargement interrompu après {delai // 60} minutes. "
            "Vérifiez votre connexion, puis réessayez."
        ) from exc

    for ligne in (issue.stdout or "").splitlines()[-15:]:
        if ligne.strip():
            noter(ligne.strip())

    if issue.returncode != 0:
        detail = (issue.stderr or issue.stdout or "").strip()[:400]
        raise InstallationNavigateurError(
            "Téléchargement impossible. Vérifiez votre connexion, puis réessayez."
            + (f"\n{detail}" if detail else "")
        )

    return "Navigateur installé. Le poste est prêt."
