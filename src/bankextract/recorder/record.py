"""Enregistrement d'un parcours bancaire dans un navigateur piloté.

L'utilisateur ouvre son portail, se connecte et télécharge un relevé comme il le
ferait à la main. Chaque geste est capté, transformé en étape rejouable, et le
tout est écrit dans un fichier JSON relisible.

Ce qui n'est jamais enregistré : le mot de passe et le code OTP. La page ne
transmet qu'un marqueur ; la valeur réelle est réclamée au trousseau au moment
du rejeu.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import BrowserConfig
from ..normalize import parse_date
from .scenario import (
    TOKEN_END,
    TOKEN_OTP,
    TOKEN_PASSWORD,
    TOKEN_START,
    TOKEN_USERNAME,
    ActionType,
    Scenario,
    Step,
    key_character,
    key_template,
    keypad_runs,
)

logger = logging.getLogger(__name__)

INJECT_SCRIPT = Path(__file__).parent / "inject.js"

#: Indices qu'un champ réclame un code d'authentification forte.
OTP_HINTS = ("otp", "sms", "code", "token", "authent", "verif", "challenge")

#: Pas de la boucle d'attente : assez court pour que « J'ai terminé » réponde
#: sans délai perceptible, assez long pour ne rien coûter.
_PAS_ATTENTE_MS = 400

#: Deux actions identiques rapprochées viennent d'un double événement, pas de
#: deux gestes : les navigateurs émettent parfois « change » puis « click ».
DEDUPLICATION_WINDOW_MS = 400


class RecordingSession:
    """Collecte les actions transmises par la page et les ordonne."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self._last_signature: tuple | None = None
        self._last_time: float = 0.0

    def add_event(self, payload: str) -> None:
        """Reçoit un événement JSON émis par le capteur injecté."""
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("Événement illisible ignoré")
            return

        action = _to_action(event.get("action"))
        if action is None:
            return

        selectors = [s for s in (event.get("selectors") or []) if s]
        value = event.get("value")
        if event.get("secret"):
            value = TOKEN_PASSWORD

        signature = (action, tuple(selectors[:1]), value)
        now = time.monotonic() * 1000
        if signature == self._last_signature and now - self._last_time < DEDUPLICATION_WINDOW_MS:
            return
        self._last_signature, self._last_time = signature, now

        # Une saisie répétée dans le même champ remplace la précédente : l'utilisateur
        # a corrigé sa frappe, seule la valeur finale compte.
        if action is ActionType.FILL and self.steps:
            previous = self.steps[-1]
            if previous.action is ActionType.FILL and previous.selectors[:1] == selectors[:1]:
                previous.value = value
                return

        self.steps.append(
            Step(
                action=action,
                selectors=selectors,
                value=value,
                label=event.get("label") or "",
                frame_url=event.get("frame_url"),
                recorded_at=datetime.now(),
            )
        )
        logger.info("  %2d. %s", len(self.steps), self.steps[-1].describe())

    def mark_last_click_as_download(self) -> None:
        """Un téléchargement vient de partir : le dernier clic en était la cause."""
        for step in reversed(self.steps):
            if step.action is ActionType.CLICK:
                step.action = ActionType.DOWNLOAD
                step.label = f"télécharger ({step.label})" if step.label else "télécharger"
                logger.info("     ↳ ce clic déclenche un téléchargement")
                return
        logger.debug("Téléchargement sans clic associé")


def _to_action(raw: str | None) -> ActionType | None:
    try:
        return ActionType(raw)
    except (ValueError, TypeError):
        return None


