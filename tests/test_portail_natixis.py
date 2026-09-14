"""Chaîne complète contre un portail calqué sur NATIXIS Algérie.

Ce portail reproduit les particularités relevées dans les journaux d'un essai
réel, celles-là mêmes qui ont fait échouer les premières tentatives :

- un sous-menu qui ne s'ouvre qu'au clic sur son entrée parente, alors que
  l'enregistreur ne capte pas le geste d'ouverture ;
- une période choisie dans un calendrier plutôt que tapée au clavier, dans des
  champs verrouillés que le calendrier remplit sans émettre d'événement ;
- un refus affiché sur la page quand la période demandée ne contient rien ;
- une connexion en deux temps, avec redirection vers la liste des comptes.
"""

import sys
from datetime import date
from pathlib import Path

import pytest
from conftest import NAVIGATEUR_DISPONIBLE

from bankextract.config import BankConfig, OtpConfig
from bankextract.recorder import ScenarioConnector, record_scenario
from bankextract.recorder.scenario import (
    TOKEN_END,
    TOKEN_PASSWORD,
    TOKEN_START,
    ActionType,
    Step,
)

sys.path.append(str(Path(__file__).parent / "fixtures" / "portail_natixis"))
from portail import (  # noqa: E402
    COMPTE,
    IDENTIFIANT,
    MOT_DE_PASSE,
    OPERATIONS,
    PERIODE_PAR_DEFAUT,
    REFUS_PERIODE_VIDE,
    demarrer_portail,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not NAVIGATEUR_DISPONIBLE, reason="aucun navigateur installé"),
]


@pytest.fixture
def portail():
    serveur, url = demarrer_portail()
    try:
        yield url
    finally:
        serveur.shutdown()
        serveur.server_close()


def _parcours(page):
    """La suite de gestes relevée dans le journal d'un essai réel."""
    page.fill("#login", IDENTIFIANT)
    page.fill("#mdpAffiche", MOT_DE_PASSE)
    page.click("input[type=submit]")
    page.wait_for_selector("#menuPrincipal")
    page.click("#entreeComptes")
    page.click("#menuPrincipal > li:nth-of-type(2) > ul > li:nth-of-type(3) > a")
    page.wait_for_selector("#formatPdf")
    page.click("#formatPdf")
    page.click("#formatCsv")
    page.select_option("#choixFormat", "csv")
    # La période se choisit au calendrier : deux icônes, deux cases cliquées.
    # Le portail écrit alors la date dans un champ verrouillé, sans émettre le
    # moindre événement — d'où l'attente, le temps que le veilleur la relève.
    page.click("#ouvrirCalendrier")
    page.click("table.calendrier td:nth-of-type(1)")
    page.wait_for_timeout(500)
    page.click("#ouvrirCalendrierFin")
    page.click("text='30'")
    page.wait_for_timeout(500)
    with page.expect_download():
        page.click("input[type=submit]")
    page.wait_for_timeout(300)


@pytest.fixture
def parcours_enregistre(settings, portail, tmp_path):
    scenario = record_scenario(
        "natixis", portail, settings.browser, label="NATIXIS - MM",
        headless=True, driver=_parcours,
    )
    return scenario, scenario.save(tmp_path / "scenarios" / "natixis.json")


@pytest.fixture
def identifiants(monkeypatch):
    monkeypatch.setenv("BANKEXTRACT_NATIXIS_USERNAME", IDENTIFIANT)
    monkeypatch.setenv("BANKEXTRACT_NATIXIS_PASSWORD", MOT_DE_PASSE)


def _rejouer(settings, chemin, debut=date(2026, 1, 1), fin=date(2026, 12, 31)):
    connecteur = ScenarioConnector(
        config=BankConfig(
            connector="scenario",
            label="NATIXIS - MM",
            history_days=400,
            options={"scenario_path": str(chemin), "account_number": COMPTE},
            otp=OtpConfig(provider="manual"),
        ),
        settings=settings,
    )
    connecteur.name = "natixis"
    return connecteur.run(start=debut, end=fin)


