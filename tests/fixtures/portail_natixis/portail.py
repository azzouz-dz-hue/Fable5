"""Portail factice calqué sur NATIXIS Algérie.

Reconstitué à partir des journaux d'un essai réel : noms des champs de
connexion, structure exacte du menu principal telle que révélée par le
sélecteur enregistré, choix du format, calendrier de période, et
téléchargement d'un relevé CSV.

Il sert à éprouver la chaîne complète — enregistrement, rejeu, analyse —
contre les particularités qui ont fait échouer les premiers essais :

- un sous-menu qui ne s'ouvre qu'au clic sur son entrée parente ;
- une période choisie dans un calendrier, et non tapée au clavier ;
- une connexion en deux temps, avec redirection.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

IDENTIFIANT = "0016777640"
MOT_DE_PASSE = "secret-natixis"
COMPTE = "00167 7764082001 45"

#: Écritures servies dans le relevé, figées pour que les tests soient stables.
OPERATIONS = [
    ("04/03/2026", "05/03/2026", "VIR RECU CLIENT SPA PHARMA", "", "1 250 000,00", "4 235 890,45"),
    ("03/03/2026", "03/03/2026", "REGLEMENT FOURNISSEUR IMPORT", "845 300,50", "", "2 985 890,45"),
    ("02/03/2026", "02/03/2026", "FRAIS TENUE DE COMPTE", "1 200,00", "", "3 831 190,95"),
    ("28/02/2026", "01/03/2026", "DOMICILIATION IMPORT DOM-2026-007", "312 450,75", "", "3 832 390,95"),
]

_ENTETE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Natixis Algérie EBanking</title>
<style>
  /* Le sous-menu ne s'ouvre qu'au clic : ni :hover ni les événements de
     survol n'y changent quoi que ce soit. */
  #menuPrincipal, #menuPrincipal ul { list-style: none; margin: 0; padding: 0; }
  #menuPrincipal > li { display: inline-block; position: relative; padding: 10px 16px;
                        background: #eef; cursor: pointer; }
  #menuPrincipal > li > ul { display: none; position: absolute; left: 0; top: 100%;
                             background: #fff; border: 1px solid #99a; min-width: 220px; z-index: 10; }
  #menuPrincipal > li.deploye > ul { display: block; }
  #menuPrincipal > li > ul > li { padding: 8px 14px; }
  table.calendrier td { padding: 6px 10px; border: 1px solid #ddd; cursor: pointer; }
</style></head><body>"""
_PIED = "</body></html>"


