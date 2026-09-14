"""Rejeu d'un parcours enregistré.

Le connecteur ne connaît rien du portail : il exécute les étapes du scénario,
remplace les jetons par les vraies valeurs au dernier moment, et récupère les
fichiers téléchargés. Ceux-ci sont ensuite analysés pour alimenter la base, ce
qui referme la boucle enregistrement → relevé → écritures.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..browser import BrowserSession, browser_session
from ..connectors.base import BankConnector, ScrapingError
from ..models import Account, ExtractionResult, StatementFile, Transaction
from ..parsers import parse_statement
from ..secrets import Credentials, get_credentials
from .scenario import (
    TOKEN_END,
    TOKEN_OTP,
    TOKEN_PASSWORD,
    TOKEN_START,
    TOKEN_USERNAME,
    ActionType,
    Scenario,
    Step,
    est_un_clic_de_jour,
)

logger = logging.getLogger(__name__)

#: Un sélecteur candidat est testé brièvement : s'il ne répond pas, on passe au
#: suivant plutôt que d'attendre le délai complet sur chacun.
CANDIDATE_TIMEOUT_MS = 4_000

#: Ouvrir un menu est immédiat : inutile d'attendre longtemps à chaque essai.
REVEAL_TIMEOUT_MS = 1_500

#: Attribut posé le temps d'identifier les parents à déplier, puis retiré.
MARQUEUR_MENU = "data-bankextract-parent"

#: Endroits où un portail annonce qu'il n'a rien à livrer.
SELECTEURS_DE_MESSAGE = (
    "[role=alert]",
    ".alert",
    ".alert-danger",
    ".erreur",
    ".error",
    ".message-erreur",
    ".msg-erreur",
    ".text-danger",
    ".notification",
)

#: Un message d'erreur tient en quelques lignes ; au-delà, c'est la page entière.
LONGUEUR_MAX_MESSAGE = 300

#: Un portail refuse aussi dans une fenêtre qu'il dessine lui-même.
SELECTEURS_DE_FENETRE = (
    ".modal",
    ".ui-dialog",
    "[class*='dialog']",
    "[class*='modal']",
    "[class*='popup']",
    "[id*='dialog']",
    "[id*='modal']",
)

#: Au-delà, un champ qui n'accepte pas la saisie ne l'acceptera pas davantage :
#: on passe à l'écriture depuis la page plutôt que d'attendre le délai complet.
DELAI_SAISIE_MS = 3_000


#: Reconnaît les champs de période dans la page, et les note.
#:
#: Aucun sélecteur de banque n'y figure : un portail change ses identifiants,
#: pas la nature de ses champs. Les indices s'additionnent pour que deux champs
#: manifestement liés à une date l'emportent sur un troisième qui n'y ressemble
#: que de loin.
_DETECTEUR_DE_CHAMPS_DE_DATE = r"""() => {
  const MOTS_ENTIERS = ["du", "au", "de", "fin", "end", "start", "from", "to", "jour", "day"];
  const MORCEAUX = ["date", "calend", "periode", "période", "debut", "début", "picker"];
  const EST_UNE_DATE = /^\s*\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}\s*$/;

  const motDeDate = (texte) => {
    const t = (texte || "").trim().toLowerCase();
    if (!t) return false;
    if (MOTS_ENTIERS.includes(t)) return true;
    return MORCEAUX.some((morceau) => t.includes(morceau));
  };

  const classeDe = (noeud) => {
    const brut = noeud.className;
    return typeof brut === "string" ? brut : (brut && brut.baseVal) || "";
  };

  /* Le texte qui annonce le champ : son étiquette, ou ce qui le précède
     immédiatement — « Période du », « au ». */
  const etiquetteDe = (champ) => {
    const morceaux = [];
    if (champ.id) {
      const etiquette = document.querySelector('label[for="' + CSS.escape(champ.id) + '"]');
      if (etiquette) morceaux.push(etiquette.textContent);
    }
    const englobante = champ.closest("label");
    if (englobante) morceaux.push(englobante.textContent);
    let precedent = champ.previousSibling;
    for (let garde = 0; precedent && garde < 3; garde += 1) {
      if (precedent.nodeType === 3 || precedent.nodeType === 1) {
        const texte = (precedent.textContent || "").trim();
        if (texte) morceaux.push(texte);
      }
      precedent = precedent.previousSibling;
    }
    return morceaux;
  };

  /* Une icône de calendrier posée à côté du champ le désigne sans ambiguïté. */
  const icôneDeCalendrier = (champ) => {
    const voisins = [];
    let suivant = champ.nextElementSibling;
    for (let garde = 0; suivant && garde < 3; garde += 1) {
      voisins.push(suivant);
      suivant = suivant.nextElementSibling;
    }
    return voisins.some((voisin) => {
      const soupe = [
        classeDe(voisin),
        voisin.id,
        voisin.getAttribute("title"),
        voisin.getAttribute("alt"),
        voisin.getAttribute("aria-label"),
        voisin.textContent,
      ]
        .join(" ")
        .toLowerCase();
      return soupe.includes("calend") || soupe.includes("datepick");
    });
  };

  const IGNORES = ["hidden", "password", "checkbox", "radio", "submit", "button", "image", "file"];
  const notes = [];
  const champs = document.querySelectorAll("input");

  for (let rang = 0; rang < champs.length; rang += 1) {
    const champ = champs[rang];
    const type = (champ.getAttribute("type") || "text").toLowerCase();
    if (IGNORES.includes(type) || champ.disabled) continue;
    const boite = champ.getBoundingClientRect();
    if (boite.width === 0 && boite.height === 0) continue;

    let note = 0;
    if (type === "date") note += 3;
    if (EST_UNE_DATE.test(champ.value)) note += 3;
    const attributs = [
      champ.name,
      champ.id,
      champ.getAttribute("placeholder"),
      champ.getAttribute("aria-label"),
      champ.getAttribute("title"),
    ];
    if (attributs.some(motDeDate)) note += 2;
    if (etiquetteDe(champ).some(motDeDate)) note += 2;
    if (icôneDeCalendrier(champ)) note += 2;

    if (note > 0) notes.push({ rang: rang, note: note });
  }
  return notes;
}"""


class StepFailure(ScrapingError):
    """Une étape du scénario n'a pas pu être rejouée."""


