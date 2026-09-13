"""Sources de code d'authentification forte."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bankextract.config import OtpConfig
from bankextract.otp import available_providers, build_otp_provider, extract_code
from bankextract.otp.base import OtpTimeout


def test_extraction_du_code_dans_un_sms():
    assert extract_code("Banque: votre code est 458213. Valable 5 min.") == "458213"
    assert extract_code("Code 1234 puis 5678") == "1234"
    assert extract_code("aucun chiffre") is None


def test_motif_personnalise():
    texte = "Reference 99 - code de connexion : 4821"
    assert extract_code(texte, r"code de connexion\s*:\s*(\d{4})") == "4821"


def test_fournisseur_inconnu_signale_les_choix():
    with pytest.raises(ValueError, match="manual"):
        build_otp_provider(OtpConfig(provider="pigeon-voyageur"))


def test_registre_complet():
    assert set(available_providers()) == {"manual", "sms_gateway", "imap", "totp", "file"}


def test_totp_calcule_localement():
    provider = build_otp_provider(
        OtpConfig(provider="totp", options={"secret": "JBSWY3DPEHPK3PXP"})
    )
    code = provider.wait_for_code()
    assert code.isdigit() and len(code) == 6


def test_totp_sans_secret():
    with pytest.raises(ValueError, match="secret"):
        build_otp_provider(OtpConfig(provider="totp")).wait_for_code()


def test_fichier_depose_est_lu_puis_consomme(tmp_path):
    chemin = tmp_path / "otp.txt"
    chemin.write_text("Votre code est 774411")
    provider = build_otp_provider(
        OtpConfig(
            provider="file",
            options={"path": str(chemin)},
            timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
    )

    depuis = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert provider.wait_for_code(since=depuis) == "774411"
    assert not chemin.exists(), "le code doit être consommé pour ne pas être réutilisé"


def test_fichier_anterieur_ignore(tmp_path):
    """Un code laissé par une exécution précédente ne doit pas être accepté."""
    chemin = tmp_path / "otp.txt"
    chemin.write_text("ancien code 111111")
    provider = build_otp_provider(
        OtpConfig(
            provider="file",
            options={"path": str(chemin)},
            timeout_seconds=1,
            poll_interval_seconds=0.1,
        )
    )

    with pytest.raises(OtpTimeout):
        provider.wait_for_code(since=datetime.now(timezone.utc) + timedelta(seconds=30))


class _GatewayHandler(BaseHTTPRequestHandler):
    payload: dict = {}

    def log_message(self, *args):
        return

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def passerelle():
    """Petite passerelle SMS HTTP pilotable depuis les tests."""
    handler = type("Bound", (_GatewayHandler,), {"payload": {"messages": []}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}/messages"
    finally:
        server.shutdown()
        server.server_close()


def _provider(url: str, **options):
    return build_otp_provider(
        OtpConfig(
            provider="sms_gateway",
            options={"url": url, **options},
            timeout_seconds=3,
            poll_interval_seconds=0.1,
        )
    )


def test_passerelle_sms_lit_le_code(passerelle):
    handler, url = passerelle
    handler.payload = {
        "messages": [
            {
                "address": "BNA",
                "body": "Votre code de connexion est 903214",
                "date": datetime.now(timezone.utc).timestamp(),
            }
        ]
    }

    assert _provider(url).wait_for_code(since=datetime.now(timezone.utc)) == "903214"


def test_passerelle_sms_filtre_l_expediteur(passerelle):
    handler, url = passerelle
    maintenant = datetime.now(timezone.utc).timestamp()
    handler.payload = {
        "messages": [
            {"address": "PUB", "body": "Promo 123456", "date": maintenant},
            {"address": "BNA", "body": "Code 654321", "date": maintenant},
        ]
    }

    assert _provider(url, sender="BNA").wait_for_code(since=datetime.now(timezone.utc)) == "654321"


def test_passerelle_sms_ignore_les_sms_anterieurs(passerelle):
    handler, url = passerelle
    handler.payload = {
        "messages": [
            {
                "address": "BNA",
                "body": "Ancien code 111222",
                "date": (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp(),
            }
        ]
    }

    with pytest.raises(OtpTimeout):
        _provider(url, lookback_seconds=0).wait_for_code(since=datetime.now(timezone.utc))


def test_passerelle_sms_horodatage_iso_et_chemin_imbrique(passerelle):
    handler, url = passerelle
    handler.payload = {
        "data": {
            "items": [
                {
                    "from": "AGB",
                    "text": "code: 445566",
                    "receivedAt": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
    }

    provider = _provider(
        url,
        messages_path="data.items",
        text_field="text",
        sender_field="from",
        date_field="receivedAt",
    )
    assert provider.wait_for_code(since=datetime.now(timezone.utc)) == "445566"


def test_passerelle_sms_url_obligatoire():
    with pytest.raises(ValueError, match="url"):
        build_otp_provider(OtpConfig(provider="sms_gateway")).wait_for_code()