class PortailHandler(BaseHTTPRequestHandler):
    sessions: set

    def log_message(self, *args) -> None:
        return

    # ------------------------------------------------------------------ outils

    def _envoyer(self, corps: bytes, statut: int = 200, type_contenu: str = "text/html; charset=utf-8",
                 entetes: dict | None = None) -> None:
        self.send_response(statut)
        self.send_header("Content-Type", type_contenu)
        self.send_header("Content-Length", str(len(corps)))
        for cle, valeur in (entetes or {}).items():
            self.send_header(cle, valeur)
        self.end_headers()
        self.wfile.write(corps)

    def _page(self, contenu: str, **kwargs) -> None:
        self._envoyer((_ENTETE + contenu + _PIED).encode("utf-8"), **kwargs)

    def _rediriger(self, vers: str, cookie: str | None = None) -> None:
        entetes = {"Location": vers}
        if cookie:
            entetes["Set-Cookie"] = f"JSESSIONID={cookie}; Path=/"
        self._envoyer(b"", statut=303, entetes=entetes)

    def _connecte(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return any(
            part.strip().startswith("JSESSIONID=") and part.strip()[11:] in self.sessions
            for part in cookie.split(";")
        )

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        chemin = urlparse(self.path).path
        if chemin in ("/", "/ebanking/index.ebk"):
            return self._connexion()
        if not self._connecte():
            return self._rediriger("/ebanking/index.ebk")
        if chemin == "/ebanking/ebanking/listeComptes.ebk":
            return self._liste_comptes()
        if chemin == "/ebanking/ebanking/releves.ebk":
            return self._releves()
        if chemin == "/ebanking/ebanking/telecharger.ebk":
            return self._telecharger()
        return self._page("<h1>404</h1>", statut=404)

    def do_POST(self) -> None:  # noqa: N802
        longueur = int(self.headers.get("Content-Length", 0))
        champs = parse_qs(self.rfile.read(longueur).decode("utf-8")) if longueur else {}
        chemin = urlparse(self.path).path

        if chemin == "/ebanking/authentification.ebk":
            identifiant = (champs.get("login") or [""])[0]
            mot_de_passe = (champs.get("mdpAffiche") or [""])[0]
            if identifiant == IDENTIFIANT and mot_de_passe == MOT_DE_PASSE:
                jeton = "SESS" + str(abs(hash(identifiant)) % 10**8)
                self.sessions.add(jeton)
                return self._rediriger("/ebanking/ebanking/listeComptes.ebk", cookie=jeton)
            return self._connexion(erreur="Identifiant ou mot de passe incorrect.")

        if chemin == "/ebanking/ebanking/telecharger.ebk":
            return self._telecharger()
        return self._page("<h1>404</h1>", statut=404)

    # ------------------------------------------------------------------ pages

    def _connexion(self, erreur: str = "") -> None:
        bandeau = f'<p class="erreur">{erreur}</p>' if erreur else ""
        self._page(
            f"""<h1>Natixis Algérie EBanking</h1>{bandeau}
<form method="post" action="/ebanking/authentification.ebk">
  <input type="text" name="login" id="login" placeholder="Identifiant">
  <input type="password" name="mdpAffiche" id="mdpAffiche" placeholder="Mot de passe">
  <input type="submit" value="Connexion">
</form>"""
        )

    def _liste_comptes(self) -> None:
        """Le menu reproduit la structure qu'a révélée le sélecteur enregistré :
        #menuPrincipal > li:nth-of-type(2) > ul > li:nth-of-type(3) > a"""
        self._page(
            f"""<ul id="menuPrincipal">
  <li><a href="/ebanking/ebanking/listeComptes.ebk">Accueil</a></li>
  <li id="entreeComptes">Mes comptes
    <ul>
      <li><a href="#">Situation de trésorerie</a></li>
      <li><a href="#">Soldes</a></li>
      <li><a href="/ebanking/ebanking/releves.ebk">Relevés d'opérations</a></li>
    </ul>
  </li>
</ul>
<h2>Liste des comptes</h2>
<table><tr><td>{COMPTE}</td><td>Compte courant</td><td>4 235 890,45 DZD</td></tr></table>
<script>
  document.getElementById('entreeComptes').addEventListener('click', function (e) {{
    if (e.target === this) this.classList.toggle('deploye');
  }});
</script>"""
        )

    def _releves(self) -> None:
        jours = "".join(
            f'<td onclick="choisirJour({j})">{j}</td>' + ("</tr><tr>" if j % 7 == 0 else "")
            for j in range(1, 31)
        )
        self._page(
            f"""<h2>Relevés d'opérations</h2>
<form method="post" action="/ebanking/ebanking/telecharger.ebk">
  <button type="button" id="formatPdf">Adobe PDF</button>
  <a href="#" id="formatCsv">Excel (CSV)</a>
  <select name="format" id="choixFormat">
    <option value="pdf">Adobe PDF</option>
    <option value="csv">Excel (CSV)</option>
  </select>
  <i id="ouvrirCalendrier" title="Choisir une période">calendrier</i>
  <div id="calendrier" style="display:none">
    <table class="calendrier"><tr>{jours}</tr></table>
    <span id="validerJour">Valider</span>
  </div>
  <input type="hidden" name="du" id="du"><input type="hidden" name="au" id="au">
  <input type="submit" value="Télécharger">
</form>
<script>
  var jours = [];
  document.getElementById('ouvrirCalendrier').onclick = function () {{
    document.getElementById('calendrier').style.display = 'block';
  }};
  function choisirJour(j) {{
    jours.push(j);
    document.getElementById(jours.length === 1 ? 'du' : 'au').value = j;
  }}
  document.getElementById('validerJour').onclick = function () {{
    document.getElementById('calendrier').style.display = 'block';
  }};
</script>"""
        )

    def _telecharger(self) -> None:
        lignes = ["Date opération;Date valeur;Libellé;Débit;Crédit;Solde"]
        lignes += [";".join(operation) for operation in OPERATIONS]
        self._envoyer(
            ("\n".join(lignes) + "\n").encode("utf-8-sig"),
            type_contenu="text/csv; charset=utf-8",
            entetes={"Content-Disposition": 'attachment; filename="releve.csv"'},
        )


def demarrer_portail(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Démarre le portail factice et renvoie (serveur, url de connexion)."""
    handler = type("PortailLie", (PortailHandler,), {"sessions": set()})
    serveur = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{serveur.server_address[1]}"
    return serveur, f"{base}/ebanking/index.ebk"


if __name__ == "__main__":
    srv, url = demarrer_portail(port=8778)
    print(f"Portail factice : {url}  ({IDENTIFIANT} / {MOT_DE_PASSE})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
