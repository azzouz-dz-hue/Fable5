"""Normalisation des montants, dates et libellés issus des portails bancaires.

Les portails algériens mélangent les conventions (séparateur de milliers espace,
insécable ou point ; décimale virgule ou point ; débit tantôt signé, tantôt dans
une colonne dédiée). Tout est ramené ici à `Decimal` et `date`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

__all__ = [
    "clean_label",
    "detect_currency",
    "format_amount",
    "parse_amount",
    "parse_date",
    "signed_amount",
]

# Espaces fines, insécables et autres séparateurs typographiques.
_SPACES = "    \u200b"
_CURRENCY_TOKENS = {
    "DZD": ("DZD", "DA", "DZ", "دج", "دينار"),
    "EUR": ("EUR", "€"),
    "USD": ("USD", "$", "US$"),
    "GBP": ("GBP", "£"),
}

_MONTHS_FR = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "janv": 1, "fev": 2, "avr": 4, "juil": 7, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

_DATE_PATTERNS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%d %m %Y",
)


def strip_accents(text: str) -> str:
    """Retire les diacritiques : « Libellé » et « LIBELLE » doivent se comparer."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def detect_currency(text: str, default: str = "DZD") -> str:
    """Devine la devise d'après le symbole ou le code présent dans le texte."""
    upper = text.upper()
    for code, tokens in _CURRENCY_TOKENS.items():
        for token in tokens:
            if token.upper() in upper:
                return code
    return default


def parse_amount(raw: str | float | Decimal | None) -> Decimal | None:
    """Convertit un montant textuel en `Decimal`.

    Gère « 1 500,50 », « 1.500,50 », « 1,500.50 », « 1500.50 DA »,
    le signe en tête ou en queue, et les parenthèses comptables « (1 500,50) ».
    Renvoie ``None`` pour une cellule vide ou non numérique.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))

    text = str(raw).strip()
    if not text:
        return None

    for ch in _SPACES:
        text = text.replace(ch, " ")

    negative = False
    # Parenthèses comptables : (1 500,50) vaut -1500.50
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    # Retire devises et libellés parasites, garde chiffres et séparateurs.
    text = re.sub(r"[^\d,.\-+]", "", text)
    if not text:
        return None

    if text.endswith("-"):  # signe en suffixe (fréquent sur les exports mainframe)
        negative = True
        text = text[:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]
    text = text.lstrip("+")

    if not text or not any(c.isdigit() for c in text):
        return None

    text = _fix_separators(text)

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _fix_separators(text: str) -> str:
    """Détermine lequel de « , » et « . » est la décimale, et retire les milliers."""
    has_comma, has_dot = "," in text, "." in text

    if has_comma and has_dot:
        # Le dernier séparateur rencontré est la décimale.
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        return text.replace(thousands_sep, "").replace(decimal_sep, ".")

    sep = "," if has_comma else "." if has_dot else ""
    if not sep:
        return text

    parts = text.split(sep)
    # Un seul séparateur suivi de 1 ou 2 chiffres => décimale ; sinon milliers.
    if len(parts) == 2 and len(parts[1]) in (1, 2):
        return f"{parts[0]}.{parts[1]}"
    if len(parts) == 2 and len(parts[1]) == 3 and sep == ".":
        return "".join(parts)  # 1.500 => 1500
    return "".join(parts)


def parse_date(raw: str | date | datetime | None, *, pivot: int = 70) -> date | None:
    """Convertit une date textuelle en `date`.

    Accepte les formats numériques courants et les mois écrits en toutes lettres
    en français. Les années à deux chiffres au-delà de ``pivot`` sont lues en 19xx.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    text = str(raw).strip()
    if not text:
        return None
    for ch in _SPACES:
        text = text.replace(ch, " ")
    text = " ".join(text.split())

    # Isole une date noyée dans un libellé ("Opération du 04/03/2026 à 10h").
    match = re.search(r"\d{1,4}[/\-. ]\d{1,2}[/\-. ]\d{2,4}", text)
    candidate = match.group(0) if match else text

    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(candidate, pattern).date()
        except ValueError:
            continue
        return _fix_two_digit_year(parsed, candidate, pivot)

    return _parse_literal_month(text, pivot)


def _fix_two_digit_year(parsed: date, source: str, pivot: int) -> date:
    """`strptime` place « 26 » en 2026 et « 85 » en 1985 ; on aligne sur le pivot."""
    if not re.search(r"\b\d{4}\b", source) and parsed.year >= 2000:
        two_digit = parsed.year % 100
        if two_digit > pivot:
            return parsed.replace(year=1900 + two_digit)
    return parsed


def _parse_literal_month(text: str, pivot: int) -> date | None:
    normalized = strip_accents(text.lower())
    match = re.search(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{2,4})", normalized)
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS_FR.get(month_name)
    if month is None:
        return None
    year_int = int(year)
    if year_int < 100:
        year_int += 1900 if year_int > pivot else 2000
    try:
        return date(year_int, month, int(day))
    except ValueError:
        return None


def signed_amount(
    debit: str | Decimal | None = None,
    credit: str | Decimal | None = None,
    amount: str | Decimal | None = None,
    sense: str | None = None,
) -> Decimal | None:
    """Ramène à un montant unique signé (négatif au débit).

    Trois présentations coexistent dans les relevés :
    colonnes débit/crédit séparées, montant unique signé, ou montant positif
    accompagné d'un sens (« D » / « C »).
    """
    debit_value = parse_amount(debit)
    credit_value = parse_amount(credit)

    if debit_value:
        return -abs(debit_value)
    if credit_value:
        return abs(credit_value)

    value = parse_amount(amount)
    if value is None:
        # Colonnes présentes mais à zéro : on conserve l'écriture nulle.
        if debit_value is not None or credit_value is not None:
            return Decimal(0)
        return None

    if sense:
        flag = strip_accents(sense.strip().upper())
        if flag.startswith("D") or flag in {"-", "DEBIT"}:
            return -abs(value)
        if flag.startswith("C") or flag in {"+", "CREDIT"}:
            return abs(value)
    return value


def clean_label(raw: str) -> str:
    """Nettoie un libellé d'opération : espaces, retours ligne, bruit d'affichage."""
    if not raw:
        return ""
    text = str(raw)
    for ch in _SPACES:
        text = text.replace(ch, " ")
    text = text.replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" -–—|;")


def format_amount(value: Decimal | float | None, placeholder: str = "—") -> str:
    """Formate un montant à la française : espace pour les milliers, virgule décimale."""
    if value is None:
        return placeholder
    return f"{Decimal(value):,.2f}".replace(",", "\u00a0").replace(".", ",")
