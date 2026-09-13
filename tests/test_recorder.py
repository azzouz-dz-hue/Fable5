"""Enregistrement d'un parcours bancaire, puis rejeu automatique."""

import json
from datetime import date

import pytest
from conftest import CHROMIUM

from bankextract.config import BankConfig, OtpConfig
from bankextract.recorder import ScenarioConnector, record_scenario
from bankextract.recorder.record import RecordingSession, annotate_scenario
from bankextract.recorder.scenario import (
    TOKEN_END,
    TOKEN_OTP,
    TOKEN_PASSWORD,
    TOKEN_START,
    TOKEN_USERNAME,
    ActionType,
    Scenario,
    Step,
)

MOT_DE_PASSE = "demo123"


# ---------------------------------------------------------------- format et annotation


def _event(**kwargs) -> str:
    return json.dumps(kwargs)


def test_le_mot_de_passe_est_remplace_des_la_reception():
    """La valeur ne doit jamais entrer dans le scénario, même en mémoire."""
    session = RecordingSession()

    session.add_event(_event(action="fill", selectors=["#pass"], secret=True, value=None))

    assert session.steps[0].value == TOKEN_PASSWORD


def test_saisie_corrigee_ne_cree_qu_une_etape():
    """L'utilisateur se trompe et retape : seule la valeur finale compte."""
    session = RecordingSession()

    session.add_event(_event(action="fill", selectors=["#user"], value="dem"))
    session.add_event(_event(action="fill", selectors=["#user"], value="demo"))

    assert len(session.steps) == 1
    assert session.steps[0].value == "demo"


def test_evenement_illisible_ignore():
    session = RecordingSession()

    session.add_event("{ pas du JSON")
    session.add_event(_event(action="danser", selectors=["#x"]))

    assert session.steps == []


def test_le_clic_precedant_un_telechargement_est_requalifie():
    session = RecordingSession()
    session.add_event(_event(action="click", selectors=["a.dl"], label="a « Télécharger »"))

    session.mark_last_click_as_download()

    assert session.steps[0].action is ActionType.DOWNLOAD


def test_identifiant_deduit_du_champ_precedant_le_mot_de_passe():
    scenario = Scenario(
        bank="x",
        steps=[
            Step(action=ActionType.FILL, selectors=["#login"], value="medico"),
            Step(action=ActionType.FILL, selectors=["#pass"], value=TOKEN_PASSWORD),
        ],
    )

    annotate_scenario(scenario)

    assert scenario.steps[0].value == TOKEN_USERNAME


def test_code_otp_deduit_et_marque_facultatif():
    """L'OTP n'est pas redemandé sur un appareil déjà reconnu : l'étape doit
    pouvoir être sautée sans faire échouer le rejeu."""
    scenario = Scenario(
        bank="x",
        steps=[
            Step(action=ActionType.FILL, selectors=["#user"], value="medico"),
            Step(action=ActionType.FILL, selectors=["#pass"], value=TOKEN_PASSWORD),
            Step(action=ActionType.FILL, selectors=["#otp-code"], value="483920"),
        ],
    )

    annotate_scenario(scenario)

    assert scenario.steps[2].value == TOKEN_OTP
    assert scenario.steps[2].optional
    assert scenario.uses_otp


def test_dates_remplacees_par_des_jetons_de_periode():
    """Sans cela, le rejeu redemanderait éternellement la même période."""
    scenario = Scenario(
        bank="x",
        steps=[
            Step(action=ActionType.FILL, selectors=["#au"], value="28/02/2026"),
            Step(action=ActionType.FILL, selectors=["#du"], value="01/02/2026"),
        ],
    )

    annotate_scenario(scenario)

    assert scenario.steps[1].value == TOKEN_START, "la date la plus ancienne est le début"
    assert scenario.steps[0].value == TOKEN_END
    assert scenario.uses_period


def test_date_unique_traitee_comme_fin_de_periode():
    scenario = Scenario(
        bank="x",
        steps=[Step(action=ActionType.FILL, selectors=["#date"], value="28/02/2026")],
    )

    annotate_scenario(scenario)

    assert scenario.steps[0].value == TOKEN_END


