"""Contrat commun à tous les connecteurs bancaires."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

from ..browser import BrowserSession, browser_session
from ..config import BankConfig, Settings
from ..models import Account, ExtractionResult, StatementFile, Transaction
from ..otp import OtpProvider, build_otp_provider
from ..secrets import Credentials, get_credentials

logger = logging.getLogger(__name__)


class LoginError(RuntimeError):
    """Échec d'authentification (identifiants refusés, compte bloqué, OTP invalide)."""


class ScrapingError(RuntimeError):
    """La page ne correspond pas à ce que le connecteur attend."""


class BankConnector(ABC):
    """Squelette d'un connecteur : l'orchestration est ici, les sélecteurs en aval."""

    name: str = "base"
    display_name: str = "Banque"
    base_url: str = ""

    def __init__(self, config: BankConfig, settings: Settings, otp: OtpProvider | None = None):
        self.config = config
        self.settings = settings
        self.otp = otp or build_otp_provider(config.otp)

    # ------------------------------------------------------------------ à implémenter

    @abstractmethod
    def login(self, session: BrowserSession, credentials: Credentials) -> None:
        """Authentifie la session, OTP compris."""

    @abstractmethod
    def fetch_accounts(self, session: BrowserSession) -> list[Account]:
        """Liste les comptes visibles après connexion."""

    @abstractmethod
    def fetch_transactions(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[Transaction]:
        """Récupère les écritures d'un compte sur la période demandée."""

    # ------------------------------------------------------------------ optionnel

    def download_statements(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[StatementFile]:
        """Télécharge les relevés officiels. Sans effet par défaut."""
        return []

    def logout(self, session: BrowserSession) -> None:
        """Déconnexion propre — évite de laisser une session ouverte côté banque."""
        return

    # ------------------------------------------------------------------ orchestration

    def run(self, start: date | None = None, end: date | None = None) -> ExtractionResult:
        """Déroule l'extraction complète et n'échoue jamais silencieusement."""
        end = end or date.today()
        start = start or end - timedelta(days=self.config.history_days)
        result = ExtractionResult(bank=self.name)

        try:
            credentials = get_credentials(self.name, self.config)
        except Exception as exc:
            result.errors.append(f"Identifiants : {exc}")
            result.finished_at = datetime.now()
            return result

        try:
            with browser_session(self.settings.browser, self.name) as session:
                try:
                    logger.info("[%s] connexion…", self.name)
                    self.login(session, credentials)

                    result.accounts = self.fetch_accounts(session)
                    logger.info("[%s] %d compte(s) trouvé(s)", self.name, len(result.accounts))

                    for account in result.accounts:
                        self._collect_account(session, account, start, end, result)

                    self.logout(session)
                except Exception as exc:
                    logger.exception("[%s] échec de l'extraction", self.name)
                    result.errors.append(f"{type(exc).__name__}: {exc}")
                    if self.settings.browser.screenshot_on_error and (
                        shot := session.screenshot("erreur")
                    ):
                        result.errors.append(f"Capture d'écran : {shot}")
        except Exception as exc:  # le navigateur n'a même pas démarré
            result.errors.append(f"Navigateur : {type(exc).__name__}: {exc}")

        result.finished_at = datetime.now()
        return result

    def _collect_account(
        self,
        session: BrowserSession,
        account: Account,
        start: date,
        end: date,
        result: ExtractionResult,
    ) -> None:
        """Traite un compte ; une erreur isolée ne doit pas perdre les autres comptes."""
        try:
            transactions = self.fetch_transactions(session, account, start, end)
            result.transactions.extend(transactions)
            logger.info("[%s] %s : %d écriture(s)", self.name, account.number, len(transactions))
        except Exception as exc:
            result.errors.append(f"Écritures {account.number}: {type(exc).__name__}: {exc}")

        if not self.config.download_statements:
            return
        try:
            files = self.download_statements(session, account, start, end)
            for statement in files:
                statement.compute_sha256()
            result.files.extend(files)
        except Exception as exc:
            result.errors.append(f"Relevés {account.number}: {type(exc).__name__}: {exc}")
