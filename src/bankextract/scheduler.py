"""Récurrences programmées par l'utilisateur.

Le calcul des échéances est volontairement composé de fonctions pures : c'est la
partie où une erreur passe inaperçue le plus longtemps, donc celle qui doit être
la plus facile à vérifier.

Deux usages possibles :
  - `bankextract scheduler --once`, appelé par cron toutes les heures ;
  - `bankextract scheduler`, qui boucle lui-même sur les postes sans cron.
"""

from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .mailer import MailConfig, Mailer, MailReport, SmtpConfig
from .pipeline import BankOutcome, run_banks

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULES_PATH = Path("config/schedules.yaml")
DEFAULT_STATE_PATH = Path("data/scheduler-state.json")


class Frequency(str, Enum):
    DAILY = "quotidien"
    WEEKLY = "hebdomadaire"
    MONTHLY = "mensuel"
    INTERVAL = "intervalle"


class Period(str, Enum):
    """Fenêtre d'extraction demandée à chaque exécution."""

    SINCE_LAST_RUN = "depuis_derniere_execution"
    LAST_DAYS = "derniers_jours"
    CURRENT_MONTH = "mois_en_cours"
    PREVIOUS_MONTH = "mois_precedent"


class Recurrence(BaseModel):
    """Quand une tâche doit se déclencher."""

    frequency: Frequency = Frequency.DAILY
    at: str = Field(default="06:00", description="Heure de déclenchement, format HH:MM.")
    day_of_week: int = Field(
        default=0, ge=0, le=6, description="0 = lundi … 6 = dimanche (hebdomadaire)."
    )
    day_of_month: int = Field(
        default=1, ge=1, le=31, description="Jour du mois (mensuel) ; ramené au dernier jour."
    )
    interval_days: int = Field(default=7, ge=1, description="Écart en jours (intervalle).")

    @field_validator("at")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        parse_time(value)  # lève si le format est mauvais
        return value

    @property
    def time_of_day(self) -> time:
        return parse_time(self.at)

    def describe(self) -> str:
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        if self.frequency is Frequency.DAILY:
            return f"chaque jour à {self.at}"
        if self.frequency is Frequency.WEEKLY:
            return f"chaque {jours[self.day_of_week]} à {self.at}"
        if self.frequency is Frequency.MONTHLY:
            return f"le {self.day_of_month} de chaque mois à {self.at}"
        return f"tous les {self.interval_days} jour(s) à {self.at}"


class Schedule(BaseModel):
    """Une tâche programmée : quelles banques, quand, et vers quelle adresse."""

    name: str
    banks: list[str] = Field(default_factory=list, description="Vide = toutes les banques actives.")
    recurrence: Recurrence = Field(default_factory=Recurrence)
    period: Period = Period.SINCE_LAST_RUN
    days: int = Field(default=30, ge=1, description="Profondeur pour « derniers_jours ».")
    mail: MailConfig = Field(default_factory=MailConfig)
    enabled: bool = True

    def window(self, last_run: datetime | None, now: datetime) -> tuple[date, date]:
        """Période à extraire, d'après le mode choisi."""
        today = now.date()
        if self.period is Period.LAST_DAYS:
            return today - timedelta(days=self.days), today
        if self.period is Period.CURRENT_MONTH:
            return today.replace(day=1), today
        if self.period is Period.PREVIOUS_MONTH:
            first_of_month = today.replace(day=1)
            last_day_previous = first_of_month - timedelta(days=1)
            return last_day_previous.replace(day=1), last_day_previous
        if last_run is None:
            # Première exécution : on remonte assez loin pour ne rien manquer.
            return today - timedelta(days=self.days), today
        # Un jour de recouvrement : la déduplication absorbe les doublons, alors
        # qu'une écriture manquée ne serait jamais rattrapée.
        return last_run.date() - timedelta(days=1), today


class SchedulerConfig(BaseModel):
    """Contenu de config/schedules.yaml."""

    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    schedules: list[Schedule] = Field(default_factory=list)

    def enabled_schedules(self) -> list[Schedule]:
        return [schedule for schedule in self.schedules if schedule.enabled]


# ---------------------------------------------------------------------- échéances


def parse_time(value: str) -> time:
    """Lit « 06:00 » ou « 6h30 »."""
    text = str(value).strip().lower().replace("h", ":")
    if text.endswith(":"):
        text += "00"
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        return time(hour=hour, minute=minute)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Heure illisible : « {value} » (attendu HH:MM)") from exc


def _at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment)