def _version_ancienne(scenario, chemin):
    """Le parcours tel que l'enregistreur le produisait avant correction.

    Le choix de la période n'y laisse que des clics : la date écrite par le
    calendrier, faute d'événement, n'était captée par personne. C'est le fichier
    que possèdent les utilisateurs ayant enregistré leur parcours jusqu'ici.
    """
    ouvreurs = ["#ouvrirCalendrier", "#ouvrirCalendrierFin"]
    etapes: list[Step] = []
    for step in scenario.steps:
        if step.value in (TOKEN_START, TOKEN_END):
            ouvreur = ouvreurs.pop(0) if ouvreurs else "#ouvrirCalendrier"
            etapes.append(
                Step(action=ActionType.CLICK, selectors=[ouvreur], label="i « calendrier »")
            )
            etapes.append(
                Step(
                    action=ActionType.CLICK,
                    selectors=["table.calendrier td:nth-of-type(1)"],
                    label="td « 1 »",
                )
            )
            continue
        etapes.append(step)

    ancien = scenario.model_copy(update={"steps": etapes})
    return ancien, ancien.save(chemin)


def test_le_parcours_complet_est_capte(parcours_enregistre):
    scenario, _ = parcours_enregistre

    assert len(scenario.downloads) == 1, "le téléchargement doit être reconnu"
    assert any(step.value == TOKEN_PASSWORD for step in scenario.steps)
    assert any(step.action is ActionType.SELECT for step in scenario.steps), "le choix du format"


def test_aucun_secret_sur_le_disque(parcours_enregistre):
    scenario, chemin = parcours_enregistre

    assert MOT_DE_PASSE not in chemin.read_text(encoding="utf-8")
    assert scenario.contains_secret_values() == []


def test_le_rejeu_va_jusqu_au_releve(settings, parcours_enregistre, identifiants):
    """Le cas qui bloquait : le sous-menu doit être rouvert sans qu'on l'ait enregistré."""
    _, chemin = parcours_enregistre

    resultat = _rejouer(settings, chemin)

    assert resultat.errors == [], resultat.errors
    assert len(resultat.files) == 1
    assert resultat.files[0].path.exists()


def test_les_ecritures_sont_lues(settings, parcours_enregistre, identifiants):
    _, chemin = parcours_enregistre

    resultat = _rejouer(settings, chemin)

    assert len(resultat.transactions) == 4
    par_libelle = {t.label: t for t in resultat.transactions}
    assert par_libelle["VIR RECU CLIENT SPA PHARMA"].amount > 0
    assert par_libelle["FRAIS TENUE DE COMPTE"].amount < 0


def test_le_sous_menu_est_rouvert_meme_sans_geste_enregistre(
    settings, portail, tmp_path, identifiants
):
    """Sans l'ouverture du menu, le lien reste invisible et le rejeu échoue.

    On retire volontairement du parcours le clic qui déploie le menu, pour
    reproduire un enregistrement où l'utilisateur l'avait ouvert d'un survol.
    """
    scenario = record_scenario(
        "natixis", portail, settings.browser, label="NATIXIS - MM",
        headless=True, driver=_parcours,
    )
    scenario.steps = [
        step for step in scenario.steps if "Mes comptes" not in (step.label or "")
    ]
    chemin = scenario.save(tmp_path / "sans-ouverture.json")

    resultat = _rejouer(settings, chemin)

    assert resultat.errors == [], resultat.errors
    assert len(resultat.transactions) == 4


def test_relance_sans_doublon(settings, parcours_enregistre, identifiants):
    from bankextract.storage import Database

    _, chemin = parcours_enregistre
    base = Database(settings.paths.database_url)

    premier = base.save_result(_rejouer(settings, chemin))
    second = base.save_result(_rejouer(settings, chemin))

    assert premier.new_transactions == 4
    assert second.new_transactions == 0
    assert second.duplicate_transactions == 4


def test_exports_produits(settings, parcours_enregistre, identifiants):
    from bankextract.pipeline import export_result

    _, chemin = parcours_enregistre
    resultat = _rejouer(settings, chemin)

    exports = export_result(resultat, settings)

    assert {chemin.suffix for chemin in exports} == {".csv", ".xlsx"}
    assert all(export.exists() and export.stat().st_size > 0 for export in exports)


