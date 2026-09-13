"""Envoi des relevés par courriel, contre un serveur SMTP local."""

import email
import socket
from datetime import date, datetime
from decimal import Decimal
from email.header import decode_header, make_header

import pytest
from aiosmtpd.controller import Controller

from bankextract.mailer import MailConfig, Mailer, SmtpConfig
from bankextract.models import Account, ExtractionResult, StatementFile, Transaction


def _decode(raw: bytes) -> tuple[str, str]:
    """Renvoie (sujet, corps texte) d'un message brut, en-têtes MIME décodés.

    On repart des octets : analyser la chaîne déjà décodée abîmerait l'UTF-8
    d'un corps en « Content-Transfer-Encoding: 8bit ».
    """
    message = email.message_from_bytes(raw)
    sujet = str(make_header(decode_header(message.get("Subject", ""))))
    corps = ""
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            corps = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
            break
    return sujet, corps


class _Sink:
    """Boîte de réception en mémoire."""

    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - imposé par aiosmtpd
        self.messages.append(
            {
                "from": envelope.mail_from,
                "to": list(envelope.rcpt_tos),
                "raw": envelope.content,  # octets bruts : le décodage revient à _decode
            }
        )
        return "250 Message accepté"


def _free_port() -> int:
    """aiosmtpd 1.4 n'accepte pas le port 0 : on en réserve un nous-mêmes."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def serveur_smtp():
    sink = _Sink()
    controller = Controller(sink, hostname="127.0.0.1", port=_free_port())
    controller.start()
    try:
        yield sink, SmtpConfig(
            host=controller.hostname,
            port=controller.port,
            use_tls=False,
            use_ssl=False,
            sender="robot@medicomedline.com",
            sender_name="BankExtract",
        )
    finally:
        controller.stop()


@pytest.fixture
def extraction(tmp_path):
    compte = Account(
        bank="bna",
        number="00100 987654321 09",
        label="Compte courant SARL",
        balance=Decimal("4235890.45"),
    )
    releve = tmp_path / "releve-mars.csv"
    releve.write_text("Date;Libellé;Débit\n04/03/2026;VIR;1200,00\n", encoding="utf-8")

    return ExtractionResult(
        bank="bna",
        accounts=[compte],
        transactions=[
            Transaction(
                account_key=compte.key,
                date=date(2026, 3, 4),
                label="VIR RECU CLIENT",
                amount=Decimal("1250000.00"),
            ),
            Transaction(
                account_key=compte.key,
                date=date(2026, 3, 3),
                label="FRAIS",
                amount=Decimal("-1200.50"),
            ),
        ],
        files=[StatementFile(account_key=compte.key, path=releve)],
    )


def test_envoi_avec_pieces_jointes(serveur_smtp, extraction, tmp_path):
    sink, smtp = serveur_smtp
    export = tmp_path / "export.csv"
    export.write_text("a;b\n1;2\n", encoding="utf-8")

    rapport = Mailer(smtp).send_result(
        extraction, MailConfig(enabled=True, to=["compta@medicomedline.com"]), [export]
    )

    assert rapport.sent, rapport.error
    assert sink.messages[0]["to"] == ["compta@medicomedline.com"]
    assert set(rapport.attachments) == {"releve-mars.csv", "export.csv"}


def test_corps_resume_l_extraction(serveur_smtp, extraction):
    sink, smtp = serveur_smtp

    Mailer(smtp).send_result(extraction, MailConfig(enabled=True, to=["compta@x.dz"]))

    _, corps = _decode(sink.messages[0]["raw"])
    assert "00100 987654321 09" in corps
    assert "2 écriture(s)" in corps
    assert "Total crédits : 1\u00a0250\u00a0000,00" in corps


def test_copie_carbone_recue(serveur_smtp, extraction):
    sink, smtp = serveur_smtp

    Mailer(smtp).send_result(
        extraction,
        MailConfig(enabled=True, to=["compta@x.dz"], cc=["direction@x.dz"]),
    )

    assert sink.messages[0]["to"] == ["compta@x.dz", "direction@x.dz"]


def test_sujet_personnalisable(serveur_smtp, extraction):
    sink, smtp = serveur_smtp

    Mailer(smtp).send_result(
        extraction,
        MailConfig(enabled=True, to=["x@y.dz"], subject="Relevé {banque} du {date}"),
    )

    sujet, _ = _decode(sink.messages[0]["raw"])
    assert sujet.startswith("Relevé BNA du ")


def test_pieces_jointes_desactivees(serveur_smtp, extraction):
    _, smtp = serveur_smtp

    rapport = Mailer(smtp).send_result(
        extraction,
        MailConfig(enabled=True, to=["x@y.dz"], attach_statements=False, attach_exports=False),
    )

    assert rapport.attachments == []


def test_limite_de_taille_respectee(serveur_smtp, extraction, tmp_path):
    """Un relevé trop lourd est écarté plutôt que de faire rejeter le message."""
    sink, smtp = serveur_smtp
    smtp.max_attachment_mb = 0  # rien ne passe

    rapport = Mailer(smtp).send_result(extraction, MailConfig(enabled=True, to=["x@y.dz"]))

    assert rapport.sent and rapport.attachments == []
    _, corps = _decode(sink.messages[0]["raw"])
    assert "non incluses" in corps and "releve-mars.csv" in corps


def test_pieces_jointes_partiellement_retenues(serveur_smtp, extraction, tmp_path):
    """Cas réel : un relevé passe, un autre est trop lourd.

    Le message doit partir avec ce qui tient et mentionner le reste — c'est la
    situation où une composition naïve échoue, le message étant déjà multipart.
    """
    sink, smtp = serveur_smtp
    smtp.max_attachment_mb = 1
    gros = tmp_path / "gros-export.xlsx"
    gros.write_bytes(b"x" * 5_000_000)

    rapport = Mailer(smtp).send_result(
        extraction, MailConfig(enabled=True, to=["x@y.dz"]), [gros]
    )

    assert rapport.sent, rapport.error
    assert rapport.attachments == ["releve-mars.csv"]
    _, corps = _decode(sink.messages[0]["raw"])
    assert "gros-export.xlsx" in corps and "non incluses" in corps


def test_selection_des_pieces_jointes_respecte_le_budget(tmp_path):
    petit = tmp_path / "petit.csv"
    petit.write_bytes(b"x" * 1000)
    gros = tmp_path / "gros.csv"
    gros.write_bytes(b"x" * 3_000_000)
    absent = tmp_path / "disparu.csv"

    gardes, ecartes = Mailer(SmtpConfig(max_attachment_mb=1)).select_attachments(
        [petit, gros, absent]
    )

    assert gardes == [petit]
    assert ecartes == ["gros.csv"]


def test_echec_signale_sans_interrompre(extraction):
    """Un serveur injoignable ne doit pas faire échouer l'extraction elle-même."""
    smtp = SmtpConfig(host="127.0.0.1", port=1, sender="x@y.dz", use_tls=False, timeout_seconds=1)

    rapport = Mailer(smtp).send_result(extraction, MailConfig(enabled=True, to=["x@y.dz"]))

    assert not rapport.sent and rapport.error


