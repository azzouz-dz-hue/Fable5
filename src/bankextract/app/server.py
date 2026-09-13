"""Interface graphique locale : le logiciel utilisable sans ligne de commande.

Lancée d'un double-clic, elle ouvre une page dans le navigateur. Tout reste sur
le poste : le serveur n'écoute que sur 127.0.0.1 et n'est joignable de nulle
part ailleurs.
"""

from __future__ import annotations

import logging
import socket
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..config import DEFAULT_CONFIG_PATH, BankConfig, Settings, load_settings, local_overlay_path
from ..connectors import build_connector
from ..otp import available_providers
from ..pipeline import run_banks
from ..secrets import Credentials, get_credentials, store_credentials
from ..storage import Database
from .tasks import Executeur, Operation

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


class DescriptionBanque(BaseModel):
    """Ce que l'utilisateur saisit dans le formulaire."""

    nom: str = Field(min_length=1, max_length=40)
    libelle: str = ""
    url: str = ""
    numero_compte: str = ""
    devise: str = "DZD"
    source_otp: str = "manual"
    historique_jours: int = Field(default=90, ge=1, le=3650)

    def cle(self) -> str:
        """Identifiant technique : minuscules, sans espace ni accent."""
        import unicodedata

        brut = unicodedata.normalize("NFD", self.nom.strip().lower())
        sans_accent = "".join(c for c in brut if unicodedata.category(c) != "Mn")
        return "".join(c if c.isalnum() else "_" for c in sans_accent).strip("_") or "banque"


class Identifiants(BaseModel):
    banque: str
    utilisateur: str = Field(min_length=1)
    mot_de_passe: str = Field(min_length=1)


