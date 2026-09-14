"""Interface graphique locale."""

import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from bankextract.app import creer_application
from bankextract.app.tasks import Etat, Executeur
from bankextract.config import load_settings, local_overlay_path


@pytest.fixture
def config_path(tmp_path):
    chemin = tmp_path / "banks.yaml"
    chemin.write_text(
        yaml.safe_dump(
            {
                "banks": {},
                "paths": {
                    "data_dir": str(tmp_path / "data"),
                    "downloads_dir": str(tmp_path / "data" / "dl"),
                    "exports_dir": str(tmp_path / "data" / "ex"),
                    "logs_dir": str(tmp_path / "logs"),
                    "database_url": f"sqlite:///{tmp_path}/t.db",
                },
            }
        ),
        encoding="utf-8",
    )
    return chemin


@pytest.fixture
def client(config_path):
    return TestClient(creer_application(config_path))


def _ajouter(client, **champs):
    defauts = {
        "nom": "bna",
        "libelle": "BNA — compte SARL",
        "url": "https://ebanking.bna.dz",
        "numero_compte": "00100 987654321 09",
        "devise": "DZD",
        "source_otp": "manual",
        "historique_jours": 90,
    }
    return client.post("/api/banques", json={**defauts, **champs})


# ---------------------------------------------------------------- page et état


def test_page_s_affiche(client):
    reponse = client.get("/")

    assert reponse.status_code == 200
    assert "Ajouter une banque" in reponse.text
    assert "Voir les relevés" in reponse.text


def test_manual_propose_en_premier(client):
    """Le seul mode qui fonctionne sans matériel doit être le choix par défaut."""
    page = client.get("/").text
    debut = page.index('id="f-otp"')

    premier = page[debut : debut + 400].split('<option value="')[1].split('"')[0]

    assert premier == "manual"


def test_etat_initial_vide(client):
    etat = client.get("/api/etat").json()

    assert etat["banques"] == []
    assert etat["operation"] is None
    assert etat["occupe"] is False


def test_point_de_sante(client):
    assert client.get("/api/sante").json()["statut"] == "ok"


def test_tableau_de_bord_accessible(client):
    """Les relevés se consultent depuis la même fenêtre."""
    assert client.get("/releves/").status_code == 200


# ---------------------------------------------------------------- banques


def test_ajout_d_une_banque(client):
    reponse = _ajouter(client)

    assert reponse.status_code == 200
    assert reponse.json()["cle"] == "bna"
    banques = client.get("/api/etat").json()["banques"]
    assert len(banques) == 1
    assert banques[0]["libelle"] == "BNA — compte SARL"
    assert banques[0]["pilotee_par_parcours"] is True


def test_nom_normalise(client):
    """Un nom saisi avec accents et espaces doit donner une clé utilisable."""
    reponse = _ajouter(client, nom="BNA Société Générale")

    assert reponse.json()["cle"] == "bna_societe_generale"


def test_reglages_ecrits_a_cote_du_modele(client, config_path):
    """Le modèle documenté ne doit jamais être réécrit par l'interface."""
    modele_avant = config_path.read_text(encoding="utf-8")

    _ajouter(client)

    assert config_path.read_text(encoding="utf-8") == modele_avant
    surcouche = local_overlay_path(config_path)
    assert surcouche.exists()
    contenu = yaml.safe_load(surcouche.read_text(encoding="utf-8"))
    assert contenu["banks"]["bna"]["options"]["base_url"] == "https://ebanking.bna.dz"


def test_surcouche_relue_par_la_configuration(client, config_path):
    _ajouter(client, historique_jours=45)

    settings = load_settings(config_path)

    assert settings.banks["bna"].history_days == 45
    assert settings.banks["bna"].connector == "scenario"


def test_modification_conserve_les_champs_absents(client, config_path):
    """Modifier le libellé ne doit pas effacer ce qui n'est pas dans le formulaire."""
    _ajouter(client)
    surcouche = local_overlay_path(config_path)
    contenu = yaml.safe_load(surcouche.read_text(encoding="utf-8"))
    contenu["banks"]["bna"]["options"]["parse_downloads"] = False
    surcouche.write_text(yaml.safe_dump(contenu), encoding="utf-8")

    _ajouter(client, libelle="BNA — nouveau nom")

    relu = yaml.safe_load(surcouche.read_text(encoding="utf-8"))
    assert relu["banks"]["bna"]["label"] == "BNA — nouveau nom"
    assert relu["banks"]["bna"]["options"]["parse_downloads"] is False


def test_suppression(client):
    _ajouter(client)

    assert client.delete("/api/banques/bna").status_code == 200
    assert client.get("/api/etat").json()["banques"] == []


