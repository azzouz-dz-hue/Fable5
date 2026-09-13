"""Analyse des relevés PDF.

Deux stratégies successives : extraction des tableaux quand le PDF en contient
(cas le plus courant), puis lecture ligne à ligne par expression régulière pour
les PDF « texte brut » que produisent certains mainframes bancaires.
Les PDF scannés (images) ne sont pas gérés : ils demandent un OCR.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pdfplumber

from ..models import Transaction
from ..normalize import clean_label, parse_amount, parse_date, signed_amount
from .csv_statement import map_columns

logger = logging.getLogger(__name__)

#: « 04/03/2026  05/03/2026  VIR RECU CLIENT  1 250 000,00  4 235 890,45 »
LINE_PATTERN = re.compile(
    r"^(?P<date>\d{2}[/.-]\d{2}[/.-]\d{2,4})\s+"
    r"(?P<value_date>\d{2}[/.-]\d{2}[/.-]\d{2,4})?\s*"
    r"(?P<label>.+?)\s+"
    r"(?P<amount>[\d  .,]+\d)\s*(?P<sense>[DC])?\s*"
    r"(?P<balance>[\d  .,]+\d)?\s*$"
)


def parse_pdf_statement(
    path: Path,
    account_key: str,
    currency: str = "DZD",
    pages: str | None = None,
    columns: dict[str, int] | None = None,
) -> list[Transaction]:
    """Extrait les écritures d'un relevé PDF."""
    transactions: list[Transaction] = []

    with pdfplumber.open(path) as pdf:
        selected = _select_pages(pdf.pages, pages)
        for page in selected:
            found = _from_tables(page, account_key, currency, columns)
            if not found:
                found = _from_text(page, account_key, currency)
            transactions.extend(found)

    if not transactions:
        logger.warning(
            "%s : aucune écriture extraite — PDF probablement scanné (un OCR est nécessaire).",
            path.name,
        )
    return _deduplicate(transactions)


def _select_pages(pages, spec: str | None):
    """Applique un intervalle « 2-5 » ou une liste « 1,3,7 » (1-indexé)."""
    if not spec:
        return pages
    wanted: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            first, _, last = part.partition("-")
            wanted.update(range(int(first), int(last) + 1))
        elif part:
            wanted.add(int(part))
    return [page for index, page in enumerate(pages, start=1) if index in wanted]


def _from_tables(page, account_key: str, currency: str, columns: dict[str, int] | None):
    """Lit les tableaux détectés par pdfplumber."""
    transactions: list[Transaction] = []
    for table in page.extract_tables() or []:
        if len(table) < 2:
            continue
        header = [clean_label(str(cell or "")) for cell in table[0]]
        mapping = columns or _index_mapping(header)
        if "date" not in mapping:
            continue

        for row in table[1:]:
            cells = [clean_label(str(cell or "")) for cell in row]
            transaction = _row_to_transaction(cells, mapping, account_key, currency)
            if transaction:
                transactions.append(transaction)
    return transactions


def _index_mapping(header: list[str]) -> dict[str, int]:
    """Réutilise les alias de colonnes du lecteur CSV, en index numériques."""
    by_name = map_columns(header)
    return {field: header.index(name) for field, name in by_name.items() if name in header}


def _row_to_transaction(
    cells: list[str], mapping: dict[str, int], account_key: str, currency: str
) -> Transaction | None:
    def value(field: str) -> str:
        index = mapping.get(field)
        return cells[index] if index is not None and index < len(cells) else ""

    operation_date = parse_date(value("date"))
    if operation_date is None:
        return None

    amount = signed_amount(
        debit=value("debit") or None,
        credit=value("credit") or None,
        amount=value("amount") or None,
        sense=value("sense") or None,
    )
    if amount is None:
        return None

    return Transaction(
        account_key=account_key,
        date=operation_date,
        value_date=parse_date(value("value_date")),
        label=clean_label(value("label")) or "(sans libellé)",
        amount=amount,
        currency=currency,
        balance_after=parse_amount(value("balance")),
        reference=clean_label(value("reference")) or None,
    )


def _from_text(page, account_key: str, currency: str) -> list[Transaction]:
    """Repli : relit le texte brut ligne à ligne."""
    text = page.extract_text() or ""
    transactions: list[Transaction] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = LINE_PATTERN.match(line)
        if not match:
            continue

        operation_date = parse_date(match.group("date"))
        amount = parse_amount(match.group("amount"))
        if operation_date is None or amount is None:
            continue

        sense = match.group("sense")
        signed = -abs(amount) if sense == "D" else abs(amount) if sense == "C" else amount

        transactions.append(
            Transaction(
                account_key=account_key,
                date=operation_date,
                value_date=parse_date(match.group("value_date")),
                label=clean_label(match.group("label")) or "(sans libellé)",
                amount=signed,
                currency=currency,
                balance_after=parse_amount(match.group("balance")),
            )
        )
    return transactions


def _deduplicate(transactions: list[Transaction]) -> list[Transaction]:
    """Un relevé répète parfois une ligne à cheval sur deux pages."""
    seen: set[str] = set()
    unique: list[Transaction] = []
    for transaction in transactions:
        if transaction.fingerprint in seen:
            continue
        seen.add(transaction.fingerprint)
        unique.append(transaction)
    return unique
