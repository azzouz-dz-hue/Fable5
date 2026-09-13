"""Connecteur de démonstration, branché sur le faux portail des tests.

Il sert deux usages : vérifier la chaîne complète (connexion, OTP automatique,
pagination, export) sans toucher à une vraie banque, et servir de modèle
commenté pour écrire la configuration d'une banque réelle.
"""

from __future__ import annotations

from typing import Any

from .generic import GenericPortalConnector

#: Carte de sélecteurs du faux portail — le miroir exact de ce qu'il faut
#: renseigner dans `config/banks.yaml` pour une banque réelle.
DEMO_SPEC: dict[str, Any] = {
    "base_url": "http://127.0.0.1:8777",
    "login": {
        "url": "/",
        "username_selector": "#username",
        "password_selector": "#password",
        "submit_selector": "#login-submit",
        "error_selector": ".alert-danger",
        "success_selector": ".dashboard",
        "otp": {
            "input_selector": "#otp",
            "submit_selector": "#otp-submit",
            "prompt": "Code SMS Banque Demo",
            "wait_ms": 5000,
        },
    },
    "accounts": {
        "url": "/accounts",
        "row_selector": "table.accounts tbody tr",
        "default_currency": "DZD",
        "columns": {
            "number": "td.num",
            "label": "td.lbl",
            "type": "td.typ",
            "currency": "td.cur",
            "balance": "td.bal",
        },
    },
    "transactions": {
        "url_template": "/accounts/{number}/operations",
        "row_selector": "table.operations tbody tr",
        "next_page_selector": "a.next",
        "max_pages": 10,
        "columns": {
            "date": "td.d",
            "value_date": "td.dv",
            "label": "td.lb",
            "debit": "td.db",
            "credit": "td.cr",
            "balance": "td.sd",
            "reference": "td.rf",
        },
    },
    "statements": {
        "url_template": "/accounts/{number}/statements",
        "row_selector": "table.statements tbody tr",
        "link_selector": "a.dl",
        "max_files": 3,
        "columns": {"period": "td.per"},
    },
    "logout": {"selector": "#logout"},
}


class DemoConnector(GenericPortalConnector):
    """Banque fictive utilisée par les tests et la prise en main."""

    name = "demo"
    display_name = "Banque Demo"
    base_url = "http://127.0.0.1:8777"

    @property
    def spec(self) -> dict[str, Any]:
        """Complète la configuration fournie par les valeurs de démonstration."""
        return {**DEMO_SPEC, **(self.config.options or {})}