def record_scenario(
    bank: str,
    start_url: str,
    browser_config: BrowserConfig,
    *,
    label: str = "",
    max_seconds: int = 1800,
    auto_detect: bool = True,
    headless: bool = False,
    driver: Callable[[object], None] | None = None,
    arret: threading.Event | None = None,
) -> Scenario:
    """Ouvre le portail et enregistre le parcours jusqu'à fermeture du navigateur.

    L'enregistrement s'arrête quand l'utilisateur ferme la fenêtre, ou au bout de
    `max_seconds`.

    `arret` permet de clore l'enregistrement depuis l'interface : l'utilisateur
    ne doit pas avoir à deviner que fermer la fenêtre met fin à la capture.

    `driver` permet d'automatiser le parcours au lieu d'attendre un humain : les
    actions qu'il déclenche produisent de vrais événements DOM, donc le capteur
    les enregistre exactement comme ceux d'un utilisateur. C'est ce qui rend
    l'enregistreur vérifiable par les tests.
    """
    from playwright.sync_api import sync_playwright

    session = RecordingSession()
    profile_dir = browser_config.profiles_dir / f"{bank}-record"
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_args: dict = {
        "headless": headless,  # en usage réel l'utilisateur doit voir ce qu'il fait
        "slow_mo": browser_config.slow_mo_ms,
        "locale": browser_config.locale,
        "timezone_id": browser_config.timezone,
        "accept_downloads": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if browser_config.executable_path:
        launch_args["executable_path"] = browser_config.executable_path

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), **launch_args
        )
        _instrument(context, session)

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        if driver is not None:
            driver(page)
        else:
            _attendre_la_fin(context, arret, max_seconds)

        try:
            context.close()
        except Exception:  # pragma: no cover - déjà fermé par l'utilisateur
            logger.debug("Navigateur déjà fermé")

    scenario = Scenario(
        bank=bank,
        label=label,
        base_url=start_url,
        steps=[Step(action=ActionType.GOTO, url=start_url, label=f"ouvrir {start_url}")]
        + session.steps,
    )
    if auto_detect:
        annotate_scenario(scenario)
    return scenario


