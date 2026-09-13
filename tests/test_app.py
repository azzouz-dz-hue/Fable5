"""Interface graphique locale."""

import time

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
