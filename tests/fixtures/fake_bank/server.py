"""Faux portail e-banking servant de banc d'essai.

Reproduit le parcours réel d'un portail algérien : identifiants, OTP envoyé par
« SMS », liste de comptes, écritures paginées et relevés PDF téléchargeables.
Le code OTP est écrit dans un fichier, ce qui permet de tester la chaîne
d'authentification forte automatique de bout en bout.
"""

from __future__ import annotations

import random
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

USERNAME = "demo"
PASSWORD = "demo123"

ACCOUNTS = [
    {
        "number": "00100 987654321 09",
        "label": "Compte courant SARL",
        "currency": "DZD",
        "balance": "4 235 890,45",
        "type": "Compte courant",
    },
    {
        "number": "00100 987654322 41",
        "label": "Compte devises EUR",
        "currency": "EUR",
        "balance": "12 480,00",
        "type": "Compte devise",
    },
]

# Écritures figées : les tests doivent être reproductibles.
OPERATIONS = {
    "00100987654321 09".replace(" ", ""): [
        ("04/03/2026", "05/03/2026", "VIR RECU CLIENT SPA PHARMA", "", "1 250 000,00", "4 235 890,45", "VIR26030401"),
        ("03/03/2026", "03/03/2026", "REGLEMENT FOURNISSEUR IMPORT DM-2026-014", "845 300,50", "", "2 985 890,45", "CHQ0091823"),
        ("02/03/2026", "02/03/2026", "FRAIS TENUE DE COMPTE", "1 200,00", "", "3 831 190,95", ""),
        ("28/02/2026", "01/03/2026", "DOMICILIATION IMPORT DOM-2026-007", "312 450,75", "", "3 832 390,95", "DOM26007"),
        ("27/02/2026", "27/02/2026", "VIR RECU CLINIQUE EL AMEL", "", "480 000,00", "4 144 841,70", "VIR26022702"),
        ("25/02/2026", "25/02/2026", "PRELEVEMENT CNAS", "96 750,00", "", "3 664 841,70", ""),
        ("20/02/2026", "21/02/2026", "REMISE CHEQUE 4471209", "", "725 400,00", "3 761 591,70", "REM4471209"),
        ("18/02/2026", "18/02/2026", "COMMISSION TRANSFERT DEVISE", "18 900,25", "", "3 036 191,70", ""),
    ],
    "00100987654322 41".replace(" ", ""): [
        ("03/03/2026", "04/03/2026", "TRANSFERT FOURNISSEUR ALLEMAGNE", "8 400,00", "", "12 480,00", "TRF2026031"),
        ("26/02/2026", "26/02/2026", "APPROVISIONNEMENT DEVISE", "", "20 000,00", "20 880,00", ""),
        ("15/02/2026", "15/02/2026", "FRAIS SWIFT", "120,00", "", "880,00", ""),
    ],
}

PAGE_SIZE = 5

_PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 68>>stream\nBT /F1 12 Tf 20 150 Td (Releve de compte - Banque Demo) Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)

_HEAD = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Banque Demo</title></head><body>"""
_FOOT = "</body></html>"


class FakeBankState:
    """État partagé : sessions ouvertes et dernier code OTP émis."""

    def __init__(self, otp_file: Path):
        self.otp_file = otp_file
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()

    def open_session(self) -> str:
        token = f"sess{random.randint(10**8, 10**9)}"
        code = f"{random.randint(0, 999999):06d}"
        with self.lock:
            self.sessions[token] = {"otp": code, "authenticated": False}
        # « Envoi du SMS » : le fournisseur OTP « file » lira ce fichier.
        self.otp_file.parent.mkdir(parents=True, exist_ok=True)
        self.otp_file.write_text(
            f"Banque Demo: votre code de connexion est {code}. Valable 5 minutes.",
            encoding="utf-8",
        )
        return token

    def verify(self, token: str, code: str) -> bool:
        with self.lock:
            session = self.sessions.get(token)
            if session and session["otp"] == code:
                session["authenticated"] = True
                return True
        return False

    def is_authenticated(self, token: str | None) -> bool:
        with self.lock:
            return bool(token and self.sessions.get(token, {}).get("authenticated"))


class FakeBankHandler(BaseHTTPRequestHandler):
    state: FakeBankState

    def log_message(self, *args) -> None:  # silence dans la sortie des tests
        return

    # ------------------------------------------------------------------ helpers

    def _send(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8",
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str, **kwargs) -> None:
        self._send((_HEAD + content + _FOOT).encode("utf-8"), **kwargs)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = f"session={cookie}; Path=/"
        self._send(b"", status=303, extra_headers=headers)

    def _token(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session":
                return value
        return None

    def _body_params(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", 0))
        return parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/login"):
            return self._page_login()
        if path == "/otp":
            return self._page_otp()
        if path == "/accounts":
            return self._page_accounts()
        if path.startswith("/accounts/") and path.endswith("/operations"):
            return self._page_operations(path.split("/")[2], query)
        if path.startswith("/accounts/") and path.endswith("/statements"):
            return self._page_statements(path.split("/")[2])
        if path.startswith("/download/"):
            return self._download(path.rsplit("/", 1)[-1])
        if path == "/logout":
            return self._redirect("/")
        return self._html("<h1>404</h1>", status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = self._body_params()

        if parsed.path == "/login":
            username = (params.get("username") or [""])[0]
            password = (params.get("password") or [""])[0]
            if username == USERNAME and password == PASSWORD:
                return self._redirect("/otp", cookie=self.state.open_session())
            return self._page_login(error="Identifiant ou mot de passe incorrect.")

        if parsed.path == "/otp":
            code = (params.get("otp") or [""])[0].strip()
            if self.state.verify(self._token() or "", code):
                return self._redirect("/accounts")
            return self._page_otp(error="Code incorrect ou expiré.")

        return self._html("<h1>404</h1>", status=404)

    # ------------------------------------------------------------------ pages

    def _page_login(self, error: str = "") -> None:
        banner = f'<div class="alert-danger">{error}</div>' if error else ""
        self._html(
            f"""<h1>Banque Demo — Espace client</h1>{banner}
