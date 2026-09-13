"""Orchestration : extraction → base de données → exports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .connectors import build_connector
from .export import export_csv, export_excel
from .models import ExtractionResult
from .storage import Database
from .storage.db import SaveReport

logger = logging.getLogger(__name__)


@dataclass
class BankOutcome:
    """Bilan d'une banque au terme d'une exécution."""

    bank: str
    result: ExtractionResult
    saved: SaveReport | None = None
    exports: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.ok

    def describe(self) -> str:
        parts = [self.result.summary()]
        if self.saved:
            parts.append(str(self.saved))
        return " — ".join(parts)


def run_banks(
    settings: Settings,
    banks: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    database: Database | None = None,
    export: bool = True,
) -> list[BankOutcome]:
    """Extrait les banques demandées et renvoie un bilan par banque.

    L'échec d'une banque n'interrompt pas les suivantes : un OTP expiré chez
    l'une ne doit pas priver l'utilisateur des données des autres.
    """
    selected = settings.enabled_banks()
    if banks:
        missing = [name for name in banks if name not in settings.banks]
        if missing:
            known = ", ".join(sorted(settings.banks)) or "(aucune)"
            raise KeyError(f"Banque(s) inconnue(s) : {', '.join(missing)}. Configurées : {known}")
        selected = {name: settings.banks[name] for name in banks}

    if not selected:
        logger.warning("Aucune banque activée dans la configuration.")
        return []

    db = database or Database(settings.paths.database_url)
    outcomes: list[BankOutcome] = []

    for name, config in selected.items():
        logger.info("=== %s ===", config.display_name)
        connector = build_connector(name, config, settings)

        window_start = start or (end or date.today()) - timedelta(days=config.history_days)
        result = connector.run(start=window_start, end=end or date.today())

        outcome = BankOutcome(bank=name, result=result)
        outcome.saved = db.save_result(result)

        if export and result.transactions:
            outcome.exports = export_result(result, settings)

        for error in result.errors:
            logger.error("[%s] %s", name, error)
        logger.info("[%s] %s", name, outcome.describe())
        outcomes.append(outcome)

    return outcomes


def export_result(result: ExtractionResult, settings: Settings) -> list[Path]:
    """Écrit le CSV et le classeur Excel d'une extraction."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    labels = {account.key: account.number for account in result.accounts}
    base = settings.paths.exports_dir / result.bank
    return [
        export_csv(result.transactions, base / f"{result.bank}_{stamp}.csv", labels),
        export_excel(
            result.transactions, base / f"{result.bank}_{stamp}.xlsx", labels, result.accounts
        ),
    ]


def export_database(
    settings: Settings,
    database: Database | None = None,
    start: date | None = None,
    end: date | None = None,
    account_key: str | None = None,
) -> list[Path]:
    """Exporte l'historique complet de la base, toutes banques confondues."""
    db = database or Database(settings.paths.database_url)
    transactions = db.transactions(account_key=account_key, start=start, end=end)
    if not transactions:
        return []

    labels = {account.key: account.number for account in db.accounts()}
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = settings.paths.exports_dir
    return [
        export_csv(transactions, base / f"historique_{stamp}.csv", labels),
        export_excel(transactions, base / f"historique_{stamp}.xlsx", labels),
    ]
