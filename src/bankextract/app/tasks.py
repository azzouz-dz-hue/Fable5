"""Exécution des opérations longues en arrière-plan, avec journal consultable.

Une extraction dure des dizaines de secondes et ouvre un navigateur : la faire
dans le fil de la requête HTTP figerait l'interface. Elle tourne donc dans un
fil séparé, et l'interface interroge son avancement.

Playwright, en mode synchrone, refuse de démarrer dans un fil où tourne déjà
une boucle asyncio — c'est le cas du fil principal d'uvicorn. Un `Thread`
ordinaire n'en a pas : c'est précisément ce qui rend ce découpage possible.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

#: Au-delà, le journal d'une opération n'apporte plus rien et alourdit la page.
MAX_LIGNES = 400


class Etat(str, Enum):
    EN_COURS = "en_cours"
    TERMINE = "termine"
    ECHEC = "echec"


@dataclass
class Operation:
    """Une tâche lancée depuis l'interface."""

    identifiant: str
    nom: str
    etat: Etat = Etat.EN_COURS
    debut: datetime = field(default_factory=datetime.now)
    fin: datetime | None = None
    lignes: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LIGNES))
    resume: str = ""
    erreur: str = ""

    @property
    def duree_secondes(self) -> float:
        return ((self.fin or datetime.now()) - self.debut).total_seconds()

    def en_dict(self) -> dict:
        return {
            "identifiant": self.identifiant,
            "nom": self.nom,
            "etat": self.etat.value,
            "debut": self.debut.strftime("%H:%M:%S"),
            "duree": round(self.duree_secondes),
            "lignes": list(self.lignes),
            "resume": self.resume,
            "erreur": self.erreur,
        }


class _JournalVersOperation(logging.Handler):
    """Redirige les messages de journalisation vers l'opération en cours."""

    def __init__(self, operation: Operation):
        super().__init__(level=logging.INFO)
        self.operation = operation

    def emit(self, record: logging.LogRecord) -> None:
        # Une erreur de journalisation ne doit jamais interrompre l'opération.
        with contextlib.suppress(Exception):
            self.operation.lignes.append(self.format(record))


class Executeur:
    """Ne laisse tourner qu'une opération à la fois.

    Deux extractions simultanées se disputeraient le profil de navigateur et la
    base : mieux vaut refuser clairement que produire un résultat douteux.
    """

    def __init__(self) -> None:
        self._verrou = threading.Lock()
        self._courante: Operation | None = None
        self._derniere: Operation | None = None

    @property
    def occupe(self) -> bool:
        with self._verrou:
            return self._courante is not None

    def operation_visible(self) -> Operation | None:
        """L'opération en cours, sinon la dernière terminée."""
        with self._verrou:
            return self._courante or self._derniere

    def lancer(self, nom: str, travail: Callable[[Operation], str]) -> Operation:
        """Démarre `travail` en arrière-plan. Lève si une opération tourne déjà."""
        with self._verrou:
            if self._courante is not None:
                raise RuntimeError(
                    f"« {self._courante.nom} » est déjà en cours. "
                    "Attendez qu'elle se termine."
                )
            operation = Operation(identifiant=uuid.uuid4().hex[:8], nom=nom)
            self._courante = operation

        threading.Thread(
            target=self._executer, args=(operation, travail), daemon=True, name=f"op-{nom}"
        ).start()
        return operation

    def _executer(self, operation: Operation, travail: Callable[[Operation], str]) -> None:
        relais = _JournalVersOperation(operation)
        relais.setFormatter(logging.Formatter("%(message)s"))
        racine = logging.getLogger("bankextract")
        niveau_initial = racine.level
        racine.addHandler(relais)
        racine.setLevel(logging.INFO)

        try:
            operation.resume = travail(operation) or "Terminé."
            operation.etat = Etat.TERMINE
        except Exception as exc:
            operation.etat = Etat.ECHEC
            operation.erreur = f"{type(exc).__name__} : {exc}"
            operation.lignes.append(f"ERREUR : {operation.erreur}")
        finally:
            operation.fin = datetime.now()
            racine.removeHandler(relais)
            racine.setLevel(niveau_initial)
            with self._verrou:
                self._derniere = operation
                self._courante = None
