"""Base de données locale (SQLite par défaut) et déduplication des écritures.

Une extraction recouvre toujours la précédente : la même écriture est vue
plusieurs fois. L'empreinte `Transaction.fingerprint` sert de clé unique, si
bien qu'on peut relancer l'extraction autant de fois qu'on veut sans doublon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from ..models import Account, ExtractionResult, StatementFile, Transaction


class Base(DeclarativeBase):
    pass


class DbAccount(Base):
    __tablename__ = "accounts"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    bank: Mapped[str] = mapped_column(String(64), index=True)
    number: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255), default="")
    iban: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="DZD")
    type: Mapped[str] = mapped_column(String(32), default="inconnu")
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    balance_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    transactions: Mapped[list[DbTransaction]] = relationship(back_populates="account")


class DbTransaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_transaction_fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    account_key: Mapped[str] = mapped_column(ForeignKey("accounts.key"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    label: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="DZD")
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    account: Mapped[DbAccount] = relationship(back_populates="transactions")


class DbStatement(Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("sha256", name="uq_statement_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_key: Mapped[str] = mapped_column(String(128), index=True)
    path: Mapped[str] = mapped_column(Text)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DbRun(Base):
    """Journal des exécutions — permet de savoir ce qui a tourné et ce qui a échoué."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bank: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accounts_count: Mapped[int] = mapped_column(default=0)
    new_transactions: Mapped[int] = mapped_column(default=0)
    files_count: Mapped[int] = mapped_column(default=0)
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)


@dataclass
class SaveReport:
    """Ce qu'une sauvegarde a réellement changé."""

    accounts: int = 0
    new_transactions: int = 0
    duplicate_transactions: int = 0
    statements: int = 0

    def __str__(self) -> str:
        return (
            f"{self.accounts} compte(s), {self.new_transactions} nouvelle(s) écriture(s), "
            f"{self.duplicate_transactions} doublon(s) ignoré(s), {self.statements} relevé(s)"
        )


class Database:
    """Point d'entrée unique vers la base."""

    def __init__(self, url: str = "sqlite:///data/bankextract.db", echo: bool = False):
        self.engine = create_engine(url, echo=echo, future=True)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, future=True)

    # ------------------------------------------------------------------ écriture

    def save_result(self, result: ExtractionResult) -> SaveReport:
        """Enregistre comptes, écritures et relevés d'une exécution."""
        report = SaveReport()
        with self.session() as session:
            for account in result.accounts:
                self._upsert_account(session, account)
                report.accounts += 1

            known = self._known_fingerprints(session, {t.account_key for t in result.transactions})
            for transaction in result.transactions:
                if transaction.fingerprint in known:
                    report.duplicate_transactions += 1
                    continue
                known.add(transaction.fingerprint)
                session.add(_to_db_transaction(transaction))
                report.new_transactions += 1

            for statement in result.files:
                if self._add_statement(session, statement):
                    report.statements += 1

            session.add(
                DbRun(
                    bank=result.bank,
                    started_at=result.started_at,
                    finished_at=result.finished_at or datetime.now(),
                    accounts_count=len(result.accounts),
                    new_transactions=report.new_transactions,
                    files_count=len(result.files),
                    errors="\n".join(result.errors) or None,
                )
            )
            session.commit()
        return report

    def _upsert_account(self, session: Session, account: Account) -> None:
        existing = session.get(DbAccount, account.key)
        if existing is None:
            session.add(
                DbAccount(
                    key=account.key,
                    bank=account.bank,
                    number=account.number,
                    label=account.label,
                    iban=account.iban,
                    currency=account.currency,
                    type=account.type.value,
                    balance=account.balance,
                    balance_date=account.balance_date,
                )
            )
            return
        # Le solde et le libellé évoluent ; le numéro et la devise ne bougent pas.
        existing.label = account.label or existing.label
        existing.balance = account.balance if account.balance is not None else existing.balance
        existing.balance_date = account.balance_date or existing.balance_date
        existing.type = account.type.value
        existing.updated_at = datetime.now()

    def _known_fingerprints(self, session: Session, account_keys: set[str]) -> set[str]:
        if not account_keys:
            return set()
        rows = session.execute(
            select(DbTransaction.fingerprint).where(DbTransaction.account_key.in_(account_keys))
        ).scalars()
        return set(rows)

    def _add_statement(self, session: Session, statement: StatementFile) -> bool:
        """N'enregistre le relevé que si son contenu est nouveau (hash)."""
        digest = statement.sha256 or (
            statement.compute_sha256() if statement.path.exists() else None
        )
        if digest:
            already = session.execute(
                select(DbStatement.id).where(DbStatement.sha256 == digest)
            ).first()
            if already:
                return False
        session.add(
            DbStatement(
                account_key=statement.account_key,
                path=str(statement.path),
                period_start=statement.period_start,
                period_end=statement.period_end,
                sha256=digest,
                downloaded_at=statement.downloaded_at,
            )
        )
        return True

    # ------------------------------------------------------------------ lecture

    def accounts(self) -> list[DbAccount]:
        with self.session() as session:
            return list(session.execute(select(DbAccount).order_by(DbAccount.bank)).scalars())

    def transactions(
        self,
        account_key: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[DbTransaction]:
        query = select(DbTransaction).order_by(DbTransaction.date.desc(), DbTransaction.id.desc())
        if account_key:
            query = query.where(DbTransaction.account_key == account_key)
        if start:
            query = query.where(DbTransaction.date >= start)
        if end:
            query = query.where(DbTransaction.date <= end)
        if limit:
            query = query.limit(limit)
        with self.session() as session:
            return list(session.execute(query).scalars())

    def runs(self, limit: int = 20) -> list[DbRun]:
        with self.session() as session:
            return list(
                session.execute(
                    select(DbRun).order_by(DbRun.started_at.desc()).limit(limit)
                ).scalars()
            )

    def totals(self) -> dict[str, object]:
        """Chiffres d'en-tête du tableau de bord."""
        with self.session() as session:
            return {
                "accounts": session.execute(select(func.count(DbAccount.key))).scalar_one(),
                "transactions": session.execute(select(func.count(DbTransaction.id))).scalar_one(),
                "statements": session.execute(select(func.count(DbStatement.id))).scalar_one(),
                "last_run": session.execute(select(func.max(DbRun.started_at))).scalar(),
            }


def _to_db_transaction(transaction: Transaction) -> DbTransaction:
    return DbTransaction(
        fingerprint=transaction.fingerprint,
        account_key=transaction.account_key,
        date=transaction.date,
        value_date=transaction.value_date,
        label=transaction.label,
        amount=transaction.amount,
        currency=transaction.currency,
        balance_after=transaction.balance_after,
        reference=transaction.reference,
        category=transaction.category,
    )