def test_mot_de_passe_refuse_signale_clairement(settings, parcours_enregistre, monkeypatch):
    monkeypatch.setenv("BANKEXTRACT_NATIXIS_USERNAME", IDENTIFIANT)
    monkeypatch.setenv("BANKEXTRACT_NATIXIS_PASSWORD", "mauvais")
    _, chemin = parcours_enregistre

    resultat = _rejouer(settings, chemin)

    assert not resultat.ok
    message = " ".join(resultat.errors)
    assert "introuvable" in message
    assert "page affichée" in message, "l'adresse montre qu'on est resté sur la connexion"


# --------------------------------------------------------------- la période


def test_la_date_ecrite_par_le_calendrier_devient_un_jeton(parcours_enregistre):
    """Le défaut qui rendait toute programmation vaine.

    Le portail écrit la date dans un champ verrouillé sans émettre d'événement.
    Ne restaient donc du choix de la période que des clics sur des cases — or
    une case n'est pas une date, mais une position dans le mois affiché :
    rejouée, elle en désigne une autre. L'utilisateur avait choisi du 1er au 30
    septembre ; l'extraction a demandé le 31 août, deux fois.
    """
    scenario, _ = parcours_enregistre

    valeurs = [step.value for step in scenario.steps]
    assert TOKEN_START in valeurs, "le début de période doit être un jeton"
    assert TOKEN_END in valeurs, "la fin de période doit être un jeton"
    assert not scenario.periode_figee, "la période doit suivre la date d'exécution"
    assert scenario.clics_de_calendrier == [], (
        "les clics qui n'ont servi qu'à remplir le champ doivent être oubliés : "
        "rejoués, ils rouvriraient un calendrier par-dessus la page"
    )


def test_le_releve_couvre_la_periode_demandee(settings, parcours_enregistre, identifiants):
    """La période demandée l'emporte sur celle qui a été enregistrée."""
    _, chemin = parcours_enregistre

    resultat = _rejouer(settings, chemin, debut=date(2026, 3, 2), fin=date(2026, 3, 3))

    assert resultat.errors == [], resultat.errors
    libelles = {t.label for t in resultat.transactions}
    assert libelles == {"REGLEMENT FOURNISSEUR IMPORT", "FRAIS TENUE DE COMPTE"}, (
        "seules les écritures de la période demandée doivent revenir"
    )


def test_un_parcours_enregistre_avant_correction_retrouve_la_bonne_periode(
    settings, parcours_enregistre, identifiants, tmp_path
):
    """Les parcours déjà enregistrés doivent marcher sans être refaits.

    Celui-ci ne porte aucune date : rejoué tel quel, il cliquerait deux fois la
    même case et demanderait une journée vide. Les champs de date sont donc
    renseignés d'office avant le téléchargement.
    """
    scenario, _ = parcours_enregistre
    _, chemin = _version_ancienne(scenario, tmp_path / "scenarios" / "ancien.json")

    resultat = _rejouer(settings, chemin)

    assert resultat.errors == [], resultat.errors
    assert len(resultat.transactions) == 4


def test_une_periode_sans_ecriture_est_expliquee_par_la_banque(
    settings, parcours_enregistre, identifiants
):
    """Un fichier qui n'arrive pas n'est pas une panne : c'est une réponse.

    C'est ce qu'a vécu l'utilisateur — « Timeout 45000ms exceeded while waiting
    for event "download" » — là où le portail affichait, en toutes lettres, la
    raison de son refus.
    """
    _, chemin = parcours_enregistre
    settings.browser.timeout_ms = 4_000

    resultat = _rejouer(settings, chemin, debut=date(2026, 8, 1), fin=date(2026, 8, 31))

    rapport = " ".join(resultat.errors)
    assert REFUS_PERIODE_VIDE in rapport, rapport
    assert "TimeoutError" not in rapport, (
        "le délai expiré n'explique rien ; la phrase de la banque, si"
    )


def test_la_periode_par_defaut_du_portail_ne_contient_rien(settings, parcours_enregistre):
    """Garde-fou du scénario de test : la valeur initiale des champs est vide d'écritures.

    Si elle cessait de l'être, les deux tests précédents passeraient sans rien
    prouver — le relevé arriverait quoi qu'il advienne des champs de date.
    """
    assert PERIODE_PAR_DEFAUT == "31/08/2026"
    assert all(not operation[0].endswith("/08/2026") for operation in OPERATIONS)

