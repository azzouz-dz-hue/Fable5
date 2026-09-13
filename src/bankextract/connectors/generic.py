"""Connecteur générique piloté par une description YAML du portail.

Les portails e-banking algériens n'exposent aucune API : tout passe par du HTML.
Plutôt qu'un connecteur Python par banque, ce moteur lit une *carte de sélecteurs*
(`config/banks.yaml`). Quand la banque refond son site, seul le YAML change.

Les connecteurs qui demandent une logique particulière (iframes, canvas, clavier
virtuel) héritent de cette classe et redéfinissent la méthode concernée.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..browser import BrowserSession
from ..models import Account, AccountType, StatementFile, Transaction
from ..normalize import clean_label, detect_currency, parse_amount, parse_date, signed_amount
from ..secrets import Credentials
from .base import BankConnector, LoginError, ScrapingError

logger = logging.getLogger(__name__)


class GenericPortalConnector(BankConnector):
    """Connecteur HTML déclaratif."""

    name = "generic"
    display_name = "Portail générique"

    # ------------------------------------------------------------------ accès config

    @property
    def spec(self) -> dict[str, Any]:
        return self.config.options or {}

    def _section(self, key: str) -> dict[str, Any]:
        section = self.spec.get(key)
        if not isinstance(section, dict):
            raise ScrapingError(
                f"[{self.name}] section « {key} » absente de la configuration du connecteur."
            )
        return section

    def _url(self, path: str | None) -> str:
        base = str(self.spec.get("base_url", self.base_url)).rstrip("/")
        if not path:
            return base
        if path.startswith("http"):
            return path
        return f"{base}/{path.lstrip('/')}"

    # ------------------------------------------------------------------ connexion

    def login(self, session: BrowserSession, credentials: Credentials) -> None:
        spec = self._section("login")
        session.goto(self._url(spec.get("url")))

        if consent := spec.get("cookie_accept_selector"):
            _click_if_present(session, consent)

        session.page.fill(_require(spec, "username_selector", self.name), credentials.username)
        session.page.fill(_require(spec, "password_selector", self.name), credentials.password)

        requested_at = datetime.now(timezone.utc)
        session.page.click(_require(spec, "submit_selector", self.name))
        session.page.wait_for_load_state("domcontentloaded")

        self._raise_on_error_banner(session, spec)
        self._handle_otp(session, spec.get("otp") or {}, requested_at)

        if success := spec.get("success_selector"):
            try:
                session.page.wait_for_selector(success, timeout=self.settings.browser.timeout_ms)
            except Exception as exc:
                self._raise_on_error_banner(session, spec)
                raise LoginError(
                    f"[{self.name}] connexion non confirmée : « {success} » introuvable."
                ) from exc
        logger.info("[%s] connecté", self.name)

    def _raise_on_error_banner(self, session: BrowserSession, spec: dict[str, Any]) -> None:
        """Remonte le message d'erreur de la banque plutôt qu'un timeout opaque."""
        selector = spec.get("error_selector")
        if not selector:
            return
        element = session.page.query_selector(selector)
        if element and (message := clean_label(element.inner_text())):
            raise LoginError(f"[{self.name}] la banque a répondu : « {message} »")

    def _handle_otp(
        self, session: BrowserSession, otp_spec: dict[str, Any], requested_at: datetime
    ) -> None:
        """Saisit le code d'authentification forte s'il est demandé."""
        input_selector = otp_spec.get("input_selector")
        if not input_selector:
            return

        # Certaines banques n'envoient le SMS qu'après un clic explicite.
        if trigger := otp_spec.get("trigger_selector"):
            _click_if_present(session, trigger)

        # L'OTP peut être conditionnel : appareil déjà reconnu, pas de champ affiché.
        wait_ms = int(otp_spec.get("wait_ms", 8000))
        try:
            session.page.wait_for_selector(input_selector, timeout=wait_ms)
        except Exception:
            logger.info("[%s] pas d'OTP demandé (appareil reconnu)", self.name)
            return

        prompt = otp_spec.get("prompt") or f"Code {self.display_name}"
        logger.info("[%s] OTP demandé — source : %s", self.name, self.otp.name)
        code = self.otp.wait_for_code(since=requested_at, hint=prompt)

        session.page.fill(input_selector, code)
        if submit := otp_spec.get("submit_selector"):
            session.page.click(submit)
        else:
            session.page.press(input_selector, "Enter")
        session.page.wait_for_load_state("domcontentloaded")

    # ------------------------------------------------------------------ comptes

    def fetch_accounts(self, session: BrowserSession) -> list[Account]:
        spec = self._section("accounts")
        if url := spec.get("url"):
            session.goto(self._url(url))

        row_selector = _require(spec, "row_selector", self.name)
        session.page.wait_for_selector(row_selector, timeout=self.settings.browser.timeout_ms)
        columns = spec.get("columns") or {}

        accounts: list[Account] = []
        for row in session.page.query_selector_all(row_selector):
            number = clean_label(_cell(row, columns.get("number")))
            if not number:
                continue
            currency_text = _cell(row, columns.get("currency"))
            balance_text = _cell(row, columns.get("balance"))
            accounts.append(
                Account(
                    bank=self.name,
                    number=number,
                    label=clean_label(_cell(row, columns.get("label"))),
                    iban=clean_label(_cell(row, columns.get("iban"))) or None,
                    rib=clean_label(_cell(row, columns.get("rib"))) or None,
                    currency=detect_currency(
                        currency_text or balance_text, str(spec.get("default_currency", "DZD"))
                    ),
                    type=_account_type(_cell(row, columns.get("type")) or number),
                    balance=parse_amount(balance_text),
                    balance_date=date.today() if balance_text else None,
                )
            )
        if not accounts:
            raise ScrapingError(f"[{self.name}] aucun compte lu via « {row_selector} ».")
        return accounts

    # ------------------------------------------------------------------ écritures

    def fetch_transactions(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[Transaction]:
        spec = self._section("transactions")
        date_format = spec.get("date_format", "%d/%m/%Y")

        if template := spec.get("url_template"):
            session.goto(
                self._url(
                    template.format(
                        number=account.number.replace(" ", ""),
                        raw_number=account.number,
                        iban=account.iban or "",
                        start=start.strftime(date_format),
                        end=end.strftime(date_format),
                        start_iso=start.isoformat(),
                        end_iso=end.isoformat(),
                    )
                )
            )
        self._apply_date_filter(session, spec, start, end, date_format)

        row_selector = _require(spec, "row_selector", self.name)
        columns = spec.get("columns") or {}
        max_pages = int(spec.get("max_pages", 25))

        transactions: list[Transaction] = []
        seen: set[str] = set()

        for page_index in range(max_pages):
            try:
                session.page.wait_for_selector(
                    row_selector, timeout=self.settings.browser.timeout_ms
                )
            except Exception:
                if page_index == 0:
                    logger.warning("[%s] aucune écriture pour %s", self.name, account.number)
                break

            for row in session.page.query_selector_all(row_selector):
                transaction = self._build_transaction(row, account, columns, start, end)
                if transaction and transaction.fingerprint not in seen:
                    seen.add(transaction.fingerprint)
                    transactions.append(transaction)

            if not self._go_to_next_page(session, spec):
                break

        return transactions

    def _build_transaction(
        self,
        row,
        account: Account,
        columns: dict[str, Any],
        start: date,
        end: date,
    ) -> Transaction | None:
        """Construit une écriture ; renvoie None pour les lignes parasites (totaux…)."""
        operation_date = parse_date(_cell(row, columns.get("date")))
        if operation_date is None:
            return None
        if not (start <= operation_date <= end):
            return None  # la banque déborde souvent du filtre demandé

        amount = signed_amount(
            debit=_cell(row, columns.get("debit")) or None,
            credit=_cell(row, columns.get("credit")) or None,
            amount=_cell(row, columns.get("amount")) or None,
            sense=_cell(row, columns.get("sense")) or None,
        )
        if amount is None:
            return None

        label = clean_label(_cell(row, columns.get("label")))
        return Transaction(
            account_key=account.key,
            date=operation_date,
            value_date=parse_date(_cell(row, columns.get("value_date"))),
            label=label or "(sans libellé)",
            amount=amount,
            currency=account.currency,
            balance_after=parse_amount(_cell(row, columns.get("balance"))),
            reference=clean_label(_cell(row, columns.get("reference"))) or None,
        )

    def _apply_date_filter(
        self,
        session: BrowserSession,
        spec: dict[str, Any],
        start: date,
        end: date,
        date_format: str,
    ) -> None:
        """Renseigne le formulaire de période quand le portail en propose un."""
        filter_spec = spec.get("date_filter") or {}
        if not filter_spec:
            return
        if selector := filter_spec.get("start_selector"):
            session.page.fill(selector, start.strftime(filter_spec.get("format", date_format)))
        if selector := filter_spec.get("end_selector"):
            session.page.fill(selector, end.strftime(filter_spec.get("format", date_format)))
        if selector := filter_spec.get("submit_selector"):
            session.page.click(selector)
            session.page.wait_for_load_state("domcontentloaded")

    def _go_to_next_page(self, session: BrowserSession, spec: dict[str, Any]) -> bool:
        """Passe à la page suivante ; False quand la pagination est épuisée."""
        selector = spec.get("next_page_selector")
        if not selector:
            return False
        element = session.page.query_selector(selector)
        if not element or not element.is_enabled():
            return False
        if _is_disabled(element):
            return False
        element.click()
        session.page.wait_for_load_state("domcontentloaded")
        return True

    # ------------------------------------------------------------------ relevés PDF

    def download_statements(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[StatementFile]:
        spec = self.spec.get("statements")
        if not isinstance(spec, dict):
            return []

        date_format = spec.get("date_format", "%d/%m/%Y")
        if template := spec.get("url_template"):
            session.goto(
                self._url(
                    template.format(
                        number=account.number.replace(" ", ""),
                        raw_number=account.number,
                        start=start.strftime(date_format),
                        end=end.strftime(date_format),
                        start_iso=start.isoformat(),
                        end_iso=end.isoformat(),
                    )
                )
            )

        row_selector = spec.get("row_selector")
        if not row_selector:
            return []
        try:
            session.page.wait_for_selector(row_selector, timeout=int(spec.get("wait_ms", 10_000)))
        except Exception:
            logger.info("[%s] aucun relevé listé pour %s", self.name, account.number)
            return []

        destination = self.settings.paths.downloads_dir / self.name / _safe(account.number)
        link_selector = spec.get("link_selector")
        columns = spec.get("columns") or {}
        limit = int(spec.get("max_files", 24))

        files: list[StatementFile] = []
        rows = session.page.query_selector_all(row_selector)[:limit]
        for index, row in enumerate(rows):
            link = row.query_selector(link_selector) if link_selector else row
            if link is None:
                continue
            period = parse_date(_cell(row, columns.get("period")))
            filename = _statement_name(self.name, account, period, index)
            try:
                path = session.download_to(lambda el=link: el.click(), destination, filename)
            except Exception as exc:
                logger.warning("[%s] téléchargement échoué (%s) : %s", self.name, filename, exc)
                continue
            files.append(
                StatementFile(
                    account_key=account.key,
                    path=path,
                    period_start=period,
                    source_url=session.page.url,
                )
            )
        return files

    # ------------------------------------------------------------------ déconnexion

    def logout(self, session: BrowserSession) -> None:
        if selector := (self.spec.get("logout") or {}).get("selector"):
            _click_if_present(session, selector)


# ---------------------------------------------------------------------- utilitaires


def _require(spec: dict[str, Any], key: str, connector: str) -> str:
    value = spec.get(key)
    if not value:
        raise ScrapingError(f"[{connector}] sélecteur « {key} » manquant dans la configuration.")
    return str(value)


def _cell(row, selector: str | None) -> str:
    """Lit une cellule de la ligne ; chaîne vide si le sélecteur est absent ou vide."""
    if not selector:
        return ""
    try:
        element = row.query_selector(selector)
    except Exception:
        return ""
    if element is None:
        return ""
    text = element.inner_text() or element.get_attribute("value") or ""
    return text.strip()


def _click_if_present(session: BrowserSession, selector: str) -> bool:
    element = session.page.query_selector(selector)
    if element is None:
        return False
    try:
        element.click()
        return True
    except Exception:
        return False


def _is_disabled(element) -> bool:
    if element.get_attribute("disabled") is not None:
        return True
    classes = (element.get_attribute("class") or "").lower()
    return "disabled" in classes


def _account_type(text: str) -> AccountType:
    lowered = text.lower()
    if any(word in lowered for word in ("epargne", "épargne", "livret")):
        return AccountType.EPARGNE
    if any(word in lowered for word in ("devise", "eur", "usd", "cheque devise")):
        return AccountType.DEVISE
    if any(word in lowered for word in ("credit", "crédit", "pret", "prêt")):
        return AccountType.CREDIT
    if any(word in lowered for word in ("courant", "cheque", "chèque", "ccp")):
        return AccountType.COURANT
    return AccountType.INCONNU


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text).strip("_")


def _statement_name(bank: str, account: Account, period: date | None, index: int) -> str:
    stamp = period.strftime("%Y-%m") if period else f"{date.today():%Y-%m}-{index:02d}"
    return f"{bank}_{_safe(account.number)}_{stamp}.pdf"