<form method="post" action="/login">
  <input id="username" name="username" placeholder="Identifiant">
  <input id="password" name="password" type="password" placeholder="Mot de passe">
  <button type="submit" id="login-submit">Se connecter</button>
</form>"""
        )

    def _page_otp(self, error: str = "") -> None:
        banner = f'<div class="alert-danger">{error}</div>' if error else ""
        self._html(
            f"""<h1>Authentification forte</h1>{banner}
<p>Un code vous a été envoyé par SMS.</p>
<form method="post" action="/otp">
  <input id="otp" name="otp" placeholder="Code à 6 chiffres">
  <button type="submit" id="otp-submit">Valider</button>
</form>"""
        )

    def _page_accounts(self) -> None:
        if not self.state.is_authenticated(self._token()):
            return self._redirect("/")
        rows = "".join(
            f"""<tr>
  <td class="num"><a href="/accounts/{a['number'].replace(' ', '')}/operations">{a['number']}</a></td>
  <td class="lbl">{a['label']}</td>
  <td class="typ">{a['type']}</td>
  <td class="cur">{a['currency']}</td>
  <td class="bal">{a['balance']}</td>
</tr>"""
            for a in ACCOUNTS
        )
        self._html(
            f"""<h1 class="dashboard">Mes comptes</h1>
<table class="accounts"><tbody>{rows}</tbody></table>
<a href="/logout" id="logout">Déconnexion</a>"""
        )

    def _page_operations(self, number: str, query: dict[str, list[str]]) -> None:
        if not self.state.is_authenticated(self._token()):
            return self._redirect("/")
        operations = OPERATIONS.get(number, [])
        page = int((query.get("page") or ["1"])[0])
        chunk = operations[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]

        rows = "".join(
            f"""<tr>
  <td class="d">{op[0]}</td><td class="dv">{op[1]}</td><td class="lb">{op[2]}</td>
  <td class="db">{op[3]}</td><td class="cr">{op[4]}</td><td class="sd">{op[5]}</td>
  <td class="rf">{op[6]}</td>
</tr>"""
            for op in chunk
        )
        has_next = page * PAGE_SIZE < len(operations)
        next_link = (
            f'<a class="next" href="/accounts/{number}/operations?page={page + 1}">Suivant</a>'
            if has_next
            else '<a class="next disabled">Suivant</a>'
        )
        self._html(
            f"""<h1>Opérations {number}</h1>
<table class="operations"><tbody>{rows}</tbody></table>
{next_link}
<a href="/accounts/{number}/statements">Relevés</a>"""
        )

    def _page_statements(self, number: str) -> None:
        if not self.state.is_authenticated(self._token()):
            return self._redirect("/")
        periods = [(date.today().replace(day=1) - timedelta(days=31 * i)) for i in range(3)]
        rows = "".join(
            f"""<tr>
  <td class="per">{p.strftime('%d/%m/%Y')}</td>
  <td><a class="dl" href="/download/releve-{number}-{p:%Y%m}.pdf">Télécharger</a></td>
  <td><a class="dl-csv" href="/download/releve-{number}-{p:%Y%m}.csv">Export CSV</a></td>
</tr>"""
            for p in periods
        )
        self._html(f'<h1>Relevés</h1><table class="statements"><tbody>{rows}</tbody></table>')

    def _download(self, filename: str) -> None:
        if filename.endswith(".csv"):
            return self._send(
                _statement_csv(filename),
                content_type="text/csv; charset=utf-8",
                extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        self._send(
            _PDF_BYTES,
            content_type="application/pdf",
            extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _statement_csv(filename: str) -> bytes:
    """Relevé CSV aux conventions francophones : point-virgule, virgule décimale."""
    number = filename.removeprefix("releve-").split("-")[0]
    lignes = ["Date opération;Date valeur;Libellé;Débit;Crédit;Solde;Référence"]
    for op in OPERATIONS.get(number, []):
        lignes.append(";".join(op))
    return ("\n".join(lignes) + "\n").encode("utf-8-sig")


def start_fake_bank(otp_file: Path, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Démarre le faux portail sur un port libre et renvoie (serveur, url)."""
    handler = type("BoundHandler", (FakeBankHandler,), {"state": FakeBankState(otp_file)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":  # lancement manuel : python tests/fixtures/fake_bank/server.py
    srv, url = start_fake_bank(Path("data/otp.txt"), port=8777)
    print(f"Faux portail disponible sur {url} (demo / demo123)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
