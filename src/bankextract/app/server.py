"""Interface graphique locale : le logiciel utilisable sans ligne de commande.

Lancée d'un double-clic, elle ouvre une page dans le navigateur. Tout reste sur
le poste : le serveur n'écoute que sur 127.0.0.1 et n'est joignable de nulle
part ailleurs.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..browser import browser_installed
from ..config import DEFAULT_CONFIG_PATH, BankConfig, Settings, load_settings, local_overlay_path
from ..connectors import build_connector
from ..install import installer_navigateur
from ..mailer import MailConfig
from ..otp import available_providers
from ..pipeline import run_banks
from ..scheduler import (
    DEFAULT_SCHEDULES_PATH,
    DEFAULT_STATE_PATH,
    Frequency,
    Period,
    Recurrence,
    Schedule,
    SchedulerState,
    is_due,
    load_scheduler_config,
    next_run,
)
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


class Programmation(BaseModel):
    """Une extraction récurrente, telle que décrite dans le formulaire."""

    nom: str = Field(default="", max_length=60)
    banque: str = Field(default="", description="Vide = toutes les banques actives.")
    frequence: str = Field(default="quotidien")
    heure: str = Field(default="20:00")
    jour_semaine: int = Field(default=0, ge=0, le=6)
    jour_mois: int = Field(default=1, ge=1, le=31)
    periode: str = Field(default="depuis_derniere_execution")
    jours: int = Field(default=30, ge=1, le=3650)
    destinataire: str = Field(default="", description="Adresse d'envoi ; vide = pas d'envoi.")

    def vers_schedule(self) -> Schedule:
        base = self.nom.strip() or f"{self.banque or 'toutes'}-{self.frequence}"
        return Schedule(
            name=base,
            banks=[self.banque] if self.banque else [],
            period=Period(self.periode),
            days=self.jours,
            recurrence=Recurrence(
                frequency=Frequency(self.frequence),
                at=self.heure,
                day_of_week=self.jour_semaine,
                day_of_month=self.jour_mois,
            ),
            mail=MailConfig(
                enabled=bool(self.destinataire),
                to=[self.destinataire] if self.destinataire else [],
            ),
            enabled=True,
        )


class Reglages(BaseModel):
    """Choix du navigateur, côté pilotage comme côté affichage."""

    navigateur: str = Field(
        default="", description="« chrome », « msedge », ou vide pour celui fourni."
    )
    navigateur_interface: str = Field(
        default="", description="Navigateur où ouvrir l'interface ; vide = celui du système."
    )


class Identifiants(BaseModel):
    banque: str
    utilisateur: str = Field(min_length=1)
    mot_de_passe: str = Field(min_length=1)


def creer_application(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    settings: Settings | None = None,
    schedules_path: Path | str = DEFAULT_SCHEDULES_PATH,
    state_path: Path | str = DEFAULT_STATE_PATH,
    programmateur: bool = True,
) -> FastAPI:
    """Construit l'interface. `settings` sert aux tests.

    `programmateur` lance le fil qui déclenche les extractions programmées ;
    les tests le laissent au repos.
    """
    config_path = Path(config_path)
    schedules_path = Path(schedules_path)
    etat_programmation = SchedulerState(Path(state_path))
    executeur = Executeur()
    # Permet de clore l'enregistrement depuis l'interface.
    arret_enregistrement = threading.Event()
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
            # Une seule règle : on montre ce qui est actif. Les banques d'exemple
            # du modèle sont livrées inactives, et masquer revient à désactiver.
            "banques": [
                _decrire_banque(nom, cfg, courant)
                for nom, cfg in courant.banks.items()
                if cfg.enabled
            ],
            "operation": operation.en_dict() if operation else None,
            "occupe": executeur.occupe,
            "totaux": _totaux_lisibles(base),
            "navigateur_pret": browser_installed(courant.browser),
            "version": _version_lisible(),
            "capture": _derniere_capture(courant) is not None,
            "programmations": _decrire_programmations(schedules_path, etat_programmation),
            "reglages": {
                "navigateur": courant.browser.channel or "",
                "navigateur_interface": courant.browser.interface_browser or "",
            },
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
        banques = surcouche.setdefault("banks", {})

        if cle in banques:
            del banques[cle]
            _ecrire_surcouche(config_path, surcouche)
            return {"message": f"Banque « {cle} » retirée."}

        # Banque venant du modèle livré : on ne peut pas l'effacer d'un fichier
        # qui ne nous appartient pas, on la masque dans nos propres réglages.
        if cle in lire_settings().banks:
            banques[cle] = {**banques.get(cle, {}), "enabled": False}
            _ecrire_surcouche(config_path, surcouche)
            return {"message": f"Banque « {cle} » masquée."}

        raise HTTPException(404, f"Banque « {cle} » introuvable.")

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

    @application.post("/api/operations/preparer")
    def demarrer_preparation() -> dict:
        """Télécharge le navigateur. Indispensable au premier lancement."""
        courant = lire_settings()

        def travail(operation: Operation) -> str:
            courant.paths.ensure()
            if browser_installed(courant.browser):
                return "Le navigateur est déjà installé."

            operation.lignes.append("Téléchargement du navigateur (environ 150 Mo)…")
            operation.lignes.append("Cela peut prendre plusieurs minutes.")
            return installer_navigateur(journal=operation.lignes.append)

        return _lancer(executeur, "Préparation du poste", travail)

    @application.post("/api/operations/diagnostic")
    def demarrer_diagnostic() -> dict:
        """Ouvre réellement un navigateur : le seul moyen de savoir qu'il marche."""
        courant = lire_settings()

        def travail(operation: Operation) -> str:
            from ..browser import browsers_root, ephemeral_browser

            operation.lignes.append(f"Navigateurs rangés dans {browsers_root()}")
            if not browser_installed(courant.browser):
                raise RuntimeError(
                    "Navigateur absent. Cliquez sur « Préparer le poste »."
                )
            operation.lignes.append("Navigateur présent — essai d'ouverture…")

            courant.browser.headless = True
            with ephemeral_browser(courant.browser, "diagnostic") as session:
                session.page.set_content("<h1>diagnostic</h1>")
                lu = session.page.inner_text("h1")
            if lu != "diagnostic":
                raise RuntimeError("le navigateur s'ouvre mais n'affiche pas la page")
            return "Le navigateur démarre correctement. Tout est en place."

        return _lancer(executeur, "Vérification de l'installation", travail)

    @application.post("/api/operations/enregistrer/{cle}")
    def demarrer_enregistrement(cle: str) -> dict:
        courant = lire_settings()
        config = _exiger_banque(courant, cle)
        url = str((config.options or {}).get("base_url") or "")
        if not url:
            raise HTTPException(400, "Renseignez d'abord l'adresse du portail de la banque.")

        arret_enregistrement.clear()

        def travail(operation: Operation) -> str:
            from ..recorder import record_scenario
            from ..recorder.record import annotate_scenario

            operation.lignes.append(f"Une fenêtre de navigateur s'ouvre sur {url}")
            operation.lignes.append(
                "Faites vos actions DANS CETTE FENÊTRE : connectez-vous, "
                "puis téléchargez un relevé."
            )
            operation.lignes.append(
                "Quand c'est fait, cliquez sur « J'ai terminé » ci-dessus "
                "(ou fermez la fenêtre)."
            )
            scenario = record_scenario(
                cle,
                url,
                courant.browser,
                label=config.display_name,
                max_seconds=1800,
                arret=arret_enregistrement,
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

        return _lancer(
            executeur,
            f"Enregistrement du parcours — {config.display_name}",
            travail,
            arretable=True,
        )

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

    @application.post("/api/operations/terminer")
    def terminer_enregistrement() -> dict:
        """Clôt l'enregistrement en cours, sans attendre la fermeture de la fenêtre."""
        operation = executeur.operation_visible()
        if operation is None or not operation.arretable:
            raise HTTPException(409, "Aucun enregistrement en cours.")
        arret_enregistrement.set()
        operation.lignes.append("Arrêt demandé — enregistrement du parcours…")
        return {"message": "Enregistrement en cours de clôture."}

    @application.post("/api/programmations")
    def enregistrer_programmation(programmation: Programmation) -> dict:
        try:
            schedule = programmation.vers_schedule()
        except Exception as exc:
            raise HTTPException(400, f"Programmation invalide : {exc}") from exc

        surcouche = _lire_surcouche(schedules_path)
        existantes = [
            entree
            for entree in (surcouche.get("schedules") or [])
            if entree.get("name") != schedule.name
        ]
        existantes.append(schedule.model_dump(mode="json"))
        surcouche["schedules"] = existantes
        _ecrire_surcouche(schedules_path, surcouche, _ENTETE_PROGRAMMATIONS)
        return {
            "nom": schedule.name,
            "message": f"« {schedule.name} » programmée {schedule.recurrence.describe()}.",
        }

    @application.delete("/api/programmations/{nom}")
    def supprimer_programmation(nom: str) -> dict:
        surcouche = _lire_surcouche(schedules_path)
        restantes = [
            entree for entree in (surcouche.get("schedules") or []) if entree.get("name") != nom
        ]
        if len(restantes) == len(surcouche.get("schedules") or []):
            raise HTTPException(404, f"Programmation « {nom} » introuvable.")
        surcouche["schedules"] = restantes
        _ecrire_surcouche(schedules_path, surcouche, _ENTETE_PROGRAMMATIONS)
        return {"message": f"Programmation « {nom} » supprimée."}

    @application.post("/api/reglages")
    def enregistrer_reglages(reglages: Reglages) -> dict:
        surcouche = _lire_surcouche(config_path)
        navigateur = surcouche.setdefault("browser", {})
        navigateur["channel"] = reglages.navigateur or None
        navigateur["interface_browser"] = reglages.navigateur_interface or None
        _ecrire_surcouche(config_path, surcouche)
        return {"message": "Réglages enregistrés."}

    @application.get("/api/capture")
    def derniere_capture():
        """Sert la dernière capture d'écran d'erreur, pour l'afficher sur place."""
        chemin = _derniere_capture(lire_settings())
        if chemin is None:
            raise HTTPException(404, "Aucune capture disponible.")
        return FileResponse(chemin, media_type="image/png")

    @application.get("/api/sante")
    def sante() -> dict:
        return {"statut": "ok", "heure": datetime.now().strftime("%H:%M:%S")}

    def _declencher_les_echeances() -> None:
        """Lance les extractions dont l'heure est venue.

        Tourne tant que l'interface est ouverte. Une extraction qui échoue ou
        une programmation illisible ne doit pas arrêter la surveillance.
        """
        while True:
            time.sleep(_PAS_PROGRAMMATION_S)
            try:
                config = load_scheduler_config(schedules_path)
            except Exception:
                logger.exception("Programmations illisibles")
                continue

            maintenant = datetime.now()
            for schedule in config.enabled_schedules():
                derniere = etat_programmation.last_run(schedule.name)
                if not is_due(schedule, derniere, maintenant) or executeur.occupe:
                    continue
                _lancer_programmation(schedule, derniere, maintenant, config)

    def _lancer_programmation(schedule, derniere, maintenant, config) -> None:
        debut, fin = schedule.window(derniere, maintenant)

        def travail(operation: Operation) -> str:
            from ..mailer import Mailer

            bilans = run_banks(lire_settings(), schedule.banks or None, start=debut, end=fin)
            expediteur = Mailer(config.smtp)
            resume = []
            for bilan in bilans:
                resume.append(bilan.describe())
                rapport = expediteur.send_result(bilan.result, schedule.mail, bilan.exports)
                if schedule.mail.enabled:
                    operation.lignes.append(f"Courriel : {rapport}")
            return " · ".join(resume) or "Rien à extraire."

        # Marquée exécutée avant même de commencer : sinon un échec la
        # relancerait à chaque tour de boucle.
        etat_programmation.mark_run(schedule.name, maintenant)
        try:
            executeur.lancer(f"Programmation — {schedule.name}", travail)
        except RuntimeError:
            logger.info("Programmation « %s » reportée : une opération tourne", schedule.name)

    if programmateur:
        threading.Thread(
            target=_declencher_les_echeances, daemon=True, name="programmation"
        ).start()

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


def _lancer(executeur: Executeur, nom: str, travail, arretable: bool = False) -> dict:
    try:
        operation = executeur.lancer(nom, travail, arretable=arretable)
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


#: Un tour par minute suffit : les programmations sont à l'heure près.
_PAS_PROGRAMMATION_S = 60

_ENTETE_BANQUES = (
    "# Vos réglages, écrits par l'interface de BankExtract.\n"
    "# Ils complètent le modèle documenté (banks.yaml) sans le modifier.\n"
    "# Ce fichier n'est pas publié : il reste sur ce poste.\n\n"
)

_ENTETE_PROGRAMMATIONS = (
    "# Vos extractions programmées, écrites par l'interface de BankExtract.\n"
    "# Elles remplacent les exemples du modèle (schedules.yaml).\n"
    "# Ce fichier n'est pas publié : il reste sur ce poste.\n\n"
)


def _version_lisible() -> str:
    """Version du logiciel, et date de l'exécutable quand il en est un.

    Sans cela, impossible de savoir si un correctif est bien en place sur le
    poste : deux versions peuvent produire le même message d'erreur.
    """
    from .. import __version__

    if not getattr(sys, "frozen", False):
        return f"{__version__} (sources)"
    try:
        compile_le = datetime.fromtimestamp(Path(sys.executable).stat().st_mtime)
        return f"{__version__} du {compile_le:%d/%m/%Y à %H:%M}"
    except OSError:
        return __version__


def _derniere_capture(settings: Settings) -> Path | None:
    """Capture d'écran la plus récente, déposée lors d'un échec."""
    dossier = settings.paths.logs_dir / "screenshots"
    if not dossier.is_dir():
        return None
    captures = sorted(dossier.glob("*.png"), key=lambda chemin: chemin.stat().st_mtime)
    return captures[-1] if captures else None


def _decrire_programmations(schedules_path: Path, etat: SchedulerState) -> list[dict]:
    """Ce que l'interface affiche pour chaque extraction programmée."""
    try:
        config = load_scheduler_config(schedules_path)
    except Exception:
        logger.exception("Programmations illisibles")
        return []

    maintenant = datetime.now()
    lignes = []
    for schedule in config.schedules:
        derniere = etat.last_run(schedule.name)
        lignes.append(
            {
                "nom": schedule.name,
                "banques": schedule.banks or ["toutes"],
                "recurrence": schedule.recurrence.describe(),
                "periode": schedule.period.value,
                "destinataires": schedule.mail.to if schedule.mail.enabled else [],
                "active": schedule.enabled,
                "derniere": derniere.strftime("%d/%m/%Y à %H:%M") if derniere else None,
                "prochaine": next_run(schedule.recurrence, derniere or maintenant).strftime(
                    "%d/%m/%Y à %H:%M"
                ),
            }
        )
    return lignes


def _lire_surcouche(config_path: Path) -> dict[str, Any]:
    chemin = local_overlay_path(config_path)
    if not chemin.exists():
        return {}
    return yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}


def _ecrire_surcouche(
    config_path: Path, contenu: dict[str, Any], entete: str = _ENTETE_BANQUES
) -> Path:
    chemin = local_overlay_path(config_path)
    chemin.parent.mkdir(parents=True, exist_ok=True)
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