def creer_application(
    config_path: Path | str = DEFAULT_CONFIG_PATH, settings: Settings | None = None
) -> FastAPI:
    """Construit l'interface. `settings` sert aux tests."""
    config_path = Path(config_path)
    executeur = Executeur()
    gabarits = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def lire_settings() -> Settings:
        """Relit la configuration à chaque requête : elle change en cours de route."""
        return settings or load_settings(config_path)

    application = FastAPI(title="BankExtract", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------------ page

    @application.get("/", response_class=HTMLResponse)
    def accueil(request: Request) -> HTMLResponse:
        return gabarits.TemplateResponse(
            request=request,
            name="app.html",
            context={
                "sources_otp": _sources_otp_ordonnees(),
                "config_path": str(local_overlay_path(config_path)),
            },
        )

    # ------------------------------------------------------------------ état

    @application.get("/api/etat")
    def etat() -> dict:
        courant = lire_settings()
        base = Database(courant.paths.database_url)
        operation = executeur.operation_visible()

        return {
            "banques": [_decrire_banque(nom, cfg, courant) for nom, cfg in courant.banks.items()],
            "operation": operation.en_dict() if operation else None,
            "occupe": executeur.occupe,
            "totaux": _totaux_lisibles(base),
            "dossiers": {
                "releves": str(courant.paths.downloads_dir.resolve()),
                "exports": str(courant.paths.exports_dir.resolve()),
            },
        }

    # ------------------------------------------------------------------ banques

    @application.post("/api/banques")
    def enregistrer_banque(description: DescriptionBanque) -> dict:
        cle = description.cle()
        surcouche = _lire_surcouche(config_path)
        banques = surcouche.setdefault("banks", {})
        existante = banques.get(cle, {})

        banques[cle] = {
            **existante,
            "connector": "scenario",
            "enabled": True,
            "label": description.libelle or description.nom,
            "history_days": description.historique_jours,
            "username_env": f"{cle.upper()}_USERNAME",
            "password_env": f"{cle.upper()}_PASSWORD",
            "otp": {**existante.get("otp", {}), "provider": description.source_otp},
            "options": {
                **existante.get("options", {}),
                "base_url": description.url,
                "scenario_path": f"scenarios/{cle}.json",
                "account_number": description.numero_compte or cle,
                "currency": description.devise,
            },
        }
        _ecrire_surcouche(config_path, surcouche)
        return {"cle": cle, "message": f"Banque « {description.libelle or cle} » enregistrée."}

    @application.delete("/api/banques/{cle}")
    def supprimer_banque(cle: str) -> dict:
        surcouche = _lire_surcouche(config_path)
        if cle not in surcouche.get("banks", {}):
            raise HTTPException(404, f"Banque « {cle} » introuvable dans vos réglages.")
        del surcouche["banks"][cle]
        _ecrire_surcouche(config_path, surcouche)
        return {"message": f"Banque « {cle} » retirée."}

    @application.post("/api/identifiants")
    def deposer_identifiants(identifiants: Identifiants) -> dict:
        courant = lire_settings()
        config = courant.banks.get(identifiants.banque)
        if config is None:
            raise HTTPException(404, f"Banque « {identifiants.banque} » inconnue.")
        try:
            store_credentials(
                identifiants.banque,
                config,
                Credentials(identifiants.utilisateur, identifiants.mot_de_passe),
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"message": "Identifiants enregistrés dans le coffre de Windows."}

    # ------------------------------------------------------------------ opérations

    @application.post("/api/operations/enregistrer/{cle}")
    def demarrer_enregistrement(cle: str) -> dict:
        courant = lire_settings()
        config = _exiger_banque(courant, cle)
        url = str((config.options or {}).get("base_url") or "")
        if not url:
            raise HTTPException(400, "Renseignez d'abord l'adresse du portail de la banque.")

        def travail(operation: Operation) -> str:
            from ..recorder import record_scenario
            from ..recorder.record import annotate_scenario

            operation.lignes.append(f"Ouverture de {url}")
            operation.lignes.append(
                "Connectez-vous, téléchargez un relevé, puis FERMEZ la fenêtre."
            )
            scenario = record_scenario(
                cle, url, courant.browser, label=config.display_name, max_seconds=1800
            )
            if len(scenario.steps) <= 1:
                raise RuntimeError(
                    "Aucune action enregistrée. La fenêtre a-t-elle été fermée trop tôt ?"
                )
            for note in annotate_scenario(scenario):
                operation.lignes.append(f"→ {note}")

            fuites = scenario.contains_secret_values()
            if fuites:
                operation.lignes.append(f"ATTENTION : {'; '.join(fuites)}")

            chemin = Path((config.options or {}).get("scenario_path", f"scenarios/{cle}.json"))
            scenario.save(chemin)
            if not scenario.downloads:
                operation.lignes.append(
                    "Aucun téléchargement détecté : recommencez en cliquant bien sur le relevé."
                )
            return f"Parcours enregistré ({scenario.summary()}) dans {chemin}"

        return _lancer(executeur, f"Enregistrement du parcours — {config.display_name}", travail)

    @application.post("/api/operations/extraire/{cle}")
    def demarrer_extraction(cle: str, jours: int = 90) -> dict:
        courant = lire_settings()
        config = _exiger_banque(courant, cle)

        def travail(operation: Operation) -> str:
            fin = date.today()
            debut = fin - timedelta(days=max(jours, 1))
            bilans = run_banks(courant, [cle], start=debut, end=fin)
            bilan = bilans[0]
            for chemin in bilan.exports:
                operation.lignes.append(f"Fichier produit : {chemin}")
            if not bilan.ok:
                raise RuntimeError(" · ".join(bilan.result.errors))
            return bilan.describe()

        return _lancer(executeur, f"Extraction — {config.display_name}", travail)

    @application.post("/api/operations/essai/{cle}")
    def demarrer_essai(cle: str) -> dict:
        """Rejoue le parcours en montrant le navigateur : le mode de mise au point."""
        courant = lire_settings()
        config = _exiger_banque(courant, cle)
        courant.browser.headless = False

        def travail(operation: Operation) -> str:
            connecteur = build_connector(cle, config, courant)
            fin = date.today()
            resultat = connecteur.run(start=fin - timedelta(days=config.history_days), end=fin)
            if resultat.errors:
                raise RuntimeError(" · ".join(resultat.errors))
            return resultat.summary()

        return _lancer(executeur, f"Essai visible — {config.display_name}", travail)

    @application.get("/api/sante")
    def sante() -> dict:
        return {"statut": "ok", "heure": datetime.now().strftime("%H:%M:%S")}

    # Le tableau de bord de consultation est monté tel quel : les relevés
    # extraits se consultent depuis la même fenêtre.
    from ..dashboard.app import create_app as creer_tableau_de_bord

    application.mount("/releves", creer_tableau_de_bord(lire_settings()))

    return application


# ---------------------------------------------------------------------- utilitaires


def _sources_otp_ordonnees() -> list[str]:
    """« manual » d'abord : il fonctionne sans matériel, les autres en demandent."""
    sources = available_providers()
    return ["manual"] + [source for source in sources if source != "manual"]


def _lancer(executeur: Executeur, nom: str, travail) -> dict:
    try:
        operation = executeur.lancer(nom, travail)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"identifiant": operation.identifiant, "nom": operation.nom}


