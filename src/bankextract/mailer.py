"""Envoi des relevés par courriel.

Le message récapitule l'extraction et porte en pièces jointes les relevés
téléchargés et les exports comptables. La configuration SMTP vit dans la
configuration ; le mot de passe, lui, reste dans une variable d'environnement.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from pydantic import BaseModel, Field

from .models import ExtractionResult
from .normalize import format_amount

logger = logging.getLogger(__name__)

#: Au-delà, la plupart des serveurs refusent le message : mieux vaut le dire.
DEFAULT_MAX_ATTACHMENT_MB = 20


class SmtpConfig(BaseModel):
    """Paramètres du serveur d'envoi."""

    host: str = ""
    port: int = 587
    username: str = ""
    password_env: str = Field(
        default="SMTP_PASSWORD", description="Variable d'environnement portant le mot de passe."
    )
    use_tls: bool = Field(default=True, description="STARTTLS sur le port 587.")
    use_ssl: bool = Field(default=False, description="SSL direct sur le port 465.")
    sender: str = ""
    sender_name: str = "BankExtract"
    timeout_seconds: int = 30
    max_attachment_mb: int = DEFAULT_MAX_ATTACHMENT_MB

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def password(self) -> str:
        return os.getenv(self.password_env, "")


class MailConfig(BaseModel):
    """Destinataires et contenu d'un envoi."""

    enabled: bool = False
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = "Relevés bancaires — {banque} — {date}"
    attach_statements: bool = Field(default=True, description="Joindre les relevés téléchargés.")
    attach_exports: bool = Field(default=True, description="Joindre les fichiers CSV et Excel.")
    send_on_error: bool = Field(
        default=True, description="Prévenir aussi quand l'extraction a échoué."
    )


@dataclass
class MailReport:
    """Ce qui a été envoyé, ou pourquoi rien ne l'a été."""

    sent: bool = False
    recipients: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    error: str = ""

    def __str__(self) -> str:
        if self.sent:
            return (
                f"envoyé à {', '.join(self.recipients)} "
                f"({len(self.attachments)} pièce(s) jointe(s))"
            )
        return self.error or self.skipped_reason or "non envoyé"