def _attendre_la_fin(context, arret, max_seconds: int) -> None:
    """Attend que l'utilisateur signale la fin de son parcours.

    Trois issues : il clique sur « J'ai terminé », il ferme la fenêtre, ou le
    délai expire. La fermeture se détecte de trois manières, car aucune n'est
    fiable seule : l'événement `close` du contexte n'est pas toujours émis, la
    liste des pages peut rester garnie d'une page fantôme, et interroger un
    contexte déjà fermé lève une exception au lieu de renvoyer une liste vide.
    """
    deadline = time.monotonic() + max_seconds

    while time.monotonic() < deadline:
        if arret is not None and arret.is_set():
            logger.info("Arrêt demandé depuis l'interface")
            return

        try:
            ouvertes = [page for page in context.pages if not page.is_closed()]
            if not ouvertes:
                logger.info("Plus aucune fenêtre ouverte — fin de l'enregistrement")
                return
            # Cette attente-ci passe par Playwright, contrairement à un
            # « sleep » : c'est elle qui lui laisse traiter ses événements, donc
            # constater la fermeture. Sans cela, la liste des pages resterait
            # éternellement garnie et l'enregistrement ne s'arrêterait jamais.
            ouvertes[0].wait_for_timeout(_PAS_ATTENTE_MS)
        except Exception:
            logger.info("Navigateur fermé — fin de l'enregistrement")
            return

    logger.warning("Délai d'enregistrement dépassé (%d minutes)", max_seconds // 60)


def _instrument(context, session: RecordingSession) -> None:
    """Branche le capteur de page et l'écoute des téléchargements."""
    context.expose_binding(
        "__bankextractRecord", lambda _source, payload: session.add_event(payload)
    )
    context.add_init_script(path=str(INJECT_SCRIPT))

    def attach(page) -> None:
        page.on("download", lambda _download: session.mark_last_click_as_download())

    for existing in context.pages:
        attach(existing)
    context.on("page", attach)


# ---------------------------------------------------------------------- annotation


def annotate_scenario(scenario: Scenario) -> list[str]:
    """Remplace les valeurs variables par des jetons et décrit ce qui a été déduit.

    Trois substitutions, toutes vérifiables par l'utilisateur dans le JSON :
    l'identifiant, le code OTP et les dates de période.
    """
    notes: list[str] = []
    notes += _tag_virtual_keyboard(scenario)
    notes += _tag_username(scenario)
    notes += _tag_otp(scenario)
    notes += _tag_period(scenario)
    return notes


def _tag_virtual_keyboard(scenario: Scenario) -> list[str]:
    """Masque un code saisi sur un clavier virtuel.

    Certains portails font saisir le mot de passe en cliquant des touches à
    l'écran. Les clics, eux, sont enregistrés : leur ordre reconstituerait le
    code. On remplace donc la suite par une étape unique portant le jeton, et
    le gabarit du sélecteur permet au rejeu de recliquer les bonnes touches.
    """
    notes: list[str] = []
    # De la fin vers le début : remplacer une suite décale les indices suivants.
    for first, last in reversed(keypad_runs(scenario.steps)):
        gabarit = key_template(scenario.steps[first])
        if gabarit is None:
            notes.append(
                f"étapes {first + 1} à {last + 1} : clavier virtuel détecté mais sélecteur "
                "non reconnu — vérifiez le fichier à la main avant usage"
            )
            continue

        touches = [key_character(step) for step in scenario.steps[first : last + 1]]
        scenario.steps[first : last + 1] = [
            Step(
                action=ActionType.KEYPAD,
                value=TOKEN_PASSWORD,
                key_template=gabarit,
                label=f"saisir ••• sur le clavier virtuel ({len(touches)} touches)",
                frame_url=scenario.steps[first].frame_url,
            )
        ]
        notes.append(
            f"étapes {first + 1} à {last + 1} reconnues comme clavier virtuel → "
            f"{TOKEN_PASSWORD} (les touches cliquées ne sont pas conservées)"
        )
    return notes


def _fill_steps(scenario: Scenario) -> list[tuple[int, Step]]:
    return [
        (index, step)
        for index, step in enumerate(scenario.steps)
        if step.action is ActionType.FILL
    ]


def _tag_username(scenario: Scenario) -> list[str]:
    """Le champ saisi juste avant le mot de passe est l'identifiant."""
    steps = scenario.steps
    for index, step in enumerate(steps):
        if step.value != TOKEN_PASSWORD:
            continue
        for previous in range(index - 1, -1, -1):
            candidate = steps[previous]
            if candidate.action is ActionType.FILL and candidate.value not in (
                TOKEN_PASSWORD,
                TOKEN_USERNAME,
            ):
                candidate.value = TOKEN_USERNAME
                return [f"étape {previous + 1} reconnue comme identifiant → {TOKEN_USERNAME}"]
        break
    return []


def _tag_otp(scenario: Scenario) -> list[str]:
    """Un champ au nom évocateur, saisi après le mot de passe, porte le code OTP."""
    password_index = next(
        (i for i, s in enumerate(scenario.steps) if s.value == TOKEN_PASSWORD), None
    )
    if password_index is None:
        return []

    for index, step in _fill_steps(scenario):
        if index <= password_index or step.value in (TOKEN_USERNAME, TOKEN_PASSWORD, TOKEN_OTP):
            continue
        haystack = " ".join(step.selectors + [step.label]).lower()
        if any(hint in haystack for hint in OTP_HINTS):
            step.value = TOKEN_OTP
            step.optional = True  # l'OTP n'est pas toujours redemandé
            return [
                f"étape {index + 1} reconnue comme code d'authentification → {TOKEN_OTP} "
                "(marquée facultative : certains portails ne le redemandent pas)"
            ]
    return []


def _tag_period(scenario: Scenario) -> list[str]:
    """Les dates saisies deviennent des jetons, sinon le rejeu figerait la période."""
    dated: list[tuple[int, Step, object]] = []
    for index, step in _fill_steps(scenario):
        if not step.value or step.value in (TOKEN_USERNAME, TOKEN_PASSWORD, TOKEN_OTP):
            continue
        parsed = parse_date(step.value)
        if parsed is not None:
            dated.append((index, step, parsed))

    if not dated:
        return []

    if len(dated) == 1:
        index, step, _ = dated[0]
        step.value = TOKEN_END
        return [f"étape {index + 1} reconnue comme date → {TOKEN_END}"]

    dated.sort(key=lambda item: item[2])
    notes = []
    first_index, first_step, _ = dated[0]
    last_index, last_step, _ = dated[-1]
    first_step.value = TOKEN_START
    last_step.value = TOKEN_END
    notes.append(f"étape {first_index + 1} reconnue comme début de période → {TOKEN_START}")
    notes.append(f"étape {last_index + 1} reconnue comme fin de période → {TOKEN_END}")
    for index, _step, _parsed in dated[1:-1]:
        notes.append(f"étape {index + 1} contient aussi une date — à vérifier à la main")
    return notes
