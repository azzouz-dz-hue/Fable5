"""Installation du navigateur.

Le piège à éviter : « python -m playwright install » ne fonctionne pas depuis
l'exécutable compilé, où `sys.executable` désigne bankextract.exe.
"""

import subprocess
import sys

import pytest

from bankextract.install import (
    InstallationNavigateurError,
    commande_installation,
    environnement_pilote,
    installer_navigateur,
)


def test_la_commande_n_appelle_jamais_python():
    """C'est exactement ce qui produisait « No such option: -m »."""
    commande = commande_installation()

    assert "-m" not in commande
    assert sys.executable not in commande
    assert commande[-2:] == ["install", "chromium"]


def test_la_commande_vise_le_pilote_playwright():
    commande = commande_installation()

    assert any("cli.js" in partie or "node" in partie.lower() for partie in commande)


def test_navigateur_choisi():
    assert commande_installation("firefox")[-1] == "firefox"


def test_environnement_du_pilote():
    assert environnement_pilote()["PW_LANG_NAME"] == "python"


def test_l_invocation_est_acceptee_par_le_pilote():
    """Vérifie l'appel réel, sans télécharger les 150 Mo."""
    commande = commande_installation()[:-2] + ["install", "--dry-run", "chromium"]

    issue = subprocess.run(
        commande, capture_output=True, text=True, timeout=180, env=environnement_pilote()
    )

    assert issue.returncode == 0, issue.stderr
    assert "Install location" in issue.stdout


def test_echec_remonte_un_message_lisible(monkeypatch):
    def faux_run(*args, **kwargs):
        class Issue:
            returncode = 1
            stdout = ""
            stderr = "connexion refusée"

        return Issue()

    monkeypatch.setattr("bankextract.install.subprocess.run", faux_run)

    with pytest.raises(InstallationNavigateurError, match="Vérifiez votre connexion"):
        installer_navigateur()


def test_delai_depasse_explique_quoi_faire(monkeypatch):
    def faux_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="playwright", timeout=1800)

    monkeypatch.setattr("bankextract.install.subprocess.run", faux_run)

    with pytest.raises(InstallationNavigateurError, match="30 minutes"):
        installer_navigateur()


def test_pilote_absent_conseille_la_reinstallation(monkeypatch):
    def faux_run(*args, **kwargs):
        raise FileNotFoundError("node introuvable")

    monkeypatch.setattr("bankextract.install.subprocess.run", faux_run)

    with pytest.raises(InstallationNavigateurError, match="Réinstallez"):
        installer_navigateur()


def test_journal_recoit_la_progression(monkeypatch):
    lignes = []

    def faux_run(*args, **kwargs):
        class Issue:
            returncode = 0
            stdout = "Downloading Chromium\nChromium downloaded to /chemin"
            stderr = ""

        return Issue()

    monkeypatch.setattr("bankextract.install.subprocess.run", faux_run)
    installer_navigateur(journal=lignes.append)

    assert "Downloading Chromium" in lignes


# ---------------------------------------------------------------- détection


@pytest.fixture
def racine_navigateurs(tmp_path, monkeypatch):
    """Simule le dossier où Playwright dépose les navigateurs."""
    from bankextract.browser import reset_browser_cache

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    reset_browser_cache()
    yield tmp_path
    reset_browser_cache()


def _deposer(racine, relatif):
    cible = racine / relatif
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_bytes(b"")


@pytest.mark.parametrize(
    "arborescence",
    [
        # Playwright renomme le dossier au fil des versions : « chrome-win » hier,
        # « chrome-win64 » depuis le passage à Chrome for Testing. Chercher un
        # chemin exact revenait à casser à chaque mise à jour.
        "chromium-1234/chrome-win64/chrome.exe",
        "chromium-1148/chrome-win/chrome.exe",
        "chromium-1234/chrome-linux64/chrome",
        "chromium-1194/chrome-linux/chrome",
        "chromium_headless_shell-1234/chrome-win64/headless_shell.exe",
        "chromium-1234/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    ],
)
def test_navigateur_detecte_quel_que_soit_le_nommage(racine_navigateurs, arborescence):
    from bankextract.browser import browser_installed

    _deposer(racine_navigateurs, arborescence)

    assert browser_installed() is True


def test_aucun_navigateur(racine_navigateurs):
    from bankextract.browser import browser_installed

    _deposer(racine_navigateurs, "ffmpeg-1011/ffmpeg")

    assert browser_installed() is False


def test_dossier_absent(tmp_path, monkeypatch):
    from bankextract.browser import browser_installed, reset_browser_cache

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "inexistant"))
    reset_browser_cache()

    assert browser_installed() is False


def test_resultat_memorise(racine_navigateurs):
    """Le navigateur ne disparaît pas : inutile de refouiller le disque."""
    from bankextract.browser import browser_installed

    _deposer(racine_navigateurs, "chromium-1234/chrome-win64/chrome.exe")
    assert browser_installed() is True

    for fichier in racine_navigateurs.rglob("chrome.exe"):
        fichier.unlink()

    assert browser_installed() is True, "le résultat positif doit rester mémorisé"
