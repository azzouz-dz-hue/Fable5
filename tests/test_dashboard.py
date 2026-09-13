"""Tableau de bord de consultation."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from bankextract.dashboard.app import build_chart, create_app, monthly_flows
from bankextract.models import Account, ExtractionResult, Transaction
from bankextract.storage import Database


@pytest.fixture
def client(settings):
    compte = Account(
        bank="bna",
        number="00100 987654321 09",
        label="Compte courant SARL",
        balance=Decimal("4235890.45"),
    )
    devise = Account(
        bank="bna", number="00100 987654322 41", currency="EUR", balance=Decimal("12480.00")
    )
    ecritures = [
        Transaction(
            account_key=compte.key,
            date=date(2026, 3, 4),
            label="VIR RECU CLIENT",
            amount=Decimal("1250000.00"),
        ),
        Transaction(
            account_key=compte.key,
            date=date(2026, 2, 3),
            label="REGLEMENT FOURNISSEUR",
            amount=Decimal("-845300.50"),
        ),
        Transaction(
            account_key=devise.key,
            date=date(2026, 2, 15),
            label="FRAIS SWIFT",
            amount=Decimal("-120.00"),
            currency="EUR",
        ),
    ]
    database = Database(settings.paths.database_url)
    database.save_result(
        ExtractionResult(bank="bna", accounts=[compte, devise], transactions=ecritures)
    )
    return TestClient(create_app(settings)), compte


def test_page_affiche_comptes_et_ecritures(client):
    http, compte = client

    reponse = http.get("/")

    assert reponse.status_code == 200
    assert compte.number in reponse.text
    assert "VIR RECU CLIENT" in reponse.text
    assert "Flux mensuels" in reponse.text


def test_filtre_par_compte(client):
    http, compte = client

    reponse = http.get("/", params={"compte": compte.key})

    assert "VIR RECU CLIENT" in reponse.text
    assert "FRAIS SWIFT" not in reponse.text


def test_soldes_totalises_par_devise(client):
    """Additionner dinars et euros n'aurait aucun sens : deux totaux distincts."""
    http, _ = client

    texte = http.get("/").text

    # Espace insécable : un montant ne doit jamais être coupé en fin de ligne.
    assert "4\u00a0235\u00a0890,45 DZD" in texte
    assert "12\u00a0480,00 EUR" in texte


def test_point_de_sante(client):
    http, _ = client

    donnees = http.get("/sante").json()

    assert donnees["statut"] == "ok"
    assert donnees["accounts"] == "2"


def test_base_vide_ne_casse_pas_la_page(settings):
    http = TestClient(create_app(settings))

    reponse = http.get("/")

    assert reponse.status_code == 200
    assert "Aucun compte" in reponse.text


def test_agregation_mensuelle():
    ecritures = [
        Transaction(account_key="b:1", date=date(2026, 1, 5), label="A", amount=Decimal(100)),
        Transaction(account_key="b:1", date=date(2026, 1, 20), label="B", amount=Decimal(-40)),
        Transaction(account_key="b:1", date=date(2026, 2, 3), label="C", amount=Decimal(-10)),
    ]

    flux = monthly_flows(ecritures)

    assert len(flux) == 2
    assert flux[0]["credit"] == Decimal(100) and flux[0]["debit"] == Decimal(40)
    assert flux[1]["debit"] == Decimal(10)


def test_geometrie_du_graphe_deux_series_par_mois():
    flux = monthly_flows(
        [
            Transaction(account_key="b:1", date=date(2026, 1, 5), label="A", amount=Decimal(100)),
            Transaction(account_key="b:1", date=date(2026, 2, 5), label="B", amount=Decimal(-50)),
        ]
    )

    graphe = build_chart(flux)
    barres = [b for b in graphe["bars"] if b["series"] != "axis"]

    assert len(barres) == 4  # deux mois × (crédit + débit)
    assert all(b["path"].startswith("M ") for b in barres)
    assert graphe["peak"] == "100,00"


def test_graphe_sans_donnee():
    graphe = build_chart([])

    assert graphe["bars"] == []
