"""Normalisation des montants, dates et libellés."""

from datetime import date
from decimal import Decimal

import pytest

from bankextract.normalize import (
    clean_label,
    detect_currency,
    format_amount,
    parse_amount,
    parse_date,
    signed_amount,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1 500,50", Decimal("1500.50")),      # espace + virgule décimale
        ("1.500,50", Decimal("1500.50")),      # point de milliers
        ("1,500.50", Decimal("1500.50")),      # convention anglo-saxonne
        ("1500.50 DA", Decimal("1500.50")),    # devise accolée
        ("1 500,50 DZD", Decimal("1500.50")),
        ("(1 500,50)", Decimal("-1500.50")),   # parenthèses comptables
        ("1 500,50-", Decimal("-1500.50")),    # signe en suffixe
        ("-1 234", Decimal(-1234)),
        ("+980,25", Decimal("980.25")),
        ("0,00", Decimal("0.00")),
        ("1 234 567,89", Decimal("1234567.89")),
        (" 1 500,50 ", Decimal("1500.50")),  # espaces insécables
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", None, "—"])
def test_parse_amount_vide(raw):
    assert parse_amount(raw) is None


def test_parse_amount_types_natifs():
    assert parse_amount(Decimal("12.30")) == Decimal("12.30")
    assert parse_amount(42) == Decimal(42)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("04/03/2026", date(2026, 3, 4)),
        ("04-03-2026", date(2026, 3, 4)),
        ("04.03.2026", date(2026, 3, 4)),
        ("2026-03-04", date(2026, 3, 4)),
        ("20260304", date(2026, 3, 4)),
        ("4 mars 2026", date(2026, 3, 4)),
        ("15 février 2025", date(2025, 2, 15)),
        ("04.03.26", date(2026, 3, 4)),
        ("Opération du 04/03/2026 à 10h", date(2026, 3, 4)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_annee_deux_chiffres_avant_pivot():
    """« 85 » désigne 1985, pas 2085."""
    assert parse_date("15/08/85") == date(1985, 8, 15)


@pytest.mark.parametrize("raw", ["", "néant", None, "32/13/2026"])
def test_parse_date_invalide(raw):
    assert parse_date(raw) is None


def test_signed_amount_colonnes_separees():
    assert signed_amount(debit="1 500,50") == Decimal("-1500.50")
    assert signed_amount(credit="1 500,50") == Decimal("1500.50")


def test_signed_amount_colonne_sens():
    assert signed_amount(amount="300", sense="D") == Decimal(-300)
    assert signed_amount(amount="300", sense="C") == Decimal(300)
    assert signed_amount(amount="300", sense="Crédit") == Decimal(300)


def test_signed_amount_montant_deja_signe():
    assert signed_amount(amount="-300,50") == Decimal("-300.50")


def test_signed_amount_absent():
    assert signed_amount() is None


def test_clean_label():
    assert clean_label("  VIR   RECU\nCLIENT\t ") == "VIR RECU CLIENT"
    assert clean_label("— REGLEMENT —") == "REGLEMENT"
    assert clean_label("") == ""


@pytest.mark.parametrize(
    "text, expected",
    [("Solde 1 200 DA", "DZD"), ("12,50 €", "EUR"), ("100 USD", "USD"), ("1 200", "DZD")],
)
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        # Espace insécable (\u00a0) : un montant ne doit jamais être coupé en fin de ligne.
        (Decimal("4235890.45"), "4\u00a0235\u00a0890,45"),
        (Decimal("-1200.5"), "-1\u00a0200,50"),
        (Decimal(0), "0,00"),
        (None, "—"),
    ],
)
def test_format_amount_convention_francaise(value, expected):
    assert format_amount(value) == expected