def test_envoi_desactive():
    rapport = Mailer(SmtpConfig()).send_result(
        ExtractionResult(bank="bna"), MailConfig(enabled=False, to=["x@y.dz"])
    )

    assert not rapport.sent and "désactivé" in rapport.skipped_reason


def test_sans_destinataire():
    rapport = Mailer(SmtpConfig(host="h", sender="s@x.dz")).send_result(
        ExtractionResult(bank="bna"), MailConfig(enabled=True)
    )

    assert "destinataire" in rapport.skipped_reason


def test_smtp_non_configure():
    rapport = Mailer(SmtpConfig()).send_result(
        ExtractionResult(bank="bna"), MailConfig(enabled=True, to=["x@y.dz"])
    )

    assert "SMTP" in rapport.error


def test_echec_d_extraction_notifie(serveur_smtp):
    sink, smtp = serveur_smtp
    echec = ExtractionResult(bank="bna", errors=["LoginError: mot de passe refusé"])
    echec.finished_at = datetime.now()

    rapport = Mailer(smtp).send_result(echec, MailConfig(enabled=True, to=["x@y.dz"]))

    assert rapport.sent
    _, corps = _decode(sink.messages[0]["raw"])
    assert "mot de passe refusé" in corps


def test_echec_non_notifie_si_desactive(serveur_smtp):
    _, smtp = serveur_smtp
    echec = ExtractionResult(bank="bna", errors=["LoginError"])

    rapport = Mailer(smtp).send_result(
        echec, MailConfig(enabled=True, to=["x@y.dz"], send_on_error=False)
    )

    assert not rapport.sent and "erreur" in rapport.skipped_reason
