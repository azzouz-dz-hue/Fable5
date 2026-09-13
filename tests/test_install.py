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