def test_suppression_d_une_banque_absente(client):
    assert client.delete("/api/banques/fantome").status_code == 404


def test_identifiants_d_une_banque_inconnue(client):
    reponse = client.post(
        "/api/identifiants",
        json={"banque": "fantome", "utilisateur": "u", "mot_de_passe": "p"},
    )

    assert reponse.status_code == 404


def test_url_obligatoire_pour_enregistrer_un_parcours(client):
    _ajouter(client, url="")

    reponse = client.post("/api/operations/enregistrer/bna")

    assert reponse.status_code == 422 or reponse.status_code == 400


# ---------------------------------------------------------------- opérations


def _attendre_fin(client, secondes=5.0):
    limite = time.monotonic() + secondes
    while time.monotonic() < limite:
        etat = client.get("/api/etat").json()
        if not etat["occupe"] and etat["operation"]:
            return etat["operation"]
        time.sleep(0.1)
    raise AssertionError("opération toujours en cours")


def test_extraction_sans_parcours_echoue_lisiblement(client):
    """Le message doit dire quoi faire, pas exposer une trace technique."""
    _ajouter(client)

    assert client.post("/api/operations/extraire/bna").status_code == 200
    operation = _attendre_fin(client)

    assert operation["etat"] == Etat.ECHEC.value
    assert "Scénario" in operation["erreur"]


def test_operation_sur_banque_inconnue(client):
    assert client.post("/api/operations/extraire/fantome").status_code == 404


def test_une_seule_operation_a_la_fois():
    """Deux extractions simultanées se disputeraient le navigateur et la base."""
    executeur = Executeur()
    executeur.lancer("longue", lambda operation: time.sleep(0.6) or "fini")

    with pytest.raises(RuntimeError, match="déjà en cours"):
        executeur.lancer("autre", lambda operation: "fini")


def test_journal_consultable_pendant_l_execution():
    import logging

    executeur = Executeur()

    def travail(operation):
        logging.getLogger("bankextract.essai").info("première étape")
        time.sleep(0.2)
        return "terminé"

    operation = executeur.lancer("essai", travail)
    time.sleep(0.5)

    assert operation.etat is Etat.TERMINE
    assert "première étape" in list(operation.lignes)
    assert operation.resume == "terminé"


def test_echec_conserve_le_message(client):
    executeur = Executeur()

    def travail(operation):
        raise ValueError("portail injoignable")

    operation = executeur.lancer("essai", travail)
    time.sleep(0.3)

    assert operation.etat is Etat.ECHEC
    assert "portail injoignable" in operation.erreur


# ---------------------------------------------------------------- préparation du poste


def test_etat_signale_le_navigateur(client):
    """L'interface doit savoir si le navigateur est installé, sans le démarrer."""
    assert "navigateur_pret" in client.get("/api/etat").json()


def test_bandeau_de_preparation_present_dans_la_page(client):
    assert "Préparer le poste" in client.get("/").text


def test_preparation_ne_retelecharge_pas_un_navigateur_present(client, monkeypatch):
    monkeypatch.setattr("bankextract.app.server.browser_installed", lambda config: True)

    assert client.post("/api/operations/preparer").status_code == 200
    operation = _attendre_fin(client)

    assert operation["etat"] == Etat.TERMINE.value
    assert "déjà installé" in operation["resume"]


def test_detection_du_navigateur_sans_demarrer_playwright(tmp_path):
    """Un chemin explicite fait foi : Playwright l'utilisera tel quel."""
    from bankextract.browser import browser_installed
    from bankextract.config import BrowserConfig

    faux = tmp_path / "chrome.exe"
    faux.write_bytes(b"")

    assert browser_installed(BrowserConfig(executable_path=str(faux))) is True
    assert browser_installed(BrowserConfig(executable_path=str(tmp_path / "absent"))) is False


