"""Analyse des relevés CSV/Excel exportés par les portails."""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from ..models import Transaction
from ..normalize import clean_label, parse_date, signed_amount, strip_accents

#: Intitulés de colonnes rencontrés dans les exports francophones.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date operation", "date d'operation", "date", "date compta", "date comptable"),
    "value_date": ("date valeur", "date de valeur", "valeur"),
    "label": (
        "libelle", "libelle operation", "intitule", "description", "operation", "motif",
    ),
    "debit": ("debit", "retrait", "montant debit", "sortie"),
    "credit": ("credit", "versement", "montant credit", "entree"),
    "amount": ("montant", "amount", "mouvement"),
    "sense": ("sens", "type operation", "d/c"),
    "balance": ("solde", "solde apres", "nouveau solde"),
    "reference": ("reference", "ref", "num operation", "piece", "numero"),
}


def _normalize_header(text: str) -> str:
    """Ramène un intitulé de colonne à une forme comparable : sans accent ni ponctuation."""
    stripped = strip_accents(str(text).lower())
    return " ".join(stripped.replace("_", " ").replace(".", " ").split())


def map_columns(headers: list[str]) -> dict[str, str]:
    """Associe les colonnes du fichier aux champs normalisés.

    La correspondance exacte prime sur la correspondance partielle, sinon
    « date » capturerait « date valeur ».
    """
    normalized = {header: _normalize_header(header) for header in headers}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for field, aliases in COLUMN_ALIASES.items():
        for exact in (True, False):
            for header, norm in normalized.items():
                if header in used:
                    continue
                hit = norm in aliases if exact else any(a in norm for a in aliases)
                if hit:
                    mapping[field] = header
                    used.add(header)
                    break
            if field in mapping:
                break
    return mapping


def _read_table(path: Path, encoding: str | None, delimiter: str | None) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, dtype=str).fillna("")

    raw = path.read_bytes()
    for candidate in ([encoding] if encoding else ["utf-8-sig", "utf-8", "cp1252", "latin-1"]):
        try:
            text = raw.decode(candidate)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:  # pragma: no cover - latin-1 accepte tout octet
        text = raw.decode("latin-1", errors="replace")

    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=";,\t|").delimiter
        except csv.Error:
            delimiter = ";"

    from io import StringIO

    return pd.read_csv(StringIO(text), sep=delimiter, dtype=str).fillna("")


def parse_csv_statement(
    path: Path,
    account_key: str,
    currency: str = "DZD",
    encoding: str | None = None,
    delimiter: str | None = None,
    columns: dict[str, str] | None = None,
) -> list[Transaction]:
    """Extrait les écritures d'un relevé CSV ou Excel.

    `columns` force la correspondance quand les intitulés sont exotiques.
    """
    frame = _read_table(path, encoding, delimiter)
    if frame.empty:
        return []

    mapping = columns or map_columns([str(c) for c in frame.columns])
    if "date" not in mapping:
        raise ValueError(
            f"{path.name} : aucune colonne de date reconnue parmi {list(frame.columns)}"
        )

    transactions: list[Transaction] = []
    for _, row in frame.iterrows():
        transaction = _row_to_transaction(row, mapping, account_key, currency)
        if transaction is not None:
            transactions.append(transaction)
    return transactions


def _cell(row, mapping: dict[str, str], field: str) -> str:
    """Lit une colonne de la ligne ; chaîne vide si elle est absente du fichier."""
    column = mapping.get(field)
    return str(row[column]).strip() if column and column in row else ""


def _row_to_transaction(
    row, mapping: dict[str, str], account_key: str, currency: str
) -> Transaction | None:
    """Construit une écriture ; None pour les en-têtes répétés, totaux et séparateurs."""
    operation_date = parse_date(_cell(row, mapping, "date"))
    if operation_date is None:
        return None

    amount = signed_amount(
        debit=_cell(row, mapping, "debit") or None,
        credit=_cell(row, mapping, "credit") or None,
        amount=_cell(row, mapping, "amount") or None,
        sense=_cell(row, mapping, "sense") or None,
    )
    if amount is None:
        return None

    return Transaction(
        account_key=account_key,
        date=operation_date,
        value_date=parse_date(_cell(row, mapping, "value_date")),
        label=clean_label(_cell(row, mapping, "label")) or "(sans libellé)",
        amount=amount,
        currency=currency,
        balance_after=signed_amount(amount=_cell(row, mapping, "balance") or None),
        reference=clean_label(_cell(row, mapping, "reference")) or None,
    )
