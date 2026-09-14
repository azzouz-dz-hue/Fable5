"""Extraction de bout en bout contre le faux portail e-banking.

Couvre le parcours complet : identifiants, OTP lu automatiquement, pagination,
téléchargement des relevés, puis alimentation de la base et exports.
"""

from datetime import date

import pytest
from conftest import NAVIGATEUR_DISPONIBLE

from bankextract.config import BankConfig, OtpConfig
from bankextract.connectors import build_connector
from bankextract.connectors.base import LoginError
from bankextract.pipeline import run_banks
from bankextract.storage import Database

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not NAVIGATEUR_DISPONIBLE, reason="aucun navigateur installé"
    ),
]

DEBUT = date(2026, 2, 1)
FIN = date(2026, 3, 31)


def _config(url: str, otp_file, **kwargs) -> BankConfig:
    return BankConfig(
        connector="demo",
        label="Banque Demo",
        history_days=400,
        options={"base_url": url},
        otp=OtpConfig(
            provider="file",
            options={"path": str(otp_file)},
            timeout_seconds=20,
            poll_interval_seconds=0.3,
        ),
        **kwargs,
    )


@pytest.fixture
def identifiants(monkeypatch):
    monkeypatch.setenv("BANKEXTRACT_DEMO_USERNAME", "demo")
    monkeypatch.setenv("BANKEXTRACT_DEMO_PASSWORD", "demo123")


def test_extraction_complete(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=DEBUT, end=FIN)

    assert resultat.errors == []
    assert len(resultat.accounts) == 2

    courant, devise = resultat.accounts
    assert courant.number == "00100 987654321 09"
    assert courant.currency == "DZD"
    assert devise.currency == "EUR"

    # 8 écritures sur le compte courant (deux pages) + 3 sur le compte devise.
    assert len(resultat.transactions) == 11


def test_pagination_parcourue_entierement(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=DEBUT, end=FIN)
    courant = [t for t in resultat.transactions if t.account_key.endswith("2109")]

    assert len(courant) == 8, "la seconde page d'opérations doit être lue"
    assert min(t.date for t in courant) == date(2026, 2, 18)


def test_signes_et_soldes_normalises(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=DEBUT, end=FIN)
    par_libelle = {t.label: t for t in resultat.transactions}

    assert par_libelle["VIR RECU CLIENT SPA PHARMA"].amount > 0
    assert par_libelle["FRAIS TENUE DE COMPTE"].amount < 0
    assert par_libelle["REGLEMENT FOURNISSEUR IMPORT DM-2026-014"].reference == "CHQ0091823"
    assert str(par_libelle["VIR RECU CLIENT SPA PHARMA"].balance_after) == "4235890.45"


def test_periode_respectee(settings, fake_bank, identifiants):
    """Le portail déborde du filtre : le connecteur doit couper à la période."""
    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=date(2026, 3, 1), end=date(2026, 3, 31))

    assert resultat.transactions
    assert all(date(2026, 3, 1) <= t.date <= date(2026, 3, 31) for t in resultat.transactions)


def test_releves_telecharges(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=DEBUT, end=FIN)

    assert len(resultat.files) == 6  # 3 relevés par compte
    for fichier in resultat.files:
        assert fichier.path.exists() and fichier.path.stat().st_size > 0
        assert fichier.sha256, "l'empreinte sert à ne pas ré-archiver deux fois"


def test_mot_de_passe_refuse_remonte_le_message_de_la_banque(
    settings, fake_bank, monkeypatch
):
    url, otp_file = fake_bank
    monkeypatch.setenv("BANKEXTRACT_DEMO_USERNAME", "demo")
    monkeypatch.setenv("BANKEXTRACT_DEMO_PASSWORD", "mauvais")
    connector = build_connector("demo", _config(url, otp_file), settings)

    resultat = connector.run(start=DEBUT, end=FIN)

    assert not resultat.ok
    assert any("incorrect" in erreur for erreur in resultat.errors)
    assert resultat.accounts == []


def test_identifiants_absents_ne_lancent_pas_le_navigateur(settings, fake_bank, monkeypatch):
    url, otp_file = fake_bank
    for variable in ("BANKEXTRACT_DEMO_USERNAME", "BANKEXTRACT_DEMO_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)
    config = _config(url, otp_file)
    config.username_env = "ABSENT_USERNAME"
    config.password_env = "ABSENT_PASSWORD"
    connector = build_connector("demo", config, settings)

    resultat = connector.run(start=DEBUT, end=FIN)

    assert any("Identifiants" in erreur for erreur in resultat.errors)


def test_chaine_complete_base_et_exports(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    settings.banks = {"demo": _config(url, otp_file)}
    database = Database(settings.paths.database_url)

    bilans = run_banks(settings, start=DEBUT, end=FIN, database=database)

    assert len(bilans) == 1
    bilan = bilans[0]
    assert bilan.ok and bilan.saved.new_transactions == 11
    assert {chemin.suffix for chemin in bilan.exports} == {".csv", ".xlsx"}
    assert all(chemin.exists() for chemin in bilan.exports)
    assert database.totals()["accounts"] == 2


def test_relance_n_ajoute_aucun_doublon(settings, fake_bank, identifiants):
    url, otp_file = fake_bank
    settings.banks = {"demo": _config(url, otp_file)}
    database = Database(settings.paths.database_url)

    run_banks(settings, start=DEBUT, end=FIN, database=database)
    second = run_banks(settings, start=DEBUT, end=FIN, database=database)[0]

    assert second.saved.new_transactions == 0
    assert second.saved.duplicate_transactions == 11
    assert database.totals()["transactions"] == 11


def test_banque_inconnue_signalee(settings):
    settings.banks = {}
    with pytest.raises(KeyError, match="inconnue"):
        run_banks(settings, banks=["fantome"])


def test_connexion_invalide_leve_login_error(settings, fake_bank, monkeypatch):
    """L'erreur brute reste typée pour qui appelle le connecteur directement."""
    from bankextract.browser import browser_session
    from bankextract.secrets import Credentials

    url, otp_file = fake_bank
    connector = build_connector("demo", _config(url, otp_file), settings)

    with browser_session(settings.browser, "demo") as session, pytest.raises(LoginError):
        connector.login(session, Credentials("demo", "mauvais"))
