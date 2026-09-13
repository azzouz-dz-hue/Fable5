"""Récurrences programmées : calcul des échéances et fenêtres d'extraction."""

from datetime import date, datetime, time
from pathlib import Path

import pytest

from bankextract.scheduler import (
    Frequency,
    Period,
    Recurrence,
    Schedule,
    SchedulerState,
    is_due,
    load_scheduler_config,
    next_run,
    parse_time,
)


@pytest.mark.parametrize(
    "raw, expected",
    [("06:00", time(6, 0)), ("6:30", time(6, 30)), ("6h30", time(6, 30)), ("18h", time(18, 0))],
)
def test_lecture_de_l_heure(raw, expected):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["midi", "", "25h99h99"])
def test_heure_illisible(raw):
    with pytest.raises(ValueError, match="illisible|Heure"):
        parse_time(raw)


def test_quotidien_avant_et_apres_l_heure():
    recurrence = Recurrence(frequency=Frequency.DAILY, at="06:00")

    assert next_run(recurrence, datetime(2026, 3, 4, 5, 0)) == datetime(2026, 3, 4, 6, 0)
    assert next_run(recurrence, datetime(2026, 3, 4, 7, 0)) == datetime(2026, 3, 5, 6, 0)


def test_hebdomadaire_vise_le_bon_jour():
    # 4 mars 2026 est un mercredi ; la tâche vise le lundi.
    recurrence = Recurrence(frequency=Frequency.WEEKLY, day_of_week=0, at="08:00")

    assert next_run(recurrence, datetime(2026, 3, 4, 9, 0)) == datetime(2026, 3, 9, 8, 0)


def test_hebdomadaire_le_jour_meme_avant_l_heure():
    recurrence = Recurrence(frequency=Frequency.WEEKLY, day_of_week=2, at="08:00")

    assert next_run(recurrence, datetime(2026, 3, 4, 7, 0)) == datetime(2026, 3, 4, 8, 0)


def test_mensuel_simple():
    recurrence = Recurrence(frequency=Frequency.MONTHLY, day_of_month=1, at="06:00")

    assert next_run(recurrence, datetime(2026, 3, 4, 9, 0)) == datetime(2026, 4, 1, 6, 0)


def test_mensuel_le_31_ne_saute_pas_fevrier():
    """Un 31 demandé dans un mois plus court est ramené au dernier jour."""
    recurrence = Recurrence(frequency=Frequency.MONTHLY, day_of_month=31, at="06:00")

    assert next_run(recurrence, datetime(2026, 1, 31, 7, 0)) == datetime(2026, 2, 28, 6, 0)


def test_mensuel_passe_a_l_annee_suivante():
    recurrence = Recurrence(frequency=Frequency.MONTHLY, day_of_month=1, at="06:00")

    assert next_run(recurrence, datetime(2026, 12, 5, 9, 0)) == datetime(2027, 1, 1, 6, 0)


def test_intervalle():
    recurrence = Recurrence(frequency=Frequency.INTERVAL, interval_days=10, at="06:00")

    assert next_run(recurrence, datetime(2026, 3, 4, 7, 0)) == datetime(2026, 3, 14, 6, 0)


def test_tache_jamais_executee_est_due_une_fois_l_heure_passee():
    schedule = Schedule(name="t", recurrence=Recurrence(at="06:00"))

    assert is_due(schedule, None, datetime(2026, 3, 4, 7, 0))
    assert not is_due(schedule, None, datetime(2026, 3, 4, 5, 0))


def test_tache_desactivee_jamais_due():
    schedule = Schedule(name="t", enabled=False, recurrence=Recurrence(at="06:00"))

    assert not is_due(schedule, None, datetime(2026, 3, 4, 23, 0))


def test_execution_manquee_est_rattrapee():
    """Poste éteint pendant trois jours : la tâche doit se déclencher au réveil."""
    schedule = Schedule(name="t", recurrence=Recurrence(at="06:00"))

    assert is_due(schedule, datetime(2026, 3, 1, 6, 0), datetime(2026, 3, 4, 9, 0))


def test_pas_de_double_execution_le_meme_jour():
    schedule = Schedule(name="t", recurrence=Recurrence(at="06:00"))

    assert not is_due(schedule, datetime(2026, 3, 4, 6, 0), datetime(2026, 3, 4, 18, 0))


def test_fenetre_derniers_jours():
    schedule = Schedule(name="t", period=Period.LAST_DAYS, days=7)

    debut, fin = schedule.window(None, datetime(2026, 3, 10, 6, 0))

    assert (debut, fin) == (date(2026, 3, 3), date(2026, 3, 10))


def test_fenetre_mois_precedent():
    schedule = Schedule(name="t", period=Period.PREVIOUS_MONTH)

    debut, fin = schedule.window(None, datetime(2026, 3, 10, 6, 0))

    assert (debut, fin) == (date(2026, 2, 1), date(2026, 2, 28))


def test_fenetre_mois_en_cours():
    schedule = Schedule(name="t", period=Period.CURRENT_MONTH)

    debut, fin = schedule.window(None, datetime(2026, 3, 10, 6, 0))

    assert (debut, fin) == (date(2026, 3, 1), date(2026, 3, 10))


def test_fenetre_depuis_derniere_execution_recouvre_un_jour():
    """Un jour de recouvrement : mieux vaut un doublon, que la base absorbe,
    qu'une écriture manquée que rien ne rattrapera."""
    schedule = Schedule(name="t", period=Period.SINCE_LAST_RUN)

    debut, fin = schedule.window(datetime(2026, 3, 8, 6, 0), datetime(2026, 3, 10, 6, 0))

    assert (debut, fin) == (date(2026, 3, 7), date(2026, 3, 10))


def test_premiere_execution_remonte_la_profondeur_configuree():
    schedule = Schedule(name="t", period=Period.SINCE_LAST_RUN, days=20)

    debut, _ = schedule.window(None, datetime(2026, 3, 10, 6, 0))

    assert debut == date(2026, 2, 18)


def test_etat_persiste_entre_deux_lancements(tmp_path):
    chemin = tmp_path / "state.json"
    SchedulerState(chemin).mark_run("releve-mensuel", datetime(2026, 3, 1, 6, 0))

    relu = SchedulerState(chemin)

    assert relu.last_run("releve-mensuel") == datetime(2026, 3, 1, 6, 0)
    assert relu.last_run("inconnue") is None


def test_etat_corrompu_ne_bloque_pas_le_planificateur(tmp_path):
    chemin = tmp_path / "state.json"
    chemin.write_text("{ ceci n'est pas du JSON")

    assert SchedulerState(chemin).last_run("t") is None


def test_configuration_livree_est_valide():
    config = load_scheduler_config("config/schedules.yaml")

    assert len(config.schedules) == 3
    assert config.smtp.password_env == "SMTP_PASSWORD"


def test_toutes_les_recurrences_livrees_sont_desactivees():
    """Rien ne doit se déclencher tant que l'utilisateur n'a pas vérifié."""
    assert load_scheduler_config("config/schedules.yaml").enabled_schedules() == []


def test_aucun_mot_de_passe_dans_la_configuration_livree():
    contenu = Path("config/schedules.yaml").read_text(encoding="utf-8").lower()

    assert "password:" not in contenu, "seul password_env doit figurer dans le fichier"
