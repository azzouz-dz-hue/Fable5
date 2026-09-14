"""Rejeu d'un parcours enregistré.

Le connecteur ne connaît rien du portail : il exécute les étapes du scénario,
remplace les jetons par les vraies valeurs au dernier moment, et récupère les
fichiers téléchargés. Ceux-ci sont ensuite analysés pour alimenter la base, ce
qui referme la boucle enregistrement → relevé → écritures.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..browser import BrowserSession, browser_session
from ..connectors.base import BankConnector, ScrapingError
from ..models import Account, ExtractionResult, StatementFile, Transaction
from ..parsers import parse_statement
from ..secrets import Credentials, get_credentials
from .scenario import (
    TOKEN_END,
    TOKEN_OTP,
    TOKEN_PASSWORD,
    TOKEN_START,
    TOKEN_USERNAME,
    ActionType,
    Scenario,
    Step,
)

logger = logging.getLogger(__name__)

#: Un sélecteur candidat est testé brièvement : s'il ne répond pas, on passe au
#: suivant plutôt que d'attendre le délai complet sur chacun.
CANDIDATE_TIMEOUT_MS = 4_000

#: Ouvrir un menu est immédiat : inutile d'attendre longtemps à chaque essai.
REVEAL_TIMEOUT_MS = 1_500

#: Attribut posé le temps d'identifier les parents à déplier, puis retiré.
MARQUEUR_MENU = "data-bankextract-parent"


class StepFailure(ScrapingError):
    """Une étape du scénario n'a pas pu être rejouée."""


