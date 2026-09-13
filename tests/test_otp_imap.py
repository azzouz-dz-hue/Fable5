"""Lecture de l'OTP dans une boîte e-mail.

Aucun serveur IMAP n'est joignable depuis les tests : le client est remplacé
par un double qui rejoue des messages réels, ce qui suffit à éprouver ce qui
compte ici — le filtrage par date et par expéditeur, la lecture des messages
multipart, et le choix du code.
"""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from bankextract.config import OtpConfig
from bankextract.otp import build_otp_provider
from bankextract.otp.base import OtpTimeout
from bankextract.otp.imap_mail import _body

MAINTENANT = datetime.now(timezone.utc)


def _message(corps: str, sujet: str = "Code de connexion", quand=None, html: str = "") -> bytes:
    message = EmailMessage()
    message["Subject"] = sujet
    message["From"] = "noreply@bna.dz"
    message["Date"] = (quand or MAINTENANT).strftime("%a, %d %b %Y %H:%M:%S %z")
    message.set_content(corps)
    if html:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


class FauxImap:
    """Double du client imaplib, réduit à ce que le fournisseur utilise."""

    def __init__(self, messages: list[bytes], echec_recherche: bool = False):
        self.messages = messages
        self.echec_recherche = echec_recherche
        self.criteres: list = []
        self.connecte = False

    def __enter__(self):
        self.connecte = True
        return self

    def __exit__(self, *_):
        self.connecte = False
        return False

    def login(self, user, password):
        self.identifiants = (user, password)
        return ("OK", [b""])

    def select(self, mailbox):
        self.boite = mailbox
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.criteres = list(criteria)
        if self.echec_recherche:
            return ("NO", [b""])
        if not self.messages:
            return ("OK", [b""])
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))])

    def fetch(self, uid, parts):
        index = int(uid) - 1
        return ("OK", [(b"1 (RFC822 {...}", self.messages[index])])


@pytest.fixture
def brancher(monkeypatch):
    """Remplace imaplib.IMAP4_SSL et renvoie le double utilisé."""
    boite = {}

    def poser(messages, **kwargs):
        faux = FauxImap(messages, **kwargs)
        boite["client"] = faux
        monkeypatch.setattr("imaplib.IMAP4_SSL", lambda host, port: faux)
        return faux

    return poser


def _provider(**options):
    return build_otp_provider(
        OtpConfig(
            provider="imap",
            options={
                "host": "imap.exemple.dz",
                "user": "compta@exemple.dz",
                "password": "secret",
                **options,
            },
            timeout_seconds=2,
            poll_interval_seconds=0.1,
        )
    )


def test_code_lu_dans_un_message_simple(brancher):
    brancher([_message("Votre code de connexion est 903214.")])

    assert _provider().wait_for_code(since=MAINTENANT) == "903214"


def test_code_lu_dans_le_sujet(brancher):
    brancher([_message("Bonjour,", sujet="Votre code : 445566")])

    assert _provider().wait_for_code(since=MAINTENANT) == "445566"


def test_message_multipart_lu(brancher):
    """Les banques envoient souvent une version texte et une version HTML."""
    brancher([_message("Voir la version HTML.", html="<p>Code : <b>778899</b></p>")])

    assert _provider().wait_for_code(since=MAINTENANT) == "778899"


def test_message_anterieur_ignore(brancher):
    """Un ancien code ne doit jamais être réutilisé."""
    brancher([_message("code 111222", quand=MAINTENANT - timedelta(hours=3))])

    with pytest.raises(OtpTimeout):
        _provider().wait_for_code(since=MAINTENANT)


def test_message_le_plus_recent_prioritaire(brancher):
    brancher(
        [
            _message("ancien code 111111", quand=MAINTENANT - timedelta(minutes=1)),
            _message("nouveau code 222222"),
        ]
    )

    assert _provider().wait_for_code(since=MAINTENANT - timedelta(minutes=5)) == "222222"


def test_expediteur_transmis_au_serveur(brancher):
    """Le filtrage par expéditeur doit se faire côté serveur, pas après coup."""
    faux = brancher([_message("code 903214")])

    _provider(**{"from": "noreply@bna.dz"}).wait_for_code(since=MAINTENANT)

    assert "FROM" in faux.criteres and "noreply@bna.dz" in faux.criteres


def test_recherche_couvre_la_veille(brancher):
    """IMAP ne filtre qu'à la journée : chercher depuis la veille évite de rater
    un message reçu juste avant minuit."""
    faux = brancher([_message("code 903214")])

    _provider().wait_for_code(since=MAINTENANT)

    attendu = (MAINTENANT - timedelta(days=1)).strftime("%d-%b-%Y")
    assert faux.criteres[0] == "SINCE" and faux.criteres[1] == attendu


def test_boite_aux_lettres_configurable(brancher):
    faux = brancher([_message("code 903214")])

    _provider(mailbox="Banque").wait_for_code(since=MAINTENANT)

    assert faux.boite == "Banque"


def test_motif_personnalise(brancher):
    brancher([_message("Reference 99 — code de connexion : 4821")])

    provider = _provider(code_pattern=r"code de connexion\s*:\s*(\d{4})")
    assert provider.wait_for_code(since=MAINTENANT) == "4821"


def test_recherche_en_echec_ne_leve_pas(brancher):
    brancher([_message("code 903214")], echec_recherche=True)

    with pytest.raises(OtpTimeout):
        _provider().wait_for_code(since=MAINTENANT)


def test_boite_vide(brancher):
    brancher([])

    with pytest.raises(OtpTimeout):
        _provider().wait_for_code(since=MAINTENANT)


def test_mot_de_passe_lu_dans_l_environnement(brancher, monkeypatch):
    monkeypatch.setenv("OTP_IMAP_PASSWORD", "depuis-env")
    faux = brancher([_message("code 903214")])

    provider = build_otp_provider(
        OtpConfig(
            provider="imap",
            options={
                "host": "imap.exemple.dz",
                "user": "compta@exemple.dz",
                "password_env": "OTP_IMAP_PASSWORD",
            },
            timeout_seconds=2,
            poll_interval_seconds=0.1,
        )
    )
    provider.wait_for_code(since=MAINTENANT)

    assert faux.identifiants == ("compta@exemple.dz", "depuis-env")


@pytest.mark.parametrize(
    "options, manquant",
    [
        ({"user": "u", "password": "p"}, "host"),
        ({"host": "h", "password": "p"}, "user"),
        ({"host": "h", "user": "u"}, "password_env"),
    ],
)
def test_configuration_incomplete_signalee(options, manquant):
    provider = build_otp_provider(OtpConfig(provider="imap", options=options))

    with pytest.raises(ValueError, match=manquant.split("_")[0]):
        provider.wait_for_code()


def test_extraction_du_corps_multipart():
    message = EmailMessage()
    message.set_content("version texte")
    message.add_alternative("<p>version HTML</p>", subtype="html")

    corps = _body(message)

    assert "version texte" in corps and "version HTML" in corps
