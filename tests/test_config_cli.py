"""Configuration, registre des connecteurs et interface en ligne de commande."""

import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from bankextract.cli import app
from bankextract.config import BankConfig, Settings, load_settings
from bankextract.connectors import available_connectors, build_connector, get_connector_class
from bankextract.secrets import Credentials, CredentialsError, get_credentials

runner = CliRunner()


def test_configuration_livree_est_valide():
    settings = load_settings("config/banks.yaml")

    assert "bna" in settings.banks
    assert settings.banks["bna"].connector == "bna"
    assert settings.banks["bna"].otp.provider == "sms_gateway"


def test_toutes_les_banques_livrees_sont_desactivees_par_defaut():
    """Rien ne doit se connecter tant que l'utilisateur n'a pas vérifié sa config."""
    settings = load_settings("config/banks.yaml")

    assert settings.enabled_banks() == {}


def test_chaque_banque_configuree_a_un_connecteur_connu():
    settings = load_settings("config/banks.yaml")

    for nom, banque in settings.banks.items():
        assert banque.connector in available_connectors(), nom


def test_configuration_absente_donne_des_valeurs_par_defaut(tmp_path):
    settings = load_settings(tmp_path / "introuvable.yaml")

    assert settings.banks == {}
    assert settings.browser.headless is True


def test_connecteur_inconnu_liste_les_choix():
    with pytest.raises(KeyError, match="bna"):
        get_connector_class("banque-imaginaire")


def test_plusieurs_acces_chez_la_meme_banque():
    """Deux profils BNA doivent rester distincts (base, profil navigateur, trousseau)."""
    settings = Settings()
    config = BankConfig(connector="bna")

    premier = build_connector("bna_sarl", config, settings)
    second = build_connector("bna_perso", config, settings)

    assert premier.name == "bna_sarl" and second.name == "bna_perso"


def test_identifiants_lus_depuis_l_environnement(monkeypatch):
    monkeypatch.setenv("MA_BANQUE_USER", "sarl-medico")
    monkeypatch.setenv("MA_BANQUE_PASS", "secret")
    config = BankConfig(
        connector="bna", username_env="MA_BANQUE_USER", password_env="MA_BANQUE_PASS"
    )

    creds = get_credentials("bna", config)

    assert creds.username == "sarl-medico" and creds.password == "secret"


def test_identifiants_jamais_affiches_en_clair():
    """Une trace d'exécution ne doit pas révéler le mot de passe."""
    assert "secret" not in repr(Credentials("user", "secret"))


def test_identifiants_manquants_indiquent_la_marche_a_suivre(monkeypatch):
    monkeypatch.delenv("BANKEXTRACT_BNA_USERNAME", raising=False)
    monkeypatch.delenv("BANKEXTRACT_BNA_PASSWORD", raising=False)

    with pytest.raises(CredentialsError, match="bankextract login"):
        get_credentials("bna", BankConfig(connector="bna", keyring_service="service-inexistant"))


def test_cli_list_affiche_les_banques():
    resultat = runner.invoke(app, ["list", "--config", "config/banks.yaml"])

    assert resultat.exit_code == 0
    assert "bna" in resultat.stdout


def test_cli_extract_refuse_une_banque_inconnue():
    resultat = runner.invoke(app, ["extract", "fantome", "--config", "config/banks.yaml"])

    assert resultat.exit_code == 2


def test_cli_aide_disponible():
    resultat = runner.invoke(app, ["--help"])

    assert resultat.exit_code == 0
    for commande in ("extract", "login", "dashboard", "export", "inspect"):
        assert commande in resultat.stdout


def test_surcouche_locale_complete_le_modele(tmp_path):
    """Les réglages du poste s'ajoutent au modèle documenté sans le réécrire."""
    import yaml

    from bankextract.config import local_overlay_path

    modele = tmp_path / "banks.yaml"
    modele.write_text(
        yaml.safe_dump(
            {"banks": {"bna": {"connector": "bna", "enabled": False, "history_days": 90}}}
        )
    )
    local_overlay_path(modele).write_text(
        yaml.safe_dump({"banks": {"bna": {"enabled": True}, "cpa": {"connector": "scenario"}}})
    )

    settings = load_settings(modele)

    assert settings.banks["bna"].enabled is True
    assert settings.banks["bna"].history_days == 90, "le modèle reste la base"
    assert settings.banks["cpa"].connector == "scenario"


def test_surcouche_absente_sans_effet(tmp_path):
    import yaml

    modele = tmp_path / "banks.yaml"
    modele.write_text(yaml.safe_dump({"banks": {"bna": {"connector": "bna"}}}))

    assert load_settings(modele).banks["bna"].connector == "bna"


def test_cli_app_disponible():
    resultat = runner.invoke(app, ["--help"])

    assert "app" in resultat.stdout and "setup" in resultat.stdout


# ---------------------------------------------------------------- sortie console


def _executer_avec_console_windows(code: str) -> subprocess.CompletedProcess:
    """Exécute du code Python avec la sortie contrainte en cp1252.

    C'est la situation d'une console Windows ordinaire, ou d'une sortie
    redirigée vers un fichier journal.
    """
    environnement = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environnement,
        timeout=60,
    )


def test_les_symboles_passent_sur_une_console_windows():
    """« ✓ » n'existe pas en cp1252 : sans précaution, la commande échoue."""
    code = (
        "from bankextract.cli import _configurer_sortie, console\n"
        "_configurer_sortie()\n"
        "console.print('[green]\\u2713[/green] pr\\u00eat \\u2014 \\u2022\\u2022\\u2022')\n"
    )

    issue = _executer_avec_console_windows(code)

    assert issue.returncode == 0, issue.stderr


def test_sans_la_correction_la_sortie_echouerait():
    """Contrôle négatif : montre que la précaution sert vraiment à quelque chose."""
    code = "import sys\nsys.stdout.write('\\u2713')\n"

    issue = _executer_avec_console_windows(code)

    assert issue.returncode != 0
    assert "charmap" in issue.stderr or "encode" in issue.stderr


def test_configurer_la_sortie_est_sans_effet_de_bord():
    from bankextract.cli import _configurer_sortie

    _configurer_sortie()
    _configurer_sortie()  # deux fois de suite ne doit rien casser