def test_detecteur_de_secret_oublie():
    """Filet de sécurité : un champ de mot de passe porteur d'une vraie valeur."""
    scenario = Scenario(
        bank="x",
        steps=[Step(action=ActionType.FILL, selectors=["#password"], value="motdepasse")],
    )

    assert scenario.contains_secret_values()


def test_etape_secrete_jamais_affichee_en_clair():
    step = Step(action=ActionType.FILL, selectors=["#pass"], value=TOKEN_PASSWORD)

    assert "•••" in step.describe()


def test_enregistrement_relu_a_l_identique(tmp_path):
    scenario = Scenario(
        bank="bna",
        label="BNA",
        steps=[Step(action=ActionType.GOTO, url="https://x.dz"), Step(action=ActionType.CLICK)],
    )

    chemin = scenario.save(tmp_path / "bna.json")

    assert Scenario.load(chemin).steps == scenario.steps


def test_scenario_absent_indique_la_marche_a_suivre(tmp_path):
    with pytest.raises(FileNotFoundError, match="bankextract record"):
        Scenario.load(tmp_path / "absent.json")


# ---------------------------------------------------------------- parcours complet

def navigateur(test):
    """Marque un test qui pilote un vrai navigateur.

    Il porte le marqueur « e2e » (pour « pytest -m "not e2e" ») et se saute de
    lui-même là où aucun Chromium n'est installé.
    """
    test = pytest.mark.skipif(
        CHROMIUM is None, reason="Chromium introuvable — définissez BANKEXTRACT_CHROMIUM"
    )(test)
    return pytest.mark.e2e(test)


def _parcours_utilisateur(otp_file):
    """Reproduit ce qu'un utilisateur ferait, une seule fois, dans son portail."""

    def driver(page):
        page.fill("#username", "demo")
        page.fill("#password", MOT_DE_PASSE)
        page.click("#login-submit")
        page.wait_for_selector("#otp")
        code = otp_file.read_text(encoding="utf-8").split("est ")[1].split(".")[0]
        page.fill("#otp", code)
        page.click("#otp-submit")
        page.wait_for_selector(".dashboard")
        page.click("table.accounts tbody tr:nth-of-type(1) td.num a")
        page.wait_for_selector("table.operations")
        page.click('text="Relevés"')
        page.wait_for_selector("table.statements")
        with page.expect_download():
            page.click("table.statements tbody tr:nth-of-type(1) a.dl-csv")
        page.wait_for_timeout(300)

    return driver


@pytest.fixture
def parcours_enregistre(settings, fake_bank, tmp_path):
    """Enregistre une fois le parcours contre le faux portail."""
    url, otp_file = fake_bank
    scenario = record_scenario(
        "demo",
        url,
        settings.browser,
        label="Banque Demo",
        headless=True,
        driver=_parcours_utilisateur(otp_file),
    )
    return scenario, scenario.save(tmp_path / "scenarios" / "demo.json"), url, otp_file


@navigateur
def test_le_parcours_est_capte_dans_l_ordre(parcours_enregistre):
    scenario, _, _, _ = parcours_enregistre

    actions = [step.action for step in scenario.steps]

    assert actions[0] is ActionType.GOTO
    assert ActionType.DOWNLOAD in actions, "le clic de téléchargement doit être reconnu"
    assert len(scenario.downloads) == 1
    assert scenario.uses_otp


@navigateur
def test_aucun_secret_n_est_ecrit_sur_le_disque(parcours_enregistre):
    """La garantie la plus importante de l'enregistreur."""
    scenario, chemin, _, _ = parcours_enregistre

    contenu = chemin.read_text(encoding="utf-8")

    assert MOT_DE_PASSE not in contenu
    assert scenario.contains_secret_values() == []
    assert any(step.value == TOKEN_PASSWORD for step in scenario.steps)


@navigateur
def test_identifiant_et_otp_reconnus_a_l_enregistrement(parcours_enregistre):
    scenario, _, _, _ = parcours_enregistre

    valeurs = [step.value for step in scenario.steps]

    assert TOKEN_USERNAME in valeurs
    assert TOKEN_OTP in valeurs


