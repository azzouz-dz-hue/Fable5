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
            target.click()
        elif step.action is ActionType.DOWNLOAD:
            return session.download_to(lambda: target.click(), destination)

        session.page.wait_for_load_state("domcontentloaded")
        return None

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

        raise StepFailure(
            "aucun sélecteur ne correspond — le portail a peut-être changé. "
            f"Candidats essayés : {', '.join(errors) or 'aucun'}"
        )

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