class Mailer:
    """Expédie les messages ; l'échec d'un envoi n'invalide pas l'extraction."""

    def __init__(self, smtp: SmtpConfig):
        self.smtp = smtp

    def send_result(
        self,
        result: ExtractionResult,
        mail: MailConfig,
        exports: list[Path] | None = None,
    ) -> MailReport:
        """Compose et envoie le compte rendu d'une extraction."""
        report = MailReport()

        if not mail.enabled:
            report.skipped_reason = "envoi désactivé pour cette banque"
            return report
        if not mail.to:
            report.skipped_reason = "aucun destinataire configuré"
            return report
        if not self.smtp.configured:
            report.error = "serveur SMTP non configuré (host et sender obligatoires)"
            return report
        if not result.ok and not mail.send_on_error:
            report.skipped_reason = "extraction en échec et envoi sur erreur désactivé"
            return report

        message = self._compose(result, mail, exports or [])
        report.recipients = list(mail.to) + list(mail.cc)
        report.attachments = [part.get_filename() for part in message.iter_attachments()]

        try:
            self._deliver(message, report.recipients)
        except Exception as exc:
            logger.exception("Envoi du courriel impossible")
            report.error = f"{type(exc).__name__}: {exc}"
            return report

        report.sent = True
        logger.info("[%s] courriel %s", result.bank, report)
        return report

    # ------------------------------------------------------------------ composition

    def _compose(
        self, result: ExtractionResult, mail: MailConfig, exports: list[Path]
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = formataddr((self.smtp.sender_name, self.smtp.sender))
        message["To"] = ", ".join(mail.to)
        if mail.cc:
            message["Cc"] = ", ".join(mail.cc)
        message["Date"] = formatdate(localtime=True)
        message["Subject"] = mail.subject.format(
            banque=result.bank.upper(),
            date=result.started_at.strftime("%d/%m/%Y"),
            statut="échec" if not result.ok else "ok",
        )
        candidates: list[Path] = []
        if mail.attach_statements:
            candidates += [statement.path for statement in result.files]
        if mail.attach_exports:
            candidates += list(exports)

        # Le tri est fait avant la composition : une fois le message devenu
        # multipart, on ne peut plus réécrire son corps.
        kept, skipped = self.select_attachments(candidates)

        body = _body(result)
        if skipped:
            logger.warning(
                "Pièces jointes écartées (limite de %d Mo atteinte) : %s",
                self.smtp.max_attachment_mb,
                ", ".join(skipped),
            )
            body += (
                "\n\nPièces jointes non incluses faute de place : "
                + ", ".join(skipped)
                + "\nElles restent disponibles dans le dossier data/."
            )
        message.set_content(body)

        for path in kept:
            maintype, subtype = _mime_type(path)
            message.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
            )
        return message

    def select_attachments(self, paths: list[Path]) -> tuple[list[Path], list[str]]:
        """Retient les fichiers qui tiennent dans le budget ; renvoie (gardés, écartés).

        Un serveur qui refuse un message trop lourd ferait perdre tout l'envoi :
        mieux vaut livrer le compte rendu et signaler ce qui manque.
        """
        budget = self.smtp.max_attachment_mb * 1024 * 1024
        used = 0
        kept: list[Path] = []
        skipped: list[str] = []

        for path in paths:
            if not path.exists():
                continue
            size = path.stat().st_size
            if used + size > budget:
                skipped.append(path.name)
                continue
            kept.append(path)
            used += size

        return kept, skipped

    # ------------------------------------------------------------------ transport

    def _deliver(self, message: EmailMessage, recipients: list[str]) -> None:
        context = ssl.create_default_context()

        if self.smtp.use_ssl:
            with smtplib.SMTP_SSL(
                self.smtp.host, self.smtp.port, timeout=self.smtp.timeout_seconds, context=context
            ) as server:
                self._authenticate(server)
                server.send_message(message, to_addrs=recipients)
            return

        with smtplib.SMTP(
            self.smtp.host, self.smtp.port, timeout=self.smtp.timeout_seconds
        ) as server:
            server.ehlo()
            if self.smtp.use_tls:
                server.starttls(context=context)
                server.ehlo()
            self._authenticate(server)
            server.send_message(message, to_addrs=recipients)

    def _authenticate(self, server: smtplib.SMTP) -> None:
        password = self.smtp.password()
        if self.smtp.username and password:
            server.login(self.smtp.username, password)
        elif self.smtp.username:
            raise RuntimeError(
                f"Mot de passe SMTP absent : renseignez « {self.smtp.password_env} » dans .env"
            )


def _mime_type(path: Path) -> tuple[str, str]:
    guessed, _ = mimetypes.guess_type(path.name)
    if not guessed:
        return "application", "octet-stream"
    maintype, _, subtype = guessed.partition("/")
    return maintype, subtype or "octet-stream"


def _body(result: ExtractionResult) -> str:
    """Corps du message : ce qui compte doit tenir dans les premières lignes."""
    lignes = [
        f"Extraction {result.bank.upper()} du "
        f"{result.started_at.strftime('%d/%m/%Y à %H:%M')}",
        "",
    ]

    if result.ok:
        lignes.append(
            f"{len(result.accounts)} compte(s), {len(result.transactions)} écriture(s), "
            f"{len(result.files)} relevé(s) téléchargé(s)."
        )
    else:
        lignes.append("⚠ L'extraction a rencontré des erreurs :")
        lignes += [f"  - {erreur}" for erreur in result.errors]
    lignes.append("")

    for account in result.accounts:
        solde = (
            f" — solde {format_amount(account.balance)} {account.currency}"
            if account.balance is not None
            else ""
        )
        lignes.append(f"• {account.number} {account.label}{solde}")

    if result.transactions:
        debits = sum(-t.amount for t in result.transactions if t.amount < 0)
        credits = sum(t.amount for t in result.transactions if t.amount > 0)
        lignes += [
            "",
            f"Total débits  : {format_amount(debits)}",
            f"Total crédits : {format_amount(credits)}",
        ]

    lignes += ["", "—", "Message généré automatiquement par BankExtract."]
    return "\n".join(lignes)
