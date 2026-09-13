"""Modèles normalisés et persistance."""

from datetime import date
from decimal import Decimal

from bankextract.models import Account, AccountType, ExtractionResult, Transaction
from bankextract.storage import Database


def _account(**kwargs) -> Account:
    defaults = {"bank": "bna", "number": "00100 987654321 09", "label": "Compte courant"}
    return Account(**{**defaults, **kwargs})


def _transaction(account: Account, **kwargs) -> Transaction:
    defaults = {
        "account_key": account.key,
        "date": date(2026, 3, 4),
        "label": "VIR RECU CLIENT",
        "amount": Decimal("1250000.00"),
    }
    return Transaction(**{**defaults, **kwargs})


def test_cle_compte_ignore_espaces_et_casse():
    assert _account().key == "bna:0010098765432109"
    assert _account(iban="dz 12 3456").key == "bna:DZ123456"


def test_devise_normalisee_en_majuscules():
    assert _account(currency="dzd").currency == "DZD"


def test_debit_credit_derives_du_signe():
    account = _account()
    debit = _transaction(account, amount=Decimal("-1200.50"))
    assert debit.debit == Decimal("1200.50") and debit.credit == 0
    credit = _transaction(account, amount=Decimal("1200.50"))
    assert credit.credit == Decimal("1200.50") and credit.debit == 0


def test_empreinte_stable_et_insensible_au_formatage_du_libelle():
    """Un libellé re-formaté par la banque ne doit pas créer un faux doublon."""
    account = _account()
    first = _transaction(account, label="VIR  RECU   CLIENT")
    second = _transaction(account, label="vir recu client")
    assert first.fingerprint == second.fingerprint


def test_empreinte_distingue_montant_et_date():
    account = _account()
    base = _transaction(account)
    assert base.fingerprint != _transaction(account, amount=Decimal(99)).fingerprint
    assert base.fingerprint != _transaction(account, date=date(2026, 3, 5)).fingerprint


def test_sauvegarde_puis_relecture(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    account = _account(balance=Decimal("4235890.45"), type=AccountType.COURANT)
    result = ExtractionResult(
        bank="bna", accounts=[account], transactions=[_transaction(account)]
    )

    report = db.save_result(result)
    assert (report.accounts, report.new_transactions, report.duplicate_transactions) == (1, 1, 0)

    stored = db.accounts()
    assert len(stored) == 1 and stored[0].balance == Decimal("4235890.45")
    assert db.transactions()[0].label == "VIR RECU CLIENT"


def test_relance_ne_cree_pas_de_doublon(tmp_path):
    """Deux extractions qui se recouvrent : la seconde n'ajoute rien."""
    db = Database(f"sqlite:///{tmp_path}/t.db")
    account = _account()
    result = ExtractionResult(bank="bna", accounts=[account], transactions=[_transaction(account)])

    db.save_result(result)
    second = db.save_result(result)

    assert second.new_transactions == 0
    assert second.duplicate_transactions == 1
    assert db.totals()["transactions"] == 1


def test_solde_mis_a_jour_sans_dupliquer_le_compte(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.save_result(ExtractionResult(bank="bna", accounts=[_account(balance=Decimal(100))]))
    db.save_result(ExtractionResult(bank="bna", accounts=[_account(balance=Decimal(250))]))

    accounts = db.accounts()
    assert len(accounts) == 1 and accounts[0].balance == Decimal(250)


def test_journal_des_executions_conserve_les_erreurs(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    db.save_result(ExtractionResult(bank="bna", errors=["LoginError: mot de passe refusé"]))

    runs = db.runs()
    assert len(runs) == 1 and "mot de passe refusé" in runs[0].errors


def test_filtrage_par_periode(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    account = _account()
    transactions = [
        _transaction(account, date=date(2026, 1, 10), amount=Decimal(10)),
        _transaction(account, date=date(2026, 3, 10), amount=Decimal(20)),
    ]
    db.save_result(ExtractionResult(bank="bna", accounts=[account], transactions=transactions))

    filtered = db.transactions(start=date(2026, 2, 1), end=date(2026, 4, 1))
    assert len(filtered) == 1 and filtered[0].amount == Decimal(20)
