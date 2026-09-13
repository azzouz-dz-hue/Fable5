"""Format d'un parcours enregistré puis rejoué.

Un scénario est la trace de ce que l'utilisateur a fait une fois dans le portail
de sa banque. Il est volontairement lisible et modifiable à la main : c'est du
JSON commenté par ses propres noms de champs, pas un format opaque.

Deux principes gouvernent ce format :

1. **Aucun secret n'y figure.** Les mots de passe et les codes OTP sont
   remplacés à l'enregistrement par des jetons (`{{password}}`, `{{otp}}`)
   résolus au moment du rejeu depuis le trousseau.
2. **Plusieurs sélecteurs par étape.** Un portail bancaire change souvent un
   identifiant ou une classe ; le rejeu essaie les candidats dans l'ordre et
   ne tombe en échec que si tous échouent.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

#: Jetons remplacés au moment du rejeu.
TOKEN_USERNAME = "{{username}}"
TOKEN_PASSWORD = "{{password}}"
TOKEN_OTP = "{{otp}}"
TOKEN_START = "{{start}}"
TOKEN_END = "{{end}}"

ALL_TOKENS = (TOKEN_USERNAME, TOKEN_PASSWORD, TOKEN_OTP, TOKEN_START, TOKEN_END)


class ActionType(str, Enum):
    """Les gestes qu'un utilisateur pose dans un portail bancaire."""

    GOTO = "goto"           # navigation directe vers une URL
    CLICK = "click"
    FILL = "fill"           # saisie dans un champ
    SELECT = "select"       # choix dans une liste déroulante
    PRESS = "press"         # touche clavier (Entrée, Tabulation…)
    CHECK = "check"         # case à cocher
    WAIT = "wait"           # attente explicite ajoutée par l'utilisateur
    DOWNLOAD = "download"   # clic dont on attend un téléchargement


class Step(BaseModel):
    """Une action du parcours."""

    model_config = ConfigDict(str_strip_whitespace=True)

    action: ActionType
    selectors: list[str] = Field(
        default_factory=list,
        description="Candidats essayés dans l'ordre ; le premier qui répond gagne.",
    )
    value: str | None = Field(
        default=None, description="Texte saisi, option choisie ou touche pressée."
    )
    url: str | None = Field(default=None, description="URL pour une navigation.")
    frame_url: str | None = Field(
        default=None, description="URL de l'iframe portant l'élément, si ce n'est pas la page."
    )
    label: str = Field(default="", description="Description lisible, affichée au rejeu.")
    optional: bool = Field(
        default=False,
        description="Étape tolérée absente (bandeau cookies, écran d'OTP non demandé).",
    )
    wait_ms: int = Field(default=0, description="Pause après l'étape.")
    recorded_at: datetime | None = None

    @property
    def is_secret(self) -> bool:
        return self.value in (TOKEN_PASSWORD, TOKEN_OTP)

    def describe(self) -> str:
        """Résumé d'une ligne, sans jamais révéler de valeur sensible."""
        if self.label:
            return self.label
        target = self.selectors[0] if self.selectors else (self.url or "")
        if self.action is ActionType.GOTO:
            return f"aller sur {self.url}"
        if self.action is ActionType.FILL:
            shown = "•••" if self.is_secret else f"« {self.value} »"
            return f"saisir {shown} dans {target}"
        if self.action is ActionType.DOWNLOAD:
            return f"télécharger via {target}"
        return f"{self.action.value} {target}"


class Scenario(BaseModel):
    """Parcours complet d'une banque, de la connexion au téléchargement."""

    model_config = ConfigDict(str_strip_whitespace=True)

    bank: str
    label: str = ""
    base_url: str = ""
    steps: list[Step] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime | None = None
    version: int = 1
    notes: str = Field(
        default="",
        description="Remarques libres : particularités du portail, points à surveiller.",
    )

    @property
    def display_name(self) -> str:
        return self.label or self.bank.upper()

    @property
    def downloads(self) -> list[Step]:
        return [step for step in self.steps if step.action is ActionType.DOWNLOAD]

    @property
    def uses_otp(self) -> bool:
        return any(step.value == TOKEN_OTP for step in self.steps)

    @property
    def uses_period(self) -> bool:
        return any(step.value in (TOKEN_START, TOKEN_END) for step in self.steps)

    def contains_secret_values(self) -> list[str]:
        """Repère un secret qui aurait échappé au masquage — filet de sécurité.

        Renvoie la description des étapes suspectes ; la liste doit rester vide.
        """
        suspects: list[str] = []
        for index, step in enumerate(self.steps, start=1):
            if step.action is not ActionType.FILL or not step.value:
                continue
            if step.value in ALL_TOKENS:
                continue
            selector_text = " ".join(step.selectors).lower()
            if any(word in selector_text for word in ("password", "passwd", "mot-de-passe", "mdp")):
                suspects.append(f"étape {index} : {step.selectors[0] if step.selectors else '?'}")
        return suspects

    # ------------------------------------------------------------------ E/S

    def save(self, path: Path) -> Path:
        """Écrit le scénario en JSON indenté, lisible et modifiable."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now()
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> Scenario:
        if not path.exists():
            raise FileNotFoundError(
                f"Scénario introuvable : {path}. Enregistrez-le avec « bankextract record »."
            )
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def summary(self) -> str:
        pieces = [f"{len(self.steps)} étape(s)", f"{len(self.downloads)} téléchargement(s)"]
        if self.uses_otp:
            pieces.append("OTP")
        if self.uses_period:
            pieces.append("période variable")
        return " · ".join(pieces)


def scenario_path(directory: Path, bank: str) -> Path:
    """Emplacement canonique du scénario d'une banque."""
    return Path(directory) / f"{bank}.json"
