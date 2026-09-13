"""Connecteurs des banques algériennes.

Aucune banque algérienne ne publie d'API : chaque connecteur pilote le portail
e-banking dans un navigateur. Les sélecteurs CSS vivent dans `config/banks.yaml`
et non ici, car ils changent à chaque refonte du site ; ces classes ne portent
que l'identité de la banque et les particularités de son portail.

Pour renseigner les sélecteurs d'une banque : `bankextract inspect <banque>`
ouvre le portail en mode visible et guide le repérage.
"""

from __future__ import annotations

from .generic import GenericPortalConnector


class BnaConnector(GenericPortalConnector):
    """Banque Nationale d'Algérie."""

    name = "bna"
    display_name = "BNA"
    base_url = "https://ebanking.bna.dz"


class CpaConnector(GenericPortalConnector):
    """Crédit Populaire d'Algérie."""

    name = "cpa"
    display_name = "CPA"
    base_url = "https://ebanking.cpa-bank.dz"


class BeaConnector(GenericPortalConnector):
    """Banque Extérieure d'Algérie."""

    name = "bea"
    display_name = "BEA"
    base_url = "https://ebanking.bea.dz"


class BadrConnector(GenericPortalConnector):
    """Banque de l'Agriculture et du Développement Rural."""

    name = "badr"
    display_name = "BADR"
    base_url = "https://ebanking.badr-bank.dz"


class BdlConnector(GenericPortalConnector):
    """Banque de Développement Local."""

    name = "bdl"
    display_name = "BDL"
    base_url = "https://ebanking.bdl.dz"


class SgaConnector(GenericPortalConnector):
    """Société Générale Algérie."""

    name = "sga"
    display_name = "Société Générale Algérie"
    base_url = "https://www.sgalgerie.dz"


class AgbConnector(GenericPortalConnector):
    """Gulf Bank Algérie."""

    name = "agb"
    display_name = "AGB"
    base_url = "https://ebanking.agb.dz"


class BnpConnector(GenericPortalConnector):
    """BNP Paribas El Djazaïr."""

    name = "bnpad"
    display_name = "BNP Paribas El Djazaïr"
    base_url = "https://www.bnpparibas.dz"


class TrustConnector(GenericPortalConnector):
    """Trust Bank Algeria."""

    name = "trust"
    display_name = "Trust Bank Algeria"
    base_url = "https://ebanking.trustbank.dz"


class CcpConnector(GenericPortalConnector):
    """Algérie Poste — CCP / Baridi Mob (ecc.poste.dz)."""

    name = "ccp"
    display_name = "Algérie Poste (CCP)"
    base_url = "https://ecc.poste.dz"


ALGERIAN_CONNECTORS = (
    BnaConnector,
    CpaConnector,
    BeaConnector,
    BadrConnector,
    BdlConnector,
    SgaConnector,
    AgbConnector,
    BnpConnector,
    TrustConnector,
    CcpConnector,
)
