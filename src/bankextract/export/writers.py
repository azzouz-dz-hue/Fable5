"""Génération des fichiers exploitables en comptabilité.

Colonnes volontairement proches d'un grand livre : date, libellé, débit, crédit,
solde — un comptable retrouve ses repères sans retraitement.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ..models import Transaction

COLUMNS = [
    "Banque",
    "Compte",
    "Date opération",
    "Date valeur",
    "Libellé",
    "Débit",
    "Crédit",
    "Devise",
    "Solde",
    "Référence",
    "Catégorie",
]


def _row(transaction, account_label: str = "") -> dict:
    bank, _, number = str(transaction.account_key).partition(":")
    amount = Decimal(str(transaction.amount))
    return {
        "Banque": bank.upper(),
        "Compte": account_label or number,
        "Date opération": transaction.date,
        "Date valeur": getattr(transaction, "value_date", None),
        "Libellé": transaction.label,
        "Débit": -amount if amount < 0 else None,
        "Crédit": amount if amount > 0 else None,
        "Devise": transaction.currency,
        "Solde": getattr(transaction, "balance_after", None),
        "Référence": getattr(transaction, "reference", None),
        "Catégorie": getattr(transaction, "category", None),
    }


def transactions_dataframe(
    transactions: Iterable[Transaction], account_labels: dict[str, str] | None = None
) -> pd.DataFrame:
    """Construit le tableau normalisé, trié du plus ancien au plus récent."""
    labels = account_labels or {}
    rows = [_row(t, labels.get(str(t.account_key), "")) for t in transactions]
    frame = pd.DataFrame(rows, columns=COLUMNS)
    if frame.empty:
        return frame
    return frame.sort_values(["Compte", "Date opération"]).reset_index(drop=True)


def export_csv(
    transactions: Sequence[Transaction],
    destination: Path,
    account_labels: dict[str, str] | None = None,
) -> Path:
    """Écrit un CSV en UTF-8 BOM, point-virgule : Excel francophone l'ouvre tel quel."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame = transactions_dataframe(transactions, account_labels)
    for column in ("Date opération", "Date valeur"):
        if column in frame:
            frame[column] = frame[column].map(
                lambda value: value.strftime("%d/%m/%Y") if isinstance(value, date) else ""
            )
    frame.to_csv(destination, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    return destination


def export_excel(
    transactions: Sequence[Transaction],
    destination: Path,
    account_labels: dict[str, str] | None = None,
    accounts: Sequence | None = None,
) -> Path:
    """Écrit un classeur : une feuille de synthèse, puis une feuille par compte."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame = transactions_dataframe(transactions, account_labels)

    with pd.ExcelWriter(destination, engine="openpyxl", datetime_format="dd/mm/yyyy") as writer:
        summary = _summary_frame(frame, accounts)
        summary.to_excel(writer, sheet_name="Synthèse", index=False)
        _autosize(writer.sheets["Synthèse"], summary)

        if frame.empty:
            frame.to_excel(writer, sheet_name="Écritures", index=False)
        else:
            for account, group in frame.groupby("Compte", sort=True):
                sheet = _sheet_name(str(account))
                data = group.drop(columns=["Compte"]).reset_index(drop=True)
                data.to_excel(writer, sheet_name=sheet, index=False)
                _autosize(writer.sheets[sheet], data)
    return destination


def _summary_frame(frame: pd.DataFrame, accounts: Sequence | None) -> pd.DataFrame:
    """Totaux par compte : débits, crédits, variation nette et période couverte."""
    if frame.empty:
        return pd.DataFrame(
            [{"Compte": "—", "Écritures": 0, "Total débit": 0, "Total crédit": 0}]
        )

    grouped = frame.groupby("Compte")
    summary = pd.DataFrame(
        {
            "Compte": grouped.size().index,
            "Écritures": grouped.size().to_numpy(),
            "Total débit": grouped["Débit"].sum().to_numpy(),
            "Total crédit": grouped["Crédit"].sum().to_numpy(),
            "Première opération": grouped["Date opération"].min().to_numpy(),
            "Dernière opération": grouped["Date opération"].max().to_numpy(),
        }
    )
    summary["Variation nette"] = summary["Total crédit"] - summary["Total débit"]

    if accounts:
        balances = {
            str(getattr(a, "number", "")): getattr(a, "balance", None) for a in accounts
        }
        summary["Solde banque"] = summary["Compte"].map(balances)
    return summary


def _sheet_name(raw: str) -> str:
    """Excel : 31 caractères maximum et pas de : \\ / ? * [ ]"""
    cleaned = "".join("-" if c in ':\\/?*[]' else c for c in raw)
    return cleaned[:31] or "Compte"


def _autosize(worksheet, frame: pd.DataFrame) -> None:
    """Ajuste la largeur des colonnes sur le contenu le plus long.

    `astype(str)` laisse les valeurs manquantes en `nan` flottant : on mesure
    donc via `str()` explicite plutôt que sur la série convertie.
    """
    for index, column in enumerate(frame.columns, start=1):
        lengths = frame[column].map(lambda value: len(str(value))).tolist()
        longest = max([len(str(column)), *lengths])
        worksheet.column_dimensions[_letter(index)].width = min(max(longest + 2, 10), 48)


def _letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


def export_ofx(
    transactions: Sequence[Transaction],
    destination: Path,
    account_number: str = "",
    currency: str = "DZD",
) -> Path:
    """Écrit un OFX 1.0.3 (SGML), le format d'import le plus largement accepté."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    ordered = sorted(transactions, key=lambda t: t.date)
    start = ordered[0].date if ordered else date.today()
    end = ordered[-1].date if ordered else date.today()

    body = "\n".join(_ofx_transaction(t) for t in ordered)
    content = f"""OFXHEADER:100
DATA:OFXSGML
VERSION:103
SECURITY:NONE
ENCODING:UTF-8
CHARSET:NONE
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>{now}<LANGUAGE>FRA</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>{currency}
<BANKACCTFROM><BANKID>000<ACCTID>{account_number or "COMPTE"}<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>{start:%Y%m%d}<DTEND>{end:%Y%m%d}
{body}
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""
    destination.write_text(content, encoding="utf-8")
    return destination


def _ofx_transaction(transaction: Transaction) -> str:
    kind = "CREDIT" if transaction.amount > 0 else "DEBIT"
    label = transaction.label.replace("<", " ").replace("&", "et")[:96]
    return (
        f"<STMTTRN><TRNTYPE>{kind}<DTPOSTED>{transaction.date:%Y%m%d}"
        f"<TRNAMT>{transaction.amount}<FITID>{transaction.fingerprint}"
        f"<NAME>{label}</STMTTRN>"
    )
