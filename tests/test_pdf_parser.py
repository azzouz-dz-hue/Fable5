"""Analyse des relevés PDF.

Les relevés réels prennent deux formes : un vrai tableau, ou du texte aligné
en colonnes que produisent les chaînes d'édition bancaires. Les deux sont
couverts ici, à partir de PDF réellement fabriqués pour le test.
"""

from datetime import date
from decimal import Decimal

import pytest

from bankextract.parsers import parse_statement
from bankextract.parsers.pdf_statement import _select_pages

reportlab = pytest.importorskip("reportlab", reason="reportlab requis pour fabriquer les PDF")

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle  # noqa: E402

COMPTE = "bna:0010098765432109"

LIGNES = [
    ["Date opération", "Date valeur", "Libellé", "Débit", "Crédit", "Solde"],
    ["04/03/2026", "05/03/2026", "VIR RECU CLIENT SPA PHARMA", "", "1 250 000,00", "4 235 890,45"],
    ["03/03/2026", "03/03/2026", "REGLEMENT FOURNISSEUR", "845 300,50", "", "2 985 890,45"],
    ["02/03/2026", "02/03/2026", "FRAIS TENUE DE COMPTE", "1 200,00", "", "3 831 190,95"],
]


def _pdf_avec_tableau(chemin, lignes=None, pages=1):
    """Relevé structuré en tableau — la présentation la plus répandue."""
    document = SimpleDocTemplate(str(chemin), pagesize=A4)
    styles = getSampleStyleSheet()
    contenu = []
    for _ in range(pages):
        tableau = Table(lignes or LIGNES)
        tableau.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        contenu += [Paragraph("Relevé de compte", styles["Title"]), tableau]
    document.build(contenu)
    return chemin


def _pdf_texte_aligne(chemin):
    """Relevé « mainframe » : pas de tableau, des colonnes alignées à la main."""
    feuille = canvas.Canvas(str(chemin), pagesize=A4)
    feuille.setFont("Courier", 9)
    y = 800
    for ligne in [
        "BANQUE NATIONALE D'ALGERIE - RELEVE DE COMPTE",
        "Compte 00100 987654321 09",
        "",
        "04/03/2026 05/03/2026 VIR RECU CLIENT SPA PHARMA   1 250 000,00 C  4 235 890,45",
        "03/03/2026 03/03/2026 REGLEMENT FOURNISSEUR          845 300,50 D  2 985 890,45",
        "02/03/2026 02/03/2026 FRAIS TENUE DE COMPTE            1 200,00 D  3 831 190,95",
        "",
        "TOTAL DES MOUVEMENTS",
    ]:
        feuille.drawString(40, y, ligne)
        y -= 14
    feuille.save()
    return chemin


def test_releve_en_tableau(tmp_path):
    ecritures = parse_statement(_pdf_avec_tableau(tmp_path / "t.pdf"), COMPTE)

    assert len(ecritures) == 3
    assert [e.date for e in ecritures] == [date(2026, 3, 4), date(2026, 3, 3), date(2026, 3, 2)]


def test_signes_issus_des_colonnes_debit_et_credit(tmp_path):
    ecritures = parse_statement(_pdf_avec_tableau(tmp_path / "t.pdf"), COMPTE)

    par_libelle = {e.label: e for e in ecritures}
    assert par_libelle["VIR RECU CLIENT SPA PHARMA"].amount == Decimal("1250000.00")
    assert par_libelle["REGLEMENT FOURNISSEUR"].amount == Decimal("-845300.50")


def test_soldes_et_dates_de_valeur_repris(tmp_path):
    ecritures = parse_statement(_pdf_avec_tableau(tmp_path / "t.pdf"), COMPTE)

    assert ecritures[0].balance_after == Decimal("4235890.45")
    assert ecritures[0].value_date == date(2026, 3, 5)


def test_releve_en_texte_aligne(tmp_path):
    """Sans tableau détectable, l'analyse retombe sur une lecture ligne à ligne."""
    ecritures = parse_statement(_pdf_texte_aligne(tmp_path / "t.pdf"), COMPTE)

    assert len(ecritures) == 3
    assert {e.label for e in ecritures} == {
        "VIR RECU CLIENT SPA PHARMA",
        "REGLEMENT FOURNISSEUR",
        "FRAIS TENUE DE COMPTE",
    }


def test_colonne_de_sens_d_ou_c_respectee(tmp_path):
    ecritures = parse_statement(_pdf_texte_aligne(tmp_path / "t.pdf"), COMPTE)

    par_libelle = {e.label: e for e in ecritures}
    assert par_libelle["VIR RECU CLIENT SPA PHARMA"].amount > 0, "« C » vaut crédit"
    assert par_libelle["FRAIS TENUE DE COMPTE"].amount < 0, "« D » vaut débit"


def test_ligne_de_total_ignoree(tmp_path):
    ecritures = parse_statement(_pdf_texte_aligne(tmp_path / "t.pdf"), COMPTE)

    assert not any("TOTAL" in e.label for e in ecritures)


def test_ligne_repetee_entre_deux_pages_dedupliquee(tmp_path):
    """Un relevé répète parfois une écriture en tête de page suivante."""
    ecritures = parse_statement(_pdf_avec_tableau(tmp_path / "t.pdf", pages=2), COMPTE)

    assert len(ecritures) == 3, "les mêmes écritures ne doivent compter qu'une fois"


def test_pdf_sans_ecriture_ne_leve_pas(tmp_path):
    """Un PDF scanné ne contient aucun texte : il faut le dire, pas planter."""
    chemin = tmp_path / "scanne.pdf"
    feuille = canvas.Canvas(str(chemin), pagesize=A4)
    feuille.drawString(40, 800, "")
    feuille.save()

    assert parse_statement(chemin, COMPTE) == []


def test_devise_transmise(tmp_path):
    ecritures = parse_statement(_pdf_avec_tableau(tmp_path / "t.pdf"), COMPTE, currency="EUR")

    assert all(e.currency == "EUR" for e in ecritures)


@pytest.mark.parametrize(
    "spec, attendu",
    [
        ("1", [1]),
        ("2-4", [2, 3, 4]),
        ("1,3", [1, 3]),
        ("1-2,5", [1, 2, 5]),
        (None, [1, 2, 3, 4, 5]),
    ],
)
def test_selection_des_pages(spec, attendu):
    pages = [1, 2, 3, 4, 5]

    assert _select_pages(pages, spec) == attendu
