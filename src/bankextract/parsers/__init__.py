"""Lecture des relevés déjà téléchargés (PDF, CSV, Excel).

Les portails limitent souvent l'historique consultable à quelques mois alors que
les relevés PDF remontent bien plus loin : ces analyseurs récupèrent ce que le
scraping ne voit pas.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Transaction
from .csv_statement import parse_csv_statement
from .pdf_statement import parse_pdf_statement

__all__ = ["parse_csv_statement", "parse_pdf_statement", "parse_statement"]


def parse_statement(path: Path, account_key: str, **kwargs) -> list[Transaction]:
    """Analyse un relevé en choisissant l'analyseur d'après l'extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf_statement(path, account_key, **kwargs)
    if suffix in (".csv", ".txt", ".tsv", ".xlsx", ".xls"):
        return parse_csv_statement(path, account_key, **kwargs)
    raise ValueError(f"Format de relevé non pris en charge : « {suffix} »")