def _exiger_banque(settings: Settings, cle: str) -> BankConfig:
    config = settings.banks.get(cle)
    if config is None:
        raise HTTPException(404, f"Banque « {cle} » inconnue.")
    return config


def _decrire_banque(nom: str, config: BankConfig, settings: Settings) -> dict:
    """Ce que l'interface affiche pour chaque banque, sans jamais lire de secret."""
    options = config.options or {}
    chemin_parcours = Path(options.get("scenario_path", f"scenarios/{nom}.json"))

    identifiants_prets = True
    try:
        get_credentials(nom, config)
    except Exception:
        identifiants_prets = False

    return {
        "cle": nom,
        "libelle": config.display_name,
        "url": options.get("base_url", ""),
        "numero_compte": options.get("account_number", ""),
        "devise": options.get("currency", "DZD"),
        "source_otp": config.otp.provider,
        "historique_jours": config.history_days,
        "active": config.enabled,
        "pilotee_par_parcours": config.connector == "scenario",
        "parcours_enregistre": chemin_parcours.exists(),
        "identifiants_prets": identifiants_prets,
    }


def _totaux_lisibles(base: Database) -> dict:
    totaux = base.totals()
    derniere = totaux.get("last_run")
    return {
        "comptes": totaux.get("accounts", 0),
        "ecritures": totaux.get("transactions", 0),
        "releves": totaux.get("statements", 0),
        "derniere_extraction": (
            derniere.strftime("%d/%m/%Y à %H:%M") if isinstance(derniere, datetime) else None
        ),
    }


def _lire_surcouche(config_path: Path) -> dict[str, Any]:
    chemin = local_overlay_path(config_path)
    if not chemin.exists():
        return {}
    return yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}


def _ecrire_surcouche(config_path: Path, contenu: dict[str, Any]) -> Path:
    chemin = local_overlay_path(config_path)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    entete = (
        "# Vos réglages, écrits par l'interface de BankExtract.\n"
        "# Ils complètent le modèle documenté (banks.yaml) sans le modifier.\n"
        "# Ce fichier n'est pas publié : il reste sur ce poste.\n\n"
    )
    chemin.write_text(
        entete + yaml.safe_dump(contenu, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return chemin


def port_libre(prefere: int = 8765) -> int:
    """Renvoie le port souhaité s'il est libre, sinon un port choisi par le système."""
    with socket.socket() as sonde:
        try:
            sonde.bind(("127.0.0.1", prefere))
            return prefere
        except OSError:
            pass
    with socket.socket() as sonde:
        sonde.bind(("127.0.0.1", 0))
        return sonde.getsockname()[1]


def erreur_lisible(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=500, content={"detail": str(exc)})