class ScenarioConnector(BankConnector):
    """Connecteur qui rejoue un parcours enregistré au lieu de suivre des sélecteurs figés."""

    name = "scenario"
    display_name = "Parcours enregistré"

    def __init__(self, config, settings, otp=None, scenario: Scenario | None = None):
        super().__init__(config=config, settings=settings, otp=otp)
        self._scenario = scenario
        self._dernier_dialogue = ""

    # ------------------------------------------------------------------ scénario

    @property
    def scenario(self) -> Scenario:
        if self._scenario is None:
            self._scenario = Scenario.load(self.scenario_path)
        return self._scenario

    @property
    def scenario_path(self) -> Path:
        configured = (self.config.options or {}).get("scenario_path")
        if configured:
            return Path(configured)
        directory = Path((self.config.options or {}).get("scenarios_dir", "scenarios"))
        return directory / f"{self.name}.json"

    # ------------------------------------------------------------------ contrat de base

    def login(self, session: BrowserSession, credentials: Credentials) -> None:
        """Le scénario porte lui-même la connexion : rien à faire séparément."""
        return None

    def fetch_accounts(self, session: BrowserSession) -> list[Account]:
        """Le compte est décrit dans la configuration : le parcours ne le lit pas."""
        return [self._declared_account()]

    def fetch_transactions(
        self, session: BrowserSession, account: Account, start: date, end: date
    ) -> list[Transaction]:
        return []

    @property
    def _delai_de_telechargement(self) -> int:
        """Attente accordée au relevé, réglable à part du délai des pages."""
        configure = (self.config.options or {}).get("download_timeout_ms")
        return int(configure) if configure else self.settings.browser.timeout_ms

    def _declared_account(self) -> Account:
        options = self.config.options or {}
        return Account(
            bank=self.name,
            number=str(options.get("account_number") or self.name),
            label=str(options.get("account_label") or self.config.display_name),
            currency=str(options.get("currency", "DZD")),
        )

    # ------------------------------------------------------------------ exécution

    def run(self, start: date | None = None, end: date | None = None) -> ExtractionResult:
        """Rejoue le parcours, récupère les relevés et en extrait les écritures."""
        end = end or date.today()
        start = start or end - timedelta(days=self.config.history_days)
        result = ExtractionResult(bank=self.name)

        try:
            scenario = self.scenario
        except Exception as exc:
            result.errors.append(f"Scénario : {exc}")
            result.finished_at = datetime.now()
            return result

        try:
            credentials = get_credentials(self.name, self.config)
        except Exception as exc:
            result.errors.append(f"Identifiants : {exc}")
            result.finished_at = datetime.now()
            return result

        account = self._declared_account()
        result.accounts = [account]
        destination = self.settings.paths.downloads_dir / self.name

        try:
            with browser_session(self.settings.browser, self.name) as session:
                try:
                    logger.info(
                        "[%s] rejeu de « %s » — %s",
                        self.name,
                        scenario.display_name,
                        scenario.summary(),
                    )
                    files = self._replay(session, scenario, credentials, start, end, destination)
                    result.files = [
                        StatementFile(account_key=account.key, path=path) for path in files
                    ]
                    for statement in result.files:
                        statement.compute_sha256()
                except Exception as exc:
                    logger.exception("[%s] rejeu interrompu", self.name)
                    result.errors.append(f"{type(exc).__name__}: {exc}")
                    if self.settings.browser.screenshot_on_error and (
                        shot := session.screenshot("rejeu")
                    ):
                        result.errors.append(f"Capture d'écran : {shot}")
        except Exception as exc:
            result.errors.append(f"Navigateur : {type(exc).__name__}: {exc}")

        result.transactions = self._parse_files(result, account, start, end)
        result.finished_at = datetime.now()
        return result

    def _parse_files(
        self, result: ExtractionResult, account: Account, start: date, end: date
    ) -> list[Transaction]:
        """Analyse les relevés téléchargés ; un fichier illisible n'arrête pas les autres."""
        if not (self.config.options or {}).get("parse_downloads", True):
            return []

        transactions: list[Transaction] = []
        for statement in result.files:
            try:
                found = parse_statement(statement.path, account.key, currency=account.currency)
            except Exception as exc:
                result.errors.append(f"Analyse {statement.path.name}: {exc}")
                continue
            kept = [t for t in found if start <= t.date <= end]
            logger.info(
                "[%s] %s : %d écriture(s) retenue(s) sur %d",
                self.name,
                statement.path.name,
                len(kept),
                len(found),
            )
            transactions.extend(kept)
        return transactions

    # ------------------------------------------------------------------ moteur de rejeu

    def _replay(
        self,
        session: BrowserSession,
        scenario: Scenario,
        credentials: Credentials,
        start: date,
        end: date,
        destination: Path,
    ) -> list[Path]:
        """Exécute les étapes dans l'ordre et renvoie les fichiers obtenus."""
        downloaded: list[Path] = []
        date_format = str((self.config.options or {}).get("date_format", "%d/%m/%Y"))
        otp_requested_at = datetime.now(timezone.utc)

        periode_a_corriger = scenario.periode_figee
        self._ecouter_les_fenetres(session)

        for number, step in enumerate(scenario.steps, start=1):
            logger.info("[%s] %2d/%d — %s", self.name, number, len(scenario.steps), step.describe())

            # Un clic sur une case de calendrier ne peut que nuire : il désigne
            # une position dans le mois affiché, non une date, et laisse un
            # calendrier ouvert par-dessus la page. La période sera écrite.
            if periode_a_corriger and est_un_clic_de_jour(step):
                logger.info("     ↳ clic de calendrier ignoré : la période sera écrite directement")
                continue

            if step.action is ActionType.DOWNLOAD and periode_a_corriger:
                self._corriger_la_periode(session, start, end, date_format)
                periode_a_corriger = False
            try:
                path = self._run_step(
                    session,
                    step,
                    credentials,
                    start,
                    end,
                    date_format,
                    destination,
                    otp_requested_at,
                )
            except Exception as exc:
                if step.optional:
                    logger.info("     ↳ étape facultative ignorée (%s)", type(exc).__name__)
                    continue
                raise StepFailure(
                    f"étape {number} ({step.describe()}) : {type(exc).__name__}: {exc}"
                ) from exc
            if path is not None:
                downloaded.append(path)
            if step.wait_ms:
                session.page.wait_for_timeout(step.wait_ms)

        if not downloaded:
            logger.warning("[%s] aucun fichier téléchargé par ce parcours", self.name)
        return downloaded

    def _run_step(
        self,
        session: BrowserSession,
        step: Step,
        credentials: Credentials,
        start: date,
        end: date,
        date_format: str,
        destination: Path,
        otp_requested_at: datetime,
    ) -> Path | None:
        """Exécute une étape ; renvoie le chemin du fichier si elle en produit un."""
        if step.action is ActionType.GOTO:
            if not step.url:
                raise StepFailure("navigation sans URL dans le scénario")
            session.goto(step.url)
            return None

        if step.action is ActionType.KEYPAD:
            self._type_on_keypad(session, step, credentials, otp_requested_at)
            session.page.wait_for_load_state("domcontentloaded")
            return None

        target = self._resolve(session, step)
        value = self._resolve_value(step, credentials, start, end, date_format, otp_requested_at)

        if step.action is ActionType.FILL:
            self._ecrire(target, value or "")
        elif step.action is ActionType.SELECT:
            target.select_option(value or "")
        elif step.action is ActionType.CHECK:
            target.check() if value != "false" else target.uncheck()
        elif step.action is ActionType.PRESS:
            target.press(value or "Enter")
        elif step.action is ActionType.WAIT:
            session.page.wait_for_timeout(int(value or 1000))
        elif step.action is ActionType.CLICK:
            self._cliquer(target)
        elif step.action is ActionType.DOWNLOAD:
            try:
                return session.download_to(
                    lambda: self._cliquer(target),
                    destination,
                    timeout_ms=self._delai_de_telechargement,
                )
            except Exception as exc:
                message = self._message_affiche(session)
                if message:
                    raise StepFailure(
                        "la banque n'a envoyé aucun fichier. Elle affiche : "
                        f"« {message} »"
                    ) from exc
                raise

        session.page.wait_for_load_state("domcontentloaded")
        return None

    def _type_on_keypad(
        self,
        session: BrowserSession,
        step: Step,
        credentials: Credentials,
        otp_requested_at: datetime,
    ) -> None:
        """Clique les touches d'un clavier virtuel, caractère par caractère."""
        if not step.key_template:
            raise StepFailure("clavier virtuel sans gabarit de touche dans le scénario")

        secret = (
            credentials.password
            if step.value == TOKEN_PASSWORD
            else self.otp.wait_for_code(since=otp_requested_at, hint=f"Code {self.display_name}")
            if step.value == TOKEN_OTP
            else step.value or ""
        )

        scope = self._frame(session, step)
        for character in secret:
            selector = step.key_template.replace("{c}", character)
            try:
                touche = scope.wait_for_selector(selector, timeout=CANDIDATE_TIMEOUT_MS)
            except Exception as exc:
                # Le caractère n'est jamais cité dans le message : il ferait fuiter le code.
                raise StepFailure(
                    "touche introuvable sur le clavier virtuel — le gabarit "
                    f"« {step.key_template} » ne correspond plus"
                ) from exc
            touche.click()

    # ------------------------------------------------------------------ période

    def _corriger_la_periode(
        self, session: BrowserSession, start: date, end: date, date_format: str
    ) -> None:
        """Écrit la période demandée dans les champs de date, juste avant le téléchargement.

        Un parcours dont les dates ont été choisies dans un calendrier ne
        contient que des clics. Rejoués, ils ne désignent pas les mêmes dates :
        une case n'est qu'une position dans le mois affiché. L'extraction
        rapporterait alors une période arbitraire — et le ferait en silence,
        ce qui est le pire des cas.

        Les champs sont reconnus par plusieurs indices et non par un sélecteur
        propre à un portail. Se fier à leur seul contenu ne suffit pas : un
        champ que le calendrier a vidé est encore un champ de date, et c'est
        précisément celui-là qu'il faut renseigner.
        """
        self._fermer_les_calendriers(session)
        champs = self._champs_de_date(session)
        if not champs:
            logger.warning(
                "[%s] période choisie au calendrier, mais aucun champ de date reconnu sur "
                "la page : la banque décidera seule de la période",
                self.name,
            )
            return

        debut, fin = start.strftime(date_format), end.strftime(date_format)
        # Un seul champ ne peut porter qu'une borne : c'est la fin qui compte,
        # un portail à date unique livrant l'historique qui s'y arrête.
        champs, voulu = (champs[:1], [fin]) if len(champs) == 1 else (champs[:2], [debut, fin])

        manques = [
            texte
            for champ, texte in zip(champs, voulu, strict=True)
            if not self._ecrire(champ, texte)
        ]
        self._fermer_les_calendriers(session)

        if manques:
            logger.warning(
                "[%s] période refusée par la page : %s n'a pas pu être écrit",
                self.name,
                ", ".join(manques),
            )
        else:
            logger.info("[%s] période écrite dans les champs : %s", self.name, " → ".join(voulu))

    def _champs_de_date(self, session: BrowserSession) -> list:
        """Champs de date visibles, les deux plus probables d'abord, en ordre de page.

        Quatre indices, chacun suffisant à lui seul, aucun propre à une banque :
        le type déclaré du champ, une date déjà présente, un mot de date dans
        ses attributs ou son étiquette, et la présence d'une icône de calendrier
        à côté de lui. Les indices s'additionnent, et seuls les deux champs les
        mieux notés sont retenus — un formulaire porte parfois d'autres champs
        qui ressemblent de loin à des dates.
        """
        try:
            notes = session.page.evaluate(_DETECTEUR_DE_CHAMPS_DE_DATE)
            candidats = session.page.query_selector_all("input")
        except Exception:
            return []

        retenus = sorted(notes, key=lambda note: (-note["note"], note["rang"]))[:2]
        return [candidats[note["rang"]] for note in sorted(retenus, key=lambda n: n["rang"])]

    def _ecrire(self, champ, texte: str) -> bool:
        """Renseigne un champ et vérifie que la valeur a bien pris.

        Beaucoup de portails verrouillent le champ de date pour forcer le
        passage par leur calendrier. `fill` refuse alors d'écrire ; on pose la
        valeur depuis la page et on émet les événements que le portail attend,
        faute de quoi il ne verrait pas le changement. La vérification est le
        point important : écrire sans regarder, c'est ce qui a laissé partir une
        demande dont le champ de début était resté vide.
        """
        if not self._verrouille(champ):
            try:
                champ.fill(texte, timeout=DELAI_SAISIE_MS)
            except Exception:
                logger.info("     ↳ saisie refusée — écriture depuis la page")

        if self._valeur(champ) != texte:
            self._ecrire_depuis_la_page(champ, texte)
        return self._valeur(champ) == texte

    def _verrouille(self, champ) -> bool:
        try:
            return bool(champ.evaluate("cible => cible.readOnly || cible.disabled"))
        except Exception:
            return False

    def _valeur(self, champ) -> str:
        try:
            return champ.input_value() or ""
        except Exception:
            return ""

    def _ecrire_depuis_la_page(self, champ, texte: str) -> None:
        """Pose la valeur et émet les événements, même sur un champ verrouillé."""
        try:
            champ.evaluate(
                """(cible, texte) => {
                    cible.removeAttribute('readonly');
                    cible.value = texte;
                    for (const nom of ['input', 'change', 'blur']) {
                        cible.dispatchEvent(new Event(nom, { bubbles: true }));
                    }
                }""",
                texte,
            )
        except Exception as exc:
            logger.info("     ↳ écriture depuis la page impossible (%s)", type(exc).__name__)

    def _fermer_les_calendriers(self, session: BrowserSession) -> None:
        """Referme un calendrier resté ouvert, qui masquerait la suite du formulaire."""
        try:
            session.page.keyboard.press("Escape")
        except Exception:
            logger.debug("Aucun calendrier à refermer")

    # ------------------------------------------------------------------ diagnostic

    def _ecouter_les_fenetres(self, session: BrowserSession) -> None:
        """Retient ce que disent les fenêtres d'alerte du navigateur, puis les ferme.

        Sans écoute, Playwright les referme sans rien en dire : le portail a
        donné sa raison et personne ne l'a lue.
        """
        self._dernier_dialogue = ""

        def noter(dialogue) -> None:
            self._dernier_dialogue = " ".join((dialogue.message or "").split())
            logger.info("[%s] le portail affiche : %s", self.name, self._dernier_dialogue)
            try:
                dialogue.dismiss()
            except Exception:  # pragma: no cover - fenêtre déjà refermée
                logger.debug("Fenêtre déjà refermée")

        try:
            session.page.on("dialog", noter)
        except Exception:  # pragma: no cover
            logger.debug("Écoute des fenêtres impossible")

    def _message_affiche(self, session: BrowserSession) -> str:
        """Texte que le portail affiche, quand il refuse au lieu de livrer.

        Un téléchargement qui n'arrive pas se manifeste par un délai expiré, ce
        qui ne dit rien de la cause. Le portail, lui, l'a écrite en toutes
        lettres — « Aucune opération disponible sur ce compte pour la période
        choisie », ou « Le champ 'Date' est obligatoire ». C'est cette phrase
        qu'il faut rapporter, et non le délai.
        """
        if self._dernier_dialogue:
            return self._dernier_dialogue[:LONGUEUR_MAX_MESSAGE]

        for selecteur in SELECTEURS_DE_MESSAGE + SELECTEURS_DE_FENETRE:
            try:
                elements = session.page.query_selector_all(selecteur)
            except Exception:
                continue
            for element in elements:
                try:
                    if not element.is_visible():
                        continue
                    texte = " ".join((element.inner_text() or "").split())
                except Exception:
                    continue
                if texte:
                    return texte[:LONGUEUR_MAX_MESSAGE]
        return ""

    def _cliquer(self, element) -> None:
        """Clique, en insistant si l'élément se dérobe.

        Trois tentatives de plus en plus directes : le clic ordinaire, qui
        respecte la visibilité et les recouvrements ; le clic forcé, qui passe
        outre ; puis le déclenchement depuis la page elle-même, qui aboutit même
        sur un lien resté caché dans un menu que rien n'a su déplier.
        """
        try:
            element.click()
            return
        except Exception as exc:
            logger.info("     ↳ clic ordinaire refusé (%s) — on insiste", type(exc).__name__)

        try:
            element.click(force=True)
            return
        except Exception:
            logger.info("     ↳ clic forcé refusé — déclenchement depuis la page")

        element.evaluate("cible => cible.click()")

    def _resolve(self, session: BrowserSession, step: Step):
        """Essaie les sélecteurs candidats dans l'ordre et renvoie le premier qui répond."""
        scope = self._frame(session, step)
        errors: list[str] = []

        for candidate in step.selectors:
            try:
                element = scope.wait_for_selector(candidate, timeout=CANDIDATE_TIMEOUT_MS)
            except Exception as exc:
                errors.append(f"{candidate} ({type(exc).__name__})")
                continue
            if element is not None:
                return element

        # Deuxième chance : l'élément peut être présent mais caché dans un menu
        # déroulant. L'utilisateur l'avait ouvert d'un survol de souris — un
        # geste que l'enregistreur ne capte pas, puisqu'il n'est pas un clic.
        logger.info("     ↳ élément introuvable en l'état — tentative d'ouverture des menus")
        for candidate in step.selectors:
            element = self._ouvrir_menu_contenant(scope, candidate)
            if element is not None:
                logger.info("     ↳ menu déroulant ouvert pour atteindre l'élément")
                return element

        raise StepFailure(self._expliquer_l_echec(session, scope, step, errors))

    def _expliquer_l_echec(
        self, session: BrowserSession, scope, step: Step, errors: list[str]
    ) -> str:
        """Dit ce qui a réellement été constaté, et non seulement qu'on a échoué.

        La distinction décisive est celle-ci : un élément absent du document
        signale que la page affichée n'est pas celle attendue — une connexion
        qui n'a pas abouti, le plus souvent ; un élément présent mais invisible
        signale un menu que l'on n'a pas su déplier. Les deux demandent des
        corrections opposées, d'où l'intérêt de les nommer.
        """
        constats: list[str] = []
        for candidate in step.selectors:
            constats.append(f"« {candidate} » : {self._etat_de_l_element(scope, candidate)}")

        try:
            page_courante = f"page affichée : {session.page.url}"
        except Exception:
            page_courante = "page affichée : inconnue"

        try:
            titre = session.page.title()
            if titre:
                page_courante += f" — « {titre} »"
        except Exception:
            pass

        return (
            "l'élément attendu est introuvable, menus déroulants compris.\n"
            + "\n".join(f"  {constat}" for constat in constats)
            + f"\n  {page_courante}"
            + (f"\n  délais : {', '.join(errors)}" if errors else "")
        )

    def _etat_de_l_element(self, scope, selecteur: str) -> str:
        """Absent du document, ou présent mais invisible ?"""
        try:
            element = scope.wait_for_selector(selecteur, state="attached", timeout=1_000)
        except Exception:
            return "absent du document — la page affichée n'est sans doute pas la bonne"
        if element is None:
            return "absent du document"
        try:
            if element.is_visible():
                return "présent et visible, mais le clic n'a pas abouti"
        except Exception:
            return "présent, état indéterminable"
        return "présent mais invisible — menu non déplié"

    def _ouvrir_menu_contenant(self, scope, selecteur: str):
        """Déplie le menu qui masque l'élément visé, puis le renvoie.

        Les portails ouvrent leurs menus de trois façons : la pseudo-classe CSS
        `:hover`, qui exige un vrai déplacement de souris ; un script à l'écoute
        des événements de survol ; ou un clic sur l'entrée parente. Les trois
        sont tentées sur chaque ancêtre, du plus extérieur au plus proche.

        Les ancêtres sont marqués depuis la page elle-même plutôt que désignés
        par un axe XPath : la façon dont Playwright enchaîne les sélecteurs
        relatifs varie, et un enchaînement qui échoue en silence donne
        l'illusion qu'aucun parent n'existe.
        """
        try:
            element = scope.wait_for_selector(
                selecteur, state="attached", timeout=REVEAL_TIMEOUT_MS
            )
        except Exception:
            return None
        if element is None:
            return None

        niveaux = self._marquer_les_ancetres(element)
        if not niveaux:
            logger.info("     ↳ aucun parent à déplier")
            return None

        try:
            # Du plus extérieur au plus proche : ouvrir un sous-menu suppose
            # d'avoir ouvert celui qui le contient.
            for niveau in range(niveaux - 1, -1, -1):
                ancetre = scope.locator(f'[{MARQUEUR_MENU}="{niveau}"]').first
                for geste in ("survol", "clic"):
                    if self._tenter(ancetre, geste, niveau) and self._est_visible(element):
                        logger.info("     ↳ menu ouvert par %s du parent %d", geste, niveau)
                        return element

            self._emettre_survols(element)
            if self._est_visible(element):
                logger.info("     ↳ menu ouvert par les événements de survol")
                return element
        finally:
            self._effacer_les_marques(scope)

        logger.info("     ↳ aucun geste n'a déplié le menu (%d parent(s) essayé(s))", niveaux)
        return None

    def _marquer_les_ancetres(self, element) -> int:
        """Marque les parents de l'élément et renvoie leur nombre."""
        try:
            return int(
                element.evaluate(
                    """(cible, marqueur) => {
                        let noeud = cible.parentElement;
                        let niveau = 0;
                        while (noeud && noeud !== document.body && niveau < 6) {
                            noeud.setAttribute(marqueur, String(niveau));
                            noeud = noeud.parentElement;
                            niveau += 1;
                        }
                        return niveau;
                    }""",
                    MARQUEUR_MENU,
                )
            )
        except Exception:
            return 0

    def _effacer_les_marques(self, scope) -> None:
        try:
            scope.evaluate(
                """marqueur => {
                    for (const noeud of document.querySelectorAll('[' + marqueur + ']')) {
                        noeud.removeAttribute(marqueur);
                    }
                }""",
                MARQUEUR_MENU,
            )
        except Exception:
            logger.debug("Marques de menu non effacées")

    def _tenter(self, ancetre, geste: str, niveau: int) -> bool:
        """Survole ou clique un parent ; False si le geste est impossible."""
        try:
            if not ancetre.is_visible():
                return False
            if geste == "survol":
                ancetre.hover(timeout=REVEAL_TIMEOUT_MS)
            else:
                ancetre.click(timeout=REVEAL_TIMEOUT_MS, no_wait_after=True)
            return True
        except Exception:
            logger.debug("Parent %d : %s impossible", niveau, geste)
            return False

    def _est_visible(self, element) -> bool:
        try:
            return bool(element.is_visible())
        except Exception:
            return False

    def _emettre_survols(self, element) -> None:
        """Dernier recours : les menus pilotés par script écoutent ces événements."""
        try:
            element.evaluate(
                """cible => {
                    const types = ['pointerover', 'mouseover', 'mouseenter'];
                    let noeud = cible;
                    let remontees = 0;
                    while (noeud && noeud !== document.body && remontees < 6) {
                        for (const type of types) {
                            noeud.dispatchEvent(new MouseEvent(type, {bubbles: true}));
                        }
                        noeud = noeud.parentElement;
                        remontees += 1;
                    }
                }"""
            )
        except Exception:
            logger.debug("Événements de survol non émis")

    def _frame(self, session: BrowserSession, step: Step):
        """Retrouve l'iframe dans laquelle l'action avait été enregistrée."""
        if not step.frame_url:
            return session.page
        for frame in session.page.frames:
            if frame.url == step.frame_url:
                return frame
        # L'URL d'une iframe porte souvent un jeton de session : on retombe sur le chemin.
        path = step.frame_url.split("?")[0]
        for frame in session.page.frames:
            if frame.url.startswith(path):
                return frame
        logger.warning("Iframe %s introuvable — tentative sur la page principale", step.frame_url)
        return session.page

    def _resolve_value(
        self,
        step: Step,
        credentials: Credentials,
        start: date,
        end: date,
        date_format: str,
        otp_requested_at: datetime,
    ) -> str | None:
        """Remplace un jeton par sa valeur réelle, au tout dernier moment."""
        value = step.value
        if value == TOKEN_USERNAME:
            return credentials.username
        if value == TOKEN_PASSWORD:
            return credentials.password
        if value == TOKEN_OTP:
            logger.info("     ↳ code d'authentification demandé (source : %s)", self.otp.name)
            return self.otp.wait_for_code(since=otp_requested_at, hint=f"Code {self.display_name}")
        if value == TOKEN_START:
            return start.strftime(date_format)
        if value == TOKEN_END:
            return end.strftime(date_format)
        return value