@navigateur
def test_plusieurs_selecteurs_par_etape(parcours_enregistre):
    """Un seul sélecteur rendrait le rejeu fragile à la moindre refonte."""
    scenario, _, _, _ = parcours_enregistre

    cliquables = [s for s in scenario.steps if s.action in (ActionType.CLICK, ActionType.DOWNLOAD)]

    assert cliquables, "le parcours doit contenir des clics"
    assert all(len(step.selectors) >= 2 for step in cliquables)


def _config(chemin, otp_file, **options) -> BankConfig:
    return BankConfig(
        connector="scenario",
        label="Banque Demo",
        history_days=400,
        options={
            "scenario_path": str(chemin),
            "account_number": "00100 987654321 09",
            **options,
        },
        otp=OtpConfig(
            provider="file",
            options={"path": str(otp_file)},
            timeout_seconds=20,
            poll_interval_seconds=0.3,
        ),
    )


@pytest.fixture
def identifiants(monkeypatch):
    monkeypatch.setenv("BANKEXTRACT_DEMO_USERNAME", "demo")
    monkeypatch.setenv("BANKEXTRACT_DEMO_PASSWORD", MOT_DE_PASSE)


def _rejouer(settings, chemin, otp_file, **options):
    connector = ScenarioConnector(config=_config(chemin, otp_file, **options), settings=settings)
    connector.name = "demo"
    return connector.run(start=date(2026, 1, 1), end=date(2026, 12, 31))


@navigateur
def test_rejeu_telecharge_le_releve(settings, parcours_enregistre, identifiants):
    _, chemin, _, otp_file = parcours_enregistre

    resultat = _rejouer(settings, chemin, otp_file)

    assert resultat.errors == [], resultat.errors
    assert len(resultat.files) == 1
    fichier = resultat.files[0]
    assert fichier.path.exists() and fichier.path.stat().st_size > 0
    assert fichier.sha256


@navigateur
def test_rejeu_analyse_le_releve_en_ecritures(settings, parcours_enregistre, identifiants):
    """La boucle complète : enregistrer, rejouer, télécharger, puis lire le relevé."""
    _, chemin, _, otp_file = parcours_enregistre

    resultat = _rejouer(settings, chemin, otp_file)

    assert len(resultat.transactions) == 8
    libelles = {t.label for t in resultat.transactions}
    assert "VIR RECU CLIENT SPA PHARMA" in libelles
    credit = next(t for t in resultat.transactions if t.label == "VIR RECU CLIENT SPA PHARMA")
    assert credit.amount > 0


@navigateur
def test_analyse_desactivable(settings, parcours_enregistre, identifiants):
    """Certains n'attendent que l'archivage du PDF officiel."""
    _, chemin, _, otp_file = parcours_enregistre

    resultat = _rejouer(settings, chemin, otp_file, parse_downloads=False)

    assert resultat.files and resultat.transactions == []


@navigateur
def test_selecteur_obsolete_signale_clairement(settings, parcours_enregistre, identifiants):
    """Quand le portail change, le message doit nommer l'étape fautive."""
    scenario, chemin, _, otp_file = parcours_enregistre
    scenario.steps[1].selectors = ["#champ-qui-nexiste-plus"]
    scenario.save(chemin)

    resultat = _rejouer(settings, chemin, otp_file)

    assert not resultat.ok
    message = " ".join(resultat.errors)
    assert "étape 2" in message and "sélecteur" in message


@navigateur
def test_etape_facultative_absente_n_arrete_pas_le_rejeu(
    settings, parcours_enregistre, identifiants
):
    scenario, chemin, _, otp_file = parcours_enregistre
    scenario.steps.insert(
        1,
        Step(
            action=ActionType.CLICK,
            selectors=["#banniere-cookies-absente"],
            optional=True,
            label="bandeau cookies",
        ),
    )
    scenario.save(chemin)

    resultat = _rejouer(settings, chemin, otp_file)

    assert resultat.errors == [], resultat.errors
    assert resultat.files


@navigateur
def test_scenario_manquant_signale_sans_ouvrir_le_navigateur(settings, tmp_path, identifiants):
    resultat = _rejouer(settings, tmp_path / "absent.json", tmp_path / "otp.txt")

    assert not resultat.ok
    assert any("Scénario" in erreur for erreur in resultat.errors)
