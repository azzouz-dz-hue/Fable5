"""Tableau de bord FastAPI : consultation en lecture seule de la base.

Aucune action bancaire n'est déclenchée depuis l'interface : elle ne fait que
lire ce que les extractions ont déposé, ce qui permet de la laisser ouverte
sans risque.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import Settings, load_settings
from ..normalize import format_amount
from ..storage import Database

TEMPLATES_DIR = Path(__file__).parent / "templates"

MONTHS_FR = [
    "", "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc.",
]


def monthly_flows(transactions, months: int = 12) -> list[dict]:
    """Agrège débits et crédits par mois, du plus ancien au plus récent."""
    buckets: OrderedDict[tuple[int, int], dict] = OrderedDict()
    for transaction in sorted(transactions, key=lambda t: t.date):
        key = (transaction.date.year, transaction.date.month)
        bucket = buckets.setdefault(
            key,
            {"label": f"{MONTHS_FR[key[1]]} {str(key[0])[2:]}", "debit": Decimal(0),
             "credit": Decimal(0)},
        )
        if transaction.amount < 0:
            bucket["debit"] += -transaction.amount
        else:
            bucket["credit"] += transaction.amount
    return list(buckets.values())[-months:]


def build_chart(flows: list[dict], width: int = 720, height: int = 240) -> dict:
    """Pré-calcule la géométrie des barres groupées.

    Le gabarit reste ainsi purement déclaratif : aucune arithmétique dans le HTML.
    """
    padding = {"left": 8, "right": 8, "top": 16, "bottom": 28}
    plot_height = height - padding["top"] - padding["bottom"]
    plot_width = width - padding["left"] - padding["right"]

    if not flows:
        return {"bars": [], "width": width, "height": height, "max": 0, "ticks": []}

    peak = max(max(f["debit"], f["credit"]) for f in flows) or Decimal(1)
    group_width = plot_width / len(flows)
    baseline = padding["top"] + plot_height
    # Marques fines, et 2px de surface entre deux barres adjacentes.
    bar_width = min(max((group_width - 14) / 2, 4), 18)

    bars = []
    for index, flow in enumerate(flows):
        group_x = padding["left"] + index * group_width
        for offset, (series, value) in enumerate(
            (("credit", flow["credit"]), ("debit", flow["debit"]))
        ):
            bar_height = max(float(value) / float(peak) * plot_height, 1.5)
            x = group_x + (group_width - 2 * bar_width - 2) / 2 + offset * (bar_width + 2)
            bars.append(
                {
                    "series": series,
                    "path": _bar_path(x, baseline - bar_height, bar_width, bar_height),
                    "label": flow["label"],
                    "value": format_amount(value),
                }
            )
        bars.append(
            {
                "series": "axis",
                "x": group_x + group_width / 2,
                "y": height - 10,
                "label": flow["label"],
            }
        )
    return {
        "bars": bars,
        "width": width,
        "height": height,
        "baseline": baseline,
        "peak": format_amount(peak),
    }


def _bar_path(x: float, y: float, width: float, height: float, radius: float = 4) -> str:
    """Barre arrondie en tête seulement : le pied reste posé sur la ligne de base."""
    r = min(radius, width / 2, height)
    return (
        f"M {x:.1f} {y + height:.1f} L {x:.1f} {y + r:.1f} "
        f"Q {x:.1f} {y:.1f} {x + r:.1f} {y:.1f} "
        f"L {x + width - r:.1f} {y:.1f} "
        f"Q {x + width:.1f} {y:.1f} {x + width:.1f} {y + r:.1f} "
        f"L {x + width:.1f} {y + height:.1f} Z"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construit l'application ; `settings` facilite les tests."""
    settings = settings or load_settings()
    database = Database(settings.paths.database_url)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["montant"] = format_amount

    app = FastAPI(title="BankExtract", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        compte: str | None = Query(default=None),
        limite: int = Query(default=100, ge=10, le=1000),
    ) -> HTMLResponse:
        accounts = database.accounts()
        transactions = database.transactions(account_key=compte, limit=limite)
        chart_source = database.transactions(account_key=compte, limit=5000)

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "totaux": database.totals(),
                "comptes": accounts,
                "ecritures": transactions,
                "compte_actif": compte,
                "limite": limite,
                "graphique": build_chart(monthly_flows(chart_source)),
                "executions": database.runs(limit=8),
                "aujourdhui": date.today(),
                "solde_total": _total_balance(accounts),
            },
        )

    @app.get("/sante")
    def health() -> dict:
        return {"statut": "ok", **{k: str(v) for k, v in database.totals().items()}}

    return app


def _total_balance(accounts) -> dict[str, str]:
    """Totalise les soldes par devise — additionner DZD et EUR n'aurait aucun sens."""
    totals: dict[str, Decimal] = {}
    for account in accounts:
        if account.balance is None:
            continue
        totals[account.currency] = totals.get(account.currency, Decimal(0)) + account.balance
    return {currency: format_amount(value) for currency, value in sorted(totals.items())}