def _clamp_day(year: int, month: int, day: int) -> date:
    """Le 31 d'un mois de 30 jours devient le 30 : la tâche ne saute pas un mois."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def next_run(recurrence: Recurrence, reference: datetime) -> datetime:
    """Première échéance strictement postérieure à `reference`."""
    moment = recurrence.time_of_day
    today = reference.date()

    if recurrence.frequency is Frequency.DAILY:
        candidate = _at(today, moment)
        return candidate if candidate > reference else _at(today + timedelta(days=1), moment)

    if recurrence.frequency is Frequency.WEEKLY:
        delta = (recurrence.day_of_week - today.weekday()) % 7
        candidate = _at(today + timedelta(days=delta), moment)
        return candidate if candidate > reference else candidate + timedelta(days=7)

    if recurrence.frequency is Frequency.MONTHLY:
        candidate = _at(_clamp_day(today.year, today.month, recurrence.day_of_month), moment)
        if candidate > reference:
            return candidate
        year = today.year + (today.month // 12)
        month = today.month % 12 + 1
        return _at(_clamp_day(year, month, recurrence.day_of_month), moment)

    candidate = _at(today, moment)
    return (
        candidate
        if candidate > reference
        else _at(today + timedelta(days=recurrence.interval_days), moment)
    )


def is_due(schedule: Schedule, last_run: datetime | None, now: datetime) -> bool:
    """Vrai si la tâche aurait dû se déclencher et ne l'a pas encore été.

    Une exécution manquée (poste éteint, panne de réseau) est rattrapée au
    prochain réveil plutôt que perdue.
    """
    if not schedule.enabled:
        return False
    if last_run is None:
        # Jamais exécutée : due dès que l'heure du jour est passée.
        return now >= _at(now.date(), schedule.recurrence.time_of_day)
    return now >= next_run(schedule.recurrence, last_run)


# ---------------------------------------------------------------------- état


class SchedulerState:
    """Mémorise la date de dernière exécution de chaque tâche."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("État du planificateur illisible — reparti de zéro : %s", self.path)
                self._data = {}

    def last_run(self, name: str) -> datetime | None:
        raw = self._data.get(name)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def mark_run(self, name: str, moment: datetime | None = None) -> None:
        self._data[name] = (moment or datetime.now()).isoformat(timespec="seconds")
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


# ---------------------------------------------------------------------- exécution


def load_scheduler_config(path: Path | str = DEFAULT_SCHEDULES_PATH) -> SchedulerConfig:
    config_path = Path(path)
    if not config_path.exists():
        return SchedulerConfig()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return SchedulerConfig.model_validate(raw)


def run_schedule(
    schedule: Schedule,
    settings: Settings,
    smtp: SmtpConfig,
    last_run: datetime | None,
    now: datetime | None = None,
) -> tuple[list[BankOutcome], list[MailReport]]:
    """Exécute une tâche : extraction sur sa fenêtre, puis envoi des courriels."""
    now = now or datetime.now()
    start, end = schedule.window(last_run, now)
    logger.info(
        "[%s] extraction du %s au %s (%s)",
        schedule.name,
        start.strftime("%d/%m/%Y"),
        end.strftime("%d/%m/%Y"),
        schedule.recurrence.describe(),
    )

    outcomes = run_banks(settings, schedule.banks or None, start=start, end=end)

    mailer = Mailer(smtp)
    reports: list[MailReport] = []
    for outcome in outcomes:
        report = mailer.send_result(outcome.result, schedule.mail, outcome.exports)
        if report.error:
            logger.error("[%s] courriel non envoyé : %s", schedule.name, report.error)
        reports.append(report)
    return outcomes, reports


def run_due_schedules(
    settings: Settings,
    config: SchedulerConfig,
    state: SchedulerState,
    now: datetime | None = None,
) -> list[str]:
    """Exécute toutes les tâches échues et renvoie leurs noms.

    Une tâche en échec n'empêche pas les suivantes : le planificateur est fait
    pour tourner sans surveillance.
    """
    now = now or datetime.now()
    executed: list[str] = []

    for schedule in config.enabled_schedules():
        last_run = state.last_run(schedule.name)
        if not is_due(schedule, last_run, now):
            continue
        try:
            run_schedule(schedule, settings, config.smtp, last_run, now)
        except Exception:
            logger.exception("[%s] tâche interrompue", schedule.name)
        finally:
            # Marquée exécutée même en cas d'échec : sinon elle se relancerait
            # en boucle à chaque réveil sans jamais aboutir.
            state.mark_run(schedule.name, now)
        executed.append(schedule.name)

    return executed
