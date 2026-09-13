"""Enregistrement et rejeu des parcours bancaires.

L'utilisateur fait une fois la manipulation dans son portail ; le logiciel la
rejoue ensuite seul, aussi souvent que voulu.
"""

from .record import annotate_scenario, record_scenario
from .replay import ScenarioConnector, StepFailure
from .scenario import ActionType, Scenario, Step, scenario_path

__all__ = [
    "Scenario",
    "Step",
    "ActionType",
    "scenario_path",
    "record_scenario",
    "annotate_scenario",
    "ScenarioConnector",
    "StepFailure",
]
