"""Registre des connecteurs bancaires."""

from __future__ import annotations

from ..config import BankConfig, Settings
from .algeria import ALGERIAN_CONNECTORS
from .base import BankConnector, LoginError, ScrapingError
from .demo import DemoConnector
from .generic import GenericPortalConnector

_REGISTRY: dict[str, type[BankConnector]] = {
    connector.name: connector for connector in ALGERIAN_CONNECTORS
}
_REGISTRY[GenericPortalConnector.name] = GenericPortalConnector
_REGISTRY[DemoConnector.name] = DemoConnector


def register(connector: type[BankConnector]) -> type[BankConnector]:
    """Ajoute un connecteur maison au registre (utilisable comme décorateur)."""
    _REGISTRY[connector.name] = connector
    return connector


def get_connector_class(name: str) -> type[BankConnector]:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Connecteur inconnu : « {name} ». Disponibles : {known}") from None


def build_connector(bank_name: str, config: BankConfig, settings: Settings) -> BankConnector:
    """Instancie le connecteur décrit par la configuration d'une banque.

    Le connecteur porte le nom de la section de configuration, ce qui permet
    d'avoir deux accès chez la même banque (`bna_sarl`, `bna_perso`).
    """
    connector_cls = get_connector_class(config.connector)
    instance = connector_cls(config=config, settings=settings)
    if bank_name != connector_cls.name:
        instance.name = bank_name
    return instance


def available_connectors() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "BankConnector",
    "DemoConnector",
    "GenericPortalConnector",
    "LoginError",
    "ScrapingError",
    "available_connectors",
    "build_connector",
    "get_connector_class",
    "register",
]
