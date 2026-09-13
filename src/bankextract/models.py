"""Modèles de données normalisés, communs à toutes les banques."""

from __future__ import annotations

import hashlib
from datetime import date as Date
from datetime import datetime as DateTime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountType(str, Enum):
    COURANT = "courant"
    EPARGNE = "epargne"
    DEVISE = "devise"
    CREDIT = "credit"
    INCONNU = "inconnu"


class Account(BaseModel):
    """Un compte bancaire tel qu'exposé par le portail."""

    model_config = ConfigDict(str_strip_whitespace=True)

    bank: str
    number: str = Field(description="Numéro de compte tel qu'affiché par la banque")
    label: str = ""
    iban: str | None = None
    rib: str | None = None
    currency: str = "DZD"
    type: AccountType = AccountType.INCONNU
    balance: Decimal | None = None
    balance_date: Date | None = None

    @property
    def key(self) -> str:
        """Identifiant stable d'un compte, indépendant du libellé."""
        base = (self.iban or self.number).replace(" ", "").upper()
        return f"{self.bank.lower()}:{base}"

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()


class Transaction(BaseModel):
    """Une écriture normalisée, quelle que soit la banque d'origine."""

    model_config = ConfigDict(str_strip_whitespace=True)

    account_key: str
    date: Date = Field(description="Date d'opération")
    value_date: Date | None = Field(default=None, description="Date de valeur")
    label: str
    amount: Decimal = Field(description="Négatif au débit, positif au crédit")
    currency: str = "DZD"
    balance_after: Decimal | None = None
    reference: str | None = None
    category: str | None = None
    raw: dict[str, str] = Field(default_factory=dict, repr=False)

    @property
    def debit(self) -> Decimal:
        return -self.amount if self.amount < 0 else Decimal(0)

    @property
    def credit(self) -> Decimal:
        return self.amount if self.amount > 0 else Decimal(0)

    @property
    def fingerprint(self) -> str:
        """Empreinte déterministe servant à dédoublonner entre deux extractions.

        Volontairement bâtie sur les seuls champs qu'une banque ne réécrit pas :
        un libellé re-formaté ou une catégorie ajoutée ne doit pas créer un doublon.
        """
        parts = [
            self.account_key,
            self.date.isoformat(),
            f"{self.amount:.2f}",
            _squash(self.label),
            self.reference or "",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class StatementFile(BaseModel):
    """Un relevé officiel téléchargé depuis le portail (PDF, CSV, OFX...)."""

    account_key: str
    path: Path
    period_start: Date | None = None
    period_end: Date | None = None
    downloaded_at: DateTime = Field(default_factory=DateTime.now)
    source_url: str | None = None
    sha256: str | None = None

    def compute_sha256(self) -> str:
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.sha256 = digest
        return digest


class ExtractionResult(BaseModel):
    """Résultat d'une exécution pour une banque donnée."""

    bank: str
    started_at: DateTime = Field(default_factory=DateTime.now)
    finished_at: DateTime | None = None
    accounts: list[Account] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    files: list[StatementFile] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{self.bank}: {len(self.accounts)} compte(s), "
            f"{len(self.transactions)} écriture(s), {len(self.files)} fichier(s)"
            + (f", {len(self.errors)} erreur(s)" if self.errors else "")
        )


def _squash(text: str) -> str:
    """Normalise un libellé pour la comparaison : casse, espaces et ponctuation."""
    return " ".join(text.upper().split())