def test_detection_parcourt_le_dossier_des_navigateurs(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    from bankextract.browser import browser_installed

    assert browser_installed() is False

    chrome = tmp_path / "chromium-1194" / "chrome-linux" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")

    assert browser_installed() is True


# ---------------------------------------------------------------- banques du modèle


@pytest.fixture
def config_avec_modele(tmp_path):
    """Modèle livré : des banques d'exemple, toutes désactivées."""
    chemin = tmp_path / "banks.yaml"
    chemin.write_text(
        yaml.safe_dump(
            {
                "banks": {
                    "bna": {"connector": "bna", "enabled": False},
                    "cpa": {"connector": "cpa", "enabled": False},
                    "active": {"connector": "generic", "enabled": True},
                },
                "paths": {"database_url": f"sqlite:///{tmp_path}/t.db"},
            }
        ),
        encoding="utf-8",
    )
    return chemin


def test_les_exemples_du_modele_n_encombrent_pas_la_liste(config_avec_modele):
    """Sept banques d'exemple affichées noieraient celles de l'utilisateur."""
    client = TestClient(creer_application(config_avec_modele))

    cles = [b["cle"] for b in client.get("/api/etat").json()["banques"]]

    assert "bna" not in cles and "cpa" not in cles
    assert cles == ["active"], "seules les banques actives restent visibles"


def test_une_banque_du_modele_peut_etre_masquee(config_avec_modele):
    """On ne peut pas l'effacer d'un fichier qui ne nous appartient pas."""
    client = TestClient(creer_application(config_avec_modele))

    reponse = client.delete("/api/banques/active")

    assert reponse.status_code == 200
    assert "masquée" in reponse.json()["message"]
    assert client.get("/api/etat").json()["banques"] == []


def test_banque_vraiment_inexistante(config_avec_modele):
    client = TestClient(creer_application(config_avec_modele))

    assert client.delete("/api/banques/fantome").status_code == 404


def test_banque_ajoutee_puis_retiree_disparait(config_avec_modele):
    client = TestClient(creer_application(config_avec_modele))
    client.post(
        "/api/banques",
        json={
            "nom": "natixis",
            "libelle": "NATIXIS - MM",
            "url": "https://ebanking.algerie.natixis.com/ebanking/index.ebk",
            "numero_compte": "00167 7764082001 45",
            "devise": "DZD",
            "source_otp": "manual",
            "historique_jours": 90,
        },
    )

    assert "natixis" in [b["cle"] for b in client.get("/api/etat").json()["banques"]]
    assert "retirée" in client.delete("/api/banques/natixis").json()["message"]
    assert "natixis" not in [b["cle"] for b in client.get("/api/etat").json()["banques"]]


def test_bouton_de_verification_present(client):
    assert "Vérifier l'installation" in client.get("/").text


def test_diagnostic_sans_navigateur(client, monkeypatch):
    monkeypatch.setattr("bankextract.app.server.browser_installed", lambda config: False)

    assert client.post("/api/operations/diagnostic").status_code == 200
    operation = _attendre_fin(client)

    assert operation["etat"] == Etat.ECHEC.value
    assert "Préparer le poste" in operation["erreur"]


# ---------------------------------------------------------------- fin et réglages


def test_terminer_sans_enregistrement(client):
    assert client.post("/api/operations/terminer").status_code == 409


def test_reglages_exposes(client):
    reglages = client.get("/api/etat").json()["reglages"]

    assert "navigateur" in reglages and "navigateur_interface" in reglages


def test_reglages_enregistres(client, config_path):
    reponse = client.post(
        "/api/reglages", json={"navigateur": "chrome", "navigateur_interface": "chrome"}
    )

    assert reponse.status_code == 200
    settings = load_settings(config_path)
    assert settings.browser.channel == "chrome"
    assert settings.browser.interface_browser == "chrome"


def test_navigateur_fourni_par_defaut(client, config_path):
    client.post("/api/reglages", json={"navigateur": "", "navigateur_interface": ""})

    assert load_settings(config_path).browser.channel is None


def test_bouton_j_ai_termine_present(client):
    assert "J'ai terminé" in client.get("/").text


def test_formulaire_de_reglages_present(client):
    page = client.get("/").text

    assert "Navigateur à piloter" in page and "Google Chrome" in page


# ---------------------------------------------------------------- programmations


@pytest.fixture
def client_programmation(tmp_path, config_path):
    (tmp_path / "schedules.yaml").write_text(
        yaml.safe_dump({"smtp": {"host": ""}, "schedules": []}), encoding="utf-8"
    )
    return TestClient(
        creer_application(
            config_path,
            schedules_path=tmp_path / "schedules.yaml",
            state_path=tmp_path / "etat.json",
            programmateur=False,
        )
    )


def _programmer(client, **champs):
    defauts = {
        "nom": "releve-quotidien",
        "banque": "",
        "frequence": "quotidien",
        "heure": "20:00",
        "periode": "depuis_derniere_execution",
        "jours": 30,
    }
    return client.post("/api/programmations", json={**defauts, **champs})


def test_extraction_quotidienne(client_programmation):
    """« tous les jours à 20h »."""
    reponse = _programmer(client_programmation)

    assert reponse.status_code == 200
    assert "chaque jour à 20:00" in reponse.json()["message"]


def test_extraction_hebdomadaire(client_programmation):
    """« tous les vendredis à 18h30 »."""
    reponse = _programmer(
        client_programmation,
        nom="point-hebdo",
        frequence="hebdomadaire",
        jour_semaine=4,
        heure="18:30",
    )

    assert "chaque vendredi à 18:30" in reponse.json()["message"]


def test_extraction_mensuelle(client_programmation):
    reponse = _programmer(
        client_programmation, nom="mensuel", frequence="mensuel", jour_mois=1, heure="06:00"
    )

    assert "le 1 de chaque mois" in reponse.json()["message"]


def test_prochaine_echeance_affichee(client_programmation):
    _programmer(client_programmation)

    programmations = client_programmation.get("/api/etat").json()["programmations"]

    assert len(programmations) == 1
    assert programmations[0]["prochaine"]
    assert programmations[0]["derniere"] is None


def test_destinataire_facultatif(client_programmation):
    _programmer(client_programmation, destinataire="compta@exemple.dz")

    programmation = client_programmation.get("/api/etat").json()["programmations"][0]

    assert programmation["destinataires"] == ["compta@exemple.dz"]


def test_sans_destinataire_pas_d_envoi(client_programmation):
    _programmer(client_programmation)

    assert client_programmation.get("/api/etat").json()["programmations"][0]["destinataires"] == []


def test_reprogrammer_remplace(client_programmation):
    """Deux fois le même nom ne doit pas créer deux programmations."""
    _programmer(client_programmation, heure="20:00")
    _programmer(client_programmation, heure="07:00")

    programmations = client_programmation.get("/api/etat").json()["programmations"]

    assert len(programmations) == 1
    assert "07:00" in programmations[0]["recurrence"]


def test_suppression_d_une_programmation(client_programmation):
    _programmer(client_programmation)

    assert client_programmation.delete("/api/programmations/releve-quotidien").status_code == 200
    assert client_programmation.get("/api/etat").json()["programmations"] == []


def test_suppression_d_une_programmation_absente(client_programmation):
    assert client_programmation.delete("/api/programmations/fantome").status_code == 404


def test_nom_deduit_si_absent(client_programmation):
    reponse = _programmer(client_programmation, nom="", banque="", frequence="hebdomadaire")

    assert reponse.json()["nom"] == "toutes-hebdomadaire"


def test_heure_illisible_refusee(client_programmation):
    reponse = _programmer(client_programmation, heure="midi")

    assert reponse.status_code in (400, 422)


def test_formulaire_de_programmation_present(client_programmation):
    page = client_programmation.get("/").text

    assert "Extractions programmées" in page
    assert "Toutes les semaines" in page and "Vendredi" in page


# ---------------------------------------------------------------- diagnostic


def test_version_affichee(client):
    """Sans elle, impossible de savoir si un correctif est en place sur le poste."""
    etat = client.get("/api/etat").json()

    assert etat["version"]
    assert "id=\"version\"" in client.get("/").text


def test_capture_absente(client):
    assert client.get("/api/capture").status_code == 404
    assert client.get("/api/etat").json()["capture"] is False


def test_capture_servie(client, tmp_path):
    captures = tmp_path / "logs" / "screenshots"
    captures.mkdir(parents=True, exist_ok=True)
    (captures / "erreur.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    assert client.get("/api/etat").json()["capture"] is True
    reponse = client.get("/api/capture")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "image/png"


def test_capture_la_plus_recente(client, tmp_path):
    import os
    import time

    captures = tmp_path / "logs" / "screenshots"
    captures.mkdir(parents=True, exist_ok=True)
    ancienne = captures / "ancienne.png"
    ancienne.write_bytes(b"ancienne")
    time.sleep(0.01)
    recente = captures / "recente.png"
    recente.write_bytes(b"recente")
    os.utime(ancienne, (1, 1))

    assert client.get("/api/capture").content == b"recente"


def test_periode_figee_signalee_sur_la_fiche(client, config_path, tmp_path):
    """L'utilisateur doit le savoir avant de programmer des extractions."""
    import json

    _ajouter(client)
    parcours = Path("scenarios") / "bna.json"
    parcours.parent.mkdir(parents=True, exist_ok=True)
    parcours.write_text(
        json.dumps(
            {
                "bank": "bna",
                "steps": [
                    {"action": "click", "selectors": ["td"], "label": "td « 1 »"},
                    {"action": "click", "selectors": ["td"], "label": "td « 30 »"},
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        banque = client.get("/api/etat").json()["banques"][0]
    finally:
        parcours.unlink(missing_ok=True)

    assert banque["periode_figee"] is True


def test_mention_de_periode_figee_dans_la_page(client):
    assert "Période figée" in client.get("/").text