class ScenarioConnector(BankConnector):
    """Connecteur qui rejoue un parcours enregistré au lieu de suivre des sélecteurs figés."""

    name = "scenario"
    display_name = "Parcours enregistré"

    def __init__(self, config, settings, otp=None, scenario: Scenario | None = None):
        super().__init__(config=config, settings=settings, otp=otp)
        self._scenario = scenario

    # ------------------------------------------------------------------ scénario

    @property
    def scenario(self) -> Scenario:
        if self._scenario is None:
            self._scenario = Scenario.load(self.scenario_path)
        return self._scenario

    @property
    def scenario_path(self) -> Path:
        configured = (self.config.options or {}).get("scenario_path")
        if configured:
            return Path(configured)
        directory = Path((self.config.options or {}).get("scenarios_dir", "scenarios"))
        return directory / f"{self.name}.json"

    # ------------------------------------------------------------------ contrat de base

    def login(self, session: BrowserSession, credentials: Credentials) -> None:
        """Le scénario porte lui-même la connexion : rien à faire séparément."""
        return None

    def fetch_accounts(self, session: BrowserSession) -> list[Account]:
        """Le compte est décrit dans la configuration : le parcours ne le lit pas."""
        return [self._declared_account()]

    def fetch_transactions(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[Transaction]:
        return []

    def _declared_account(self) -> Account:
        options = self.config.options or {}
        return Account(
            bank=self.name,
            number=str(options.get("account_number") or self.name),
            label=str(options.get("account_label") or self.config.display_name),
            currency=str(options.get("currency", "DZD")),
        )

    # ------------------------------------------------------------------ exécution

    def run(self, start: date | None = None, end: date | None = None) -> ExtractionResult:
        """Rejoue le parcours, récupère les relevés et en extrait les écritures."""
        end = end or date.today()
        start = start or end - timedelta(days=self.config.history_days)
        result = ExtractionResult(bank=self.name)

        try:
            scenario = self.scenario
        except Exception as exc:
            result.errors.append(f"Scénario : {exc}")
            result.finished_at = datetime.now()
            return result

        try:
            credentials = get_credentials(self.name, self.config)
        except Exception as exc:
            result.errors.append(f"Identifiants : {exc}")
            result.finished_at = datetime.now()
            return result

        account = self._declared_account()
        result.accounts = [account]
        destination = self.settings.paths.downloads_dir / self.name

        try:
            with browser_session(self.settings.browser, self.name) as session:
                try:
                    logger.info(
                        "[%s] rejeu de « %s » — %s",
                        self.name,
                        scenario.display_name,
                        scenario.summary(),
                    )
                    files = self._replay(session, scenario, credentials, start, end, destination)
                    result.files = [
                        StatementFile(account_key=account.key, path=path) for path in files
                    ]
                    for statement in result.files:
                        statement.compute_sha256()
                except Exception as exc:
                    logger.exception("[%s] rejeu interrompu", self.name)
                    result.errors.append(f"{type(exc).__name__}: {exc}")
                    if self.settings.browser.screenshot_on_error and (
                        shot := session.screenshot("rejeu")
                    ):
                        result.errors.append(f"Capture d'écran : {shot}")
        except Exception as exc:
            result.errors.append(f"Navigateur : {type(exc).__name__}: {exc}")

        result.transactions = self._parse_files(result, account, start, end)
        result.finished_at = datetime.now()
        return result

    def _parse_files(
        self, result: ExtractionResult, account: Account, start: date, end: date
    ) -> list[Transaction]:
        """Analyse les relevés téléchargés ; un fichier illisible n'arrête pas les autres."""
        if not (self.config.options or {}).get("parse_downloads", True):
            return []

        transactions: list[Transaction] = []
        for statement in result.files:
            try:
                found = parse_statement(statement.path, account.key, currency=account.currency)
            except Exception as exc:
                result.errors.append(f"Analyse {statement.path.name}: {exc}")
                continue
            kept = [t for t in found if start <= t.date <= end]
            logger.info(
                "[%s] %s : %d écriture(s) retenue(s) sur %d",
                self.name,
                statement.path.name,
                len(kept),
                len(found),
            )
            transactions.extend(kept)
        return transactions

    # ------------------------------------------------------------------ moteur de rejeu

    def _replay(
        self,
        session: BrowserSession,
        scenario: Scenario,
        credentials: Credentials,
        start: date,
        end: date,
        destination: Path,
    ) -> list[Path]:
        """Exécute les étapes dans l'ordre et renvoie les fichiers obtenus."""
        downloaded: list[Path] = []
        date_format = str((self.config.options or {}).get("date_format", "%d/%m/%Y"))
        otp_requested_at = datetime.now(timezone.utc)

        for number, step in enumerate(scenario.steps, start=1):
            logger.info("[%s] %2d/%d — %s", self.name, number, len(scenario.steps), step.describe())
            try:
                path = self._run_step(
                    session,
                    step,
                    credentials,
                    start,
                    end,
                    date_format,
                    destination,
                    otp_requested_at,
                )
            except Exception as exc:
                if step.optional:
                    logger.info("     ↳ étape facultative ignorée (%s)", type(exc).__name__)
                    continue
                raise StepFailure(
                    f"étape {number} ({step.describe()}) : {type(exc).__name__}: {exc}"
                ) from exc
            if path is not None:
                downloaded.append(path)
            if step.wait_ms:
                session.page.wait_for_timeout(step.wait_ms)

        if not downloaded:
            logger.warning("[%s] aucun fichier téléchargé par ce parcours", self.name)
        return downloaded

    def _run_step(
        self,
        session: BrowserSession,
        step: Step,
        credentials: Credentials,
        start: date,
        end: date,
        date_format: str,
        destination: Path,
        otp_requested_at: datetime,
    ) -> Path | None:
        """Exécute une étape ; renvoie le chemin du fichier si elle en produit un."""
        if step.action is ActionType.GOTO:
            if not step.url:
                raise StepFailure("navigation sans URL dans le scénario")
            session.goto(step.url)
            return None

        if step.action is ActionType.KEYPAD:
            self._type_on_keypad(session, step, credentials, otp_requested_at)
            session.page.wait_for_load_state("domcontentloaded")
            return None

        target = self._resolve(session, step)
        value = self._resolve_value(step, credentials, start, end, date_format, otp_requested_at)

        if step.action is ActionType.FILL:
            target.fill(value or "")
        elif step.action is ActionType.SELECT:
            target.select_option(value or "")
        elif step.action is ActionType.CHECK:
            target.check() if value != "false" else target.uncheck()
        elif step.action is ActionType.PRESS:
            target.press(value or "Enter")
        elif step.action is ActionType.WAIT:
            session.page.wait_for_timeout(int(value or 1000))
        elif step.action is ActionType.CLICK:
            self._cliquer(target)
        elif step.action is ActionType.DOWNLOAD:
            return session.download_to(lambda: self._cliquer(target), destination)

        session.page.wait_for_load_state("domcontentloaded")
        return None

    def _type_on_keypad(
        self,
        session: BrowserSession,
        step: Step,
        credentials: Credentials,
        otp_requested_at: datetime,
    ) -> None:
        """Clique les touches d'un clavier virtuel, caractère par caractère."""
        if not step.key_template:
            raise StepFailure("clavier virtuel sans gabarit de touche dans le scénario")

        secret = (
            credentials.password
            if step.value == TOKEN_PASSWORD
            else self.otp.wait_for_code(since=otp_requested_at, hint=f"Code {self.display_name}")
            if step.value == TOKEN_OTP
            else step.value or ""
        )

        scope = self._frame(session, step)
        for character in secret:
            selector = step.key_template.replace("{c}", character)
            try:
                touche = scope.wait_for_selector(selector, timeout=CANDIDATE_TIMEOUT_MS)
            except Exception as exc:
                # Le caractère n'est jamais cité dans le message : il ferait fuiter le code.
                raise StepFailure(
                    "touche introuvable sur le clavier virtuel — le gabarit "
                    f"« {step.key_template} » ne correspond plus"
                ) from exc
            touche.click()

    def _cliquer(self, element) -> None:
        """Clique, en insistant si l'élément se dérobe.

        Trois tentatives de plus en plus directes : le clic ordinaire, qui
        respecte la visibilité et les recouvrements ; le clic forcé, qui passe
        outre ; puis le déclenchement depuis la page elle-même, qui aboutit même
        sur un lien resté caché dans un menu que rien n'a su déplier.
        """
        try:
            element.click()
            return
        except Exception as exc:
            logger.info("     ↳ clic ordinaire refusé (%s) — on insiste", type(exc).__name__)

        try:
            element.click(force=True)
            return
        except Exception:
            logger.info("     ↳ clic forcé refusé — déclenchement depuis la page")

        element.evaluate("cible => cible.click()")

    def _resolve(self, session: BrowserSession, step: Step):
        """Essaie les sélecteurs candidats dans l'ordre et renvoie le premier qui répond."""
        scope = self._frame(session, step)
        errors: list[str] = []

        for candidate in step.selectors:
            try:
                element = scope.wait_for_selector(candidate, timeout=CANDIDATE_TIMEOUT_MS)
            except Exception as exc:
                errors.append(f"{candidate} ({type(exc).__name__})")
                continue
            if element is not None:
                return element

        # Deuxième chance : l'élément peut être présent mais caché dans un menu
        # déroulant. L'utilisateur l'avait ouvert d'un survol de souris — un
        # geste que l'enregistreur ne capte pas, puisqu'il n'est pas un clic.
        logger.info("     ↳ élément introuvable en l'état — tentative d'ouverture des menus")
        for candidate in step.selectors:
            element = self._ouvrir_menu_contenant(scope, candidate)
            if element is not None:
                logger.info("     ↳ menu déroulant ouvert pour atteindre l'élément")
                return element

        raise StepFailure(self._expliquer_l_echec(session, scope, step, errors))

    def _expliquer_l_echec(
        self, session: BrowserSession, scope, step: Step, errors: list[str]
    ) -> str:
        """Dit ce qui a réellement été constaté, et non seulement qu'on a échoué.

        La distinction décisive est celle-ci : un élément absent du document
        signale que la page affichée n'est pas celle attendue — une connexion
        qui n'a pas abouti, le plus souvent ; un élément présent mais invisible
        signale un menu que l'on n'a pas su déplier. Les deux demandent des
        corrections opposées, d'où l'intérêt de les nommer.
        """
        constats: list[str] = []
        for candidate in step.selectors:
            constats.append(f"« {candidate} » : {self._etat_de_l_element(scope, candidate)}")

        try:
            page_courante = f"page affichée : {session.page.url}"
        except Exception:
            page_courante = "page affichée : inconnue"

        try:
            titre = session.page.title()
            if titre:
                page_courante += f" — « {titre} »"
        except Exception:
            pass

        return (
            "l'élément attendu est introuvable, menus déroulants compris.\n"
            + "\n".join(f"  {constat}" for constat in constats)
            + f"\n  {page_courante}"
            + (f"\n  délais : {', '.join(errors)}" if errors else "")
        )

    def _etat_de_l_element(self, scope, selecteur: str) -> str:
        """Absent du document, ou présent mais invisible ?"""
        try:
            element = scope.wait_for_selector(selecteur, state="attached", timeout=1_000)
        except Exception:
            return "absent du document — la page affichée n'est sans doute pas la bonne"
        if element is None:
            return "absent du document"
        try:
            if element.is_visible():
                return "présent et visible, mais le clic n'a pas abouti"
        except Exception:
            return "présent, état indéterminable"
        return "présent mais invisible — menu non déplié"

    def _ouvrir_menu_contenant(self, scope, selecteur: str):
        """Déplie le menu qui masque l'élément visé, puis le renvoie.

        Les portails ouvrent leurs menus de trois façons : la pseudo-classe CSS
        `:hover`, qui exige un vrai déplacement de souris ; un script à l'écoute
        des événements de survol ; ou un clic sur l'entrée parente. Les trois
        sont tentées sur chaque ancêtre, du plus extérieur au plus proche.

        Les ancêtres sont marqués depuis la page elle-même plutôt que désignés
        par un axe XPath : la façon dont Playwright enchaîne les sélecteurs
        relatifs varie, et un enchaînement qui échoue en silence donne
        l'illusion qu'aucun parent n'existe.
        """
        try:
            element = scope.wait_for_selector(
                selecteur, state="attached", timeout=REVEAL_TIMEOUT_MS
            )
        except Exception:
            return None
        if element is None:
            return None

        niveaux = self._marquer_les_ancetres(element)
        if not niveaux:
            logger.info("     ↳ aucun parent à déplier")
            return None

        try:
            # Du plus extérieur au plus proche : ouvrir un sous-menu suppose
            # d'avoir ouvert celui qui le contient.
            for niveau in range(niveaux - 1, -1, -1):
                ancetre = scope.locator(f'[{MARQUEUR_MENU}="{niveau}"]').first
                for geste in ("survol", "clic"):
                    if self._tenter(ancetre, geste, niveau) and self._est_visible(element):
                        logger.info("     ↳ menu ouvert par %s du parent %d", geste, niveau)
                        return element

            self._emettre_survols(element)
            if self._est_visible(element):
                logger.info("     ↳ menu ouvert par les événements de survol")
                return element
        finally:
            self._effacer_les_marques(scope)

        logger.info("     ↳ aucun geste n'a déplié le menu (%d parent(s) essayé(s))", niveaux)
        return None

    def _marquer_les_ancetres(self, element) -> int:
        """Marque les parents de l'élément et renvoie leur nombre."""
        try:
            return int(
                element.evaluate(
                    """(cible, marqueur) => {
                        let noeud = cible.parentElement;
                        let niveau = 0;
                        while (noeud && noeud !== document.body && niveau < 6) {
                            noeud.setAttribute(marqueur, String(niveau));
                            noeud = noeud.parentElement;
                            niveau += 1;
                        }
                        return niveau;
                    }""",
                    MARQUEUR_MENU,
                )
            )
        except Exception:
            return 0

    def _effacer_les_marques(self, scope) -> None:
        try:
            scope.evaluate(
                """marqueur => {
                    for (const noeud of document.querySelectorAll('[' + marqueur + ']')) {
                        noeud.removeAttribute(marqueur);
                    }
                }""",
                MARQUEUR_MENU,
            )
        except Exception:
            logger.debug("Marques de menu non effacées")

    def _tenter(self, ancetre, geste: str, niveau: int) -> bool:
        """Survole ou clique un parent ; False si le geste est impossible."""
        try:
            if not ancetre.is_visible():
                return False
            if geste == "survol":
                ancetre.hover(timeout=REVEAL_TIMEOUT_MS)
            else:
                ancetre.click(timeout=REVEAL_TIMEOUT_MS, no_wait_after=True)
            return True
        except Exception:
            logger.debug("Parent %d : %s impossible", niveau, geste)
            return False

    def _est_visible(self, element) -> bool:
        try:
            return bool(element.is_visible())
        except Exception:
            return False

    def _emettre_survols(self, element) -> None:
        """Dernier recours : les menus pilotés par script écoutent ces événements."""
        try:
            element.evaluate(
                """cible => {
                    const types = ['pointerover', 'mouseover', 'mouseenter'];
                    let noeud = cible;
                    let remontees = 0;
                    while (noeud && noeud !== document.body && remontees < 6) {
                        for (const type of types) {
                            noeud.dispatchEvent(new MouseEvent(type, {bubbles: true}));
                        }
                        noeud = noeud.parentElement;
                        remontees += 1;
                    }
                }"""
            )
        except Exception:
            logger.debug("Événements de survol non émis")

    def _frame(self, session: BrowserSession, step: Step):
        """Retrouve l'iframe dans laquelle l'action avait été enregistrée."""
        if not step.frame_url:
            return session.page
        for frame in session.page.frames:
            if frame.url == step.frame_url:
                return frame
        # L'URL d'une iframe porte souvent un jeton de session : on retombe sur le chemin.
        path = step.frame_url.split("?")[0]
        for frame in session.page.frames:
            if frame.url.startswith(path):
                return frame
        logger.warning("Iframe %s introuvable — tentative sur la page principale", step.frame_url)
        return session.page

    def _resolve_value(
        self,
        step: Step,
        credentials: Credentials,
        start: date,
        end: date,
        date_format: str,
        otp_requested_at: datetime,
    ) -> str | None:
        """Remplace un jeton par sa valeur réelle, au tout dernier moment."""
        value = step.value
        if value == TOKEN_USERNAME:
            return credentials.username
        if value == TOKEN_PASSWORD:
            return credentials.password
        if value == TOKEN_OTP:
            logger.info("     ↳ code d'authentification demandé (source : %s)", self.otp.name)
            return self.otp.wait_for_code(since=otp_requested_at, hint=f"Code {self.display_name}")
        if value == TOKEN_START:
            return start.strftime(date_format)
        if value == TOKEN_END:
            return end.strftime(date_format)
        return value
