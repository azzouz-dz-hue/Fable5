"""Interface en ligne de commande."""

from __future__ import annotations

import getpass
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import DEFAULT_CONFIG_PATH, load_settings
from .connectors import available_connectors, build_connector
from .normalize import format_amount
from .otp import available_providers
from .pipeline import export_database, run_banks
from .scheduler import (
    DEFAULT_SCHEDULES_PATH,
    DEFAULT_STATE_PATH,
    SchedulerState,
    load_scheduler_config,
    next_run,
    run_due_schedules,
)
from .secrets import Credentials, delete_credentials, store_credentials
from .storage import Database

app = typer.Typer(
    help="Extraction automatisée des relevés de comptes bancaires.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise typer.BadParameter(f"Date illisible : « {value} » (attendu AAAA-MM-JJ ou JJ/MM/AAAA)")


@app.command()
def extract(
    banks: list[str] = typer.Argument(None, help="Banques à extraire (toutes par défaut)."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Fichier YAML."),
    since: str = typer.Option(None, "--since", help="Date de début (AAAA-MM-JJ)."),
    until: str = typer.Option(None, "--until", help="Date de fin (AAAA-MM-JJ)."),
    days: int = typer.Option(None, "--days", "-d", help="Profondeur d'historique en jours."),
    no_export: bool = typer.Option(False, "--no-export", help="Alimenter la base sans exporter."),
    show: bool = typer.Option(False, "--headed", help="Afficher le navigateur."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Lance l'extraction des relevés et alimente la base."""
    _setup_logging(verbose)
    settings = load_settings(config)
    if show:
        settings.browser.headless = False

    end = _parse_date(until) or date.today()
    start = _parse_date(since) or (end - timedelta(days=days) if days else None)

    try:
        outcomes = run_banks(settings, banks or None, start=start, end=end, export=not no_export)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    if not outcomes:
        console.print("[yellow]Aucune banque activée. Vérifiez config/banks.yaml.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title="Résultat de l'extraction", show_lines=False)
    for column in ("Banque", "Comptes", "Écritures", "Nouvelles", "Relevés", "État"):
        table.add_column(column)
    for outcome in outcomes:
        state = "[green]OK[/green]" if outcome.ok else "[red]ERREUR[/red]"
        table.add_row(
            outcome.bank,
            str(len(outcome.result.accounts)),
            str(len(outcome.result.transactions)),
            str(outcome.saved.new_transactions if outcome.saved else 0),
            str(len(outcome.result.files)),
            state,
        )
    console.print(table)

    for outcome in outcomes:
        for path in outcome.exports:
            console.print(f"  [cyan]→[/cyan] {path}")
        for error in outcome.result.errors:
            console.print(f"  [red]✗ {outcome.bank} : {error}[/red]")

    if any(not outcome.ok for outcome in outcomes):
        raise typer.Exit(code=1)


@app.command()
def login(
    bank: str = typer.Argument(..., help="Banque telle que nommée dans la configuration."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Enregistre les identifiants d'une banque dans le trousseau système."""
    settings = load_settings(config)
    if bank not in settings.banks:
        console.print(f"[red]Banque « {bank} » absente de {config}.[/red]")
        raise typer.Exit(code=2)

    bank_config = settings.banks[bank]
    console.print(f"Identifiants pour [bold]{bank_config.display_name}[/bold]")
    username = typer.prompt("Identifiant")
    password = getpass.getpass("Mot de passe : ")
    confirm = getpass.getpass("Confirmation : ")
    if password != confirm:
        console.print("[red]Les mots de passe diffèrent.[/red]")
        raise typer.Exit(code=1)

    try:
        service = store_credentials(bank, bank_config, Credentials(username, password))
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Enregistré[/green] dans le trousseau (service « {service} »).")


@app.command()
def logout(
    bank: str = typer.Argument(...),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Supprime les identifiants d'une banque du trousseau."""
    settings = load_settings(config)
    if bank not in settings.banks:
        console.print(f"[red]Banque « {bank} » inconnue.[/red]")
        raise typer.Exit(code=2)
    delete_credentials(bank, settings.banks[bank])
    console.print(f"[green]Identifiants de « {bank} » supprimés.[/green]")


@app.command(name="list")
def list_items(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Affiche les banques configurées, les connecteurs et les sources d'OTP."""
    settings = load_settings(config)

    table = Table(title="Banques configurées")
    for column in ("Nom", "Connecteur", "Actif", "OTP", "Historique"):
        table.add_column(column)
    for name, bank in settings.banks.items():
        table.add_row(
            name,
            bank.connector,
            "[green]oui[/green]" if bank.enabled else "[dim]non[/dim]",
            bank.otp.provider,
            f"{bank.history_days} j",
        )
    console.print(table)
    console.print(f"Connecteurs disponibles : [cyan]{', '.join(available_connectors())}[/cyan]")
    console.print(f"Sources d'OTP : [cyan]{', '.join(available_providers())}[/cyan]")


@app.command()
def accounts(config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c")) -> None:
    """Affiche les comptes connus de la base."""
    settings = load_settings(config)
    rows = Database(settings.paths.database_url).accounts()
    if not rows:
        console.print("[yellow]Base vide — lancez « bankextract extract ».[/yellow]")
        return

    table = Table(title="Comptes")
    for column in ("Banque", "Numéro", "Libellé", "Solde", "Au"):
        table.add_column(column)
    for account in rows:
        table.add_row(
            account.bank.upper(),
            account.number,
            account.label,
            f"{format_amount(account.balance)} {account.currency}",
            account.balance_date.strftime("%d/%m/%Y") if account.balance_date else "—",
        )
    console.print(table)


@app.command(name="export")
def export_command(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    since: str = typer.Option(None, "--since"),
    until: str = typer.Option(None, "--until"),
    account: str = typer.Option(None, "--account", help="Clé de compte (banque:numéro)."),
) -> None:
    """Exporte l'historique de la base en CSV et Excel."""
    settings = load_settings(config)
    paths = export_database(
        settings, start=_parse_date(since), end=_parse_date(until), account_key=account
    )
    if not paths:
        console.print("[yellow]Aucune écriture à exporter.[/yellow]")
        raise typer.Exit(code=1)
    for path in paths:
        console.print(f"[green]→[/green] {path}")


@app.command()
def history(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Affiche le journal des dernières exécutions."""
    settings = load_settings(config)
    table = Table(title="Exécutions")
    for column in ("Banque", "Début", "Durée", "Comptes", "Nouvelles", "Erreurs"):
        table.add_column(column)
    for run in Database(settings.paths.database_url).runs(limit):
        duration = (
            f"{(run.finished_at - run.started_at).total_seconds():.0f} s"
            if run.finished_at
            else "—"
        )
        table.add_row(
            run.bank,
            run.started_at.strftime("%d/%m %H:%M"),
            duration,
            str(run.accounts_count),
            str(run.new_transactions),
            "[red]oui[/red]" if run.errors else "[green]non[/green]",
        )
    console.print(table)


@app.command()
def inspect(
    bank: str = typer.Argument(..., help="Banque à ouvrir pour relever les sélecteurs."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    seconds: int = typer.Option(300, "--seconds", help="Durée de la session d'inspection."),
) -> None:
    """Ouvre le portail dans un navigateur visible pour relever les sélecteurs CSS.

    Aide à renseigner `config/banks.yaml` pour une banque non encore décrite :
    la page reste ouverte, les outils de développement permettent de copier les
    sélecteurs, et rien n'est extrait.
    """
    import time

    from .browser import browser_session

    settings = load_settings(config)
    settings.browser.headless = False
    if bank not in settings.banks:
        console.print(f"[red]Banque « {bank} » absente de {config}.[/red]")
        raise typer.Exit(code=2)

    connector = build_connector(bank, settings.banks[bank], settings)
    url = str((settings.banks[bank].options or {}).get("base_url") or connector.base_url)

    console.print(f"Ouverture de [cyan]{url}[/cyan] — fermez la fenêtre ou attendez {seconds}s.")
    console.print(
        "Relevez les sélecteurs (clic droit → Inspecter), puis reportez-les dans "
        f"[bold]{config}[/bold] sous la section de la banque."
    )
    with browser_session(settings.browser, bank) as session:
        session.goto(url)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not session.context.pages:
                break
            time.sleep(1)


@app.command()
def dashboard(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
) -> None:
    """Lance le tableau de bord web de consultation."""
    import uvicorn

    from .dashboard.app import create_app

    settings = load_settings(config)
    console.print(f"Tableau de bord : [cyan]http://{host}:{port}[/cyan]")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


@app.command()
def setup(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
) -> None:
    """Prépare le poste : télécharge le navigateur et crée les dossiers de travail.

    À lancer une seule fois après l'installation. Le téléchargement du
    navigateur pèse environ 150 Mo.
    """
    import subprocess

    console.print("[bold]Préparation du poste[/bold]\n")

    settings = load_settings(config)
    settings.paths.ensure()
    console.print(f"  [green]✓[/green] dossiers de travail prêts ({settings.paths.data_dir})")

    if settings.browser.executable_path and Path(settings.browser.executable_path).exists():
        console.print(
            f"  [green]✓[/green] navigateur déjà présent "
            f"({settings.browser.executable_path})"
        )
    else:
        console.print("  [dim]…[/dim] téléchargement du navigateur (environ 150 Mo, patientez)")
        try:
            issue = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                text=True,
                timeout=900,
            )
        except Exception as exc:
            console.print(f"  [red]✗[/red] téléchargement impossible : {exc}")
            raise typer.Exit(code=1) from exc
        if issue.returncode != 0:
            console.print(f"  [red]✗[/red] {issue.stderr.strip()[:400]}")
            console.print(
                "\n  Vérifiez votre connexion, puis relancez [cyan]bankextract setup[/cyan]."
            )
            raise typer.Exit(code=1)
        console.print("  [green]✓[/green] navigateur installé")

    if not Path(config).exists():
        console.print(
            f"  [yellow]![/yellow] configuration absente ({config}) — "
            "copiez le fichier config/banks.yaml fourni."
        )
    else:
        console.print(f"  [green]✓[/green] configuration lue ({config})")

    console.print("\n[green]Poste prêt.[/green] Étape suivante :")
    console.print("  [cyan]bankextract record <banque> --url https://portail-de-votre-banque[/cyan]")



# ---------------------------------------------------------------------- parcours


@app.command()
def record(
    bank: str = typer.Argument(..., help="Nom de la banque (ex. « bna »)."),
    url: str = typer.Option(None, "--url", help="Adresse du portail ; sinon celle du connecteur."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    scenarios_dir: Path = typer.Option(Path("scenarios"), "--scenarios-dir"),
    label: str = typer.Option("", "--label", help="Nom lisible de la banque."),
    minutes: int = typer.Option(30, "--minutes", help="Durée maximale de l'enregistrement."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Enregistre, dans un navigateur, la procédure de téléchargement d'un relevé.

    Faites la manipulation une seule fois — connexion, code SMS, navigation,
    téléchargement — puis fermez la fenêtre. Le parcours est enregistré et pourra
    être rejoué automatiquement.

    Le mot de passe et le code SMS ne sont jamais enregistrés : seuls des
    marqueurs le sont, remplacés au moment du rejeu.
    """
    _setup_logging(verbose)
    from .recorder import record_scenario, scenario_path
    from .recorder.record import annotate_scenario

    settings = load_settings(config)
    bank_config = settings.banks.get(bank)
    start_url = url or (bank_config.options or {}).get("base_url") if bank_config else url
    if not start_url:
        console.print(
            f"[red]Aucune adresse pour « {bank} ».[/red] "
            "Indiquez-la avec --url, par exemple : --url https://ebanking.bna.dz"
        )
        raise typer.Exit(code=2)

    destination = scenario_path(scenarios_dir, bank)
    if destination.exists():
        console.print(f"[yellow]Un parcours existe déjà : {destination}[/yellow]")
        if not typer.confirm("Le remplacer ?", default=False):
            raise typer.Exit(code=0)

    console.print()
    console.print(f"[bold]Enregistrement du parcours {label or bank.upper()}[/bold]")
    console.print(f"Une fenêtre s'ouvre sur [cyan]{start_url}[/cyan].")
    console.print("  1. Connectez-vous normalement (le mot de passe n'est pas enregistré).")
    console.print("  2. Saisissez le code SMS s'il est demandé.")
    console.print("  3. Allez jusqu'au téléchargement du relevé, et téléchargez-le.")
    console.print("  4. [bold]Fermez la fenêtre[/bold] pour terminer l'enregistrement.")
    console.print()

    scenario = record_scenario(
        bank,
        str(start_url),
        settings.browser,
        label=label,
        max_seconds=minutes * 60,
    )

    if len(scenario.steps) <= 1:
        console.print(
            "[red]Aucune action enregistrée.[/red] "
            "La fenêtre a-t-elle été fermée trop tôt ?"
        )
        raise typer.Exit(code=1)

    notes = annotate_scenario(scenario)
    _show_scenario(scenario, notes)

    suspects = scenario.contains_secret_values()
    if suspects:
        console.print(
            "[red]Attention : une valeur sensible pourrait avoir été enregistrée "
            f"({'; '.join(suspects)}). Vérifiez le fichier avant de l'utiliser.[/red]"
        )

    scenario.save(destination)
    console.print(f"\n[green]Parcours enregistré[/green] → {destination}")

    if not scenario.downloads:
        console.print(
            "[yellow]Aucun téléchargement détecté.[/yellow] Le rejeu naviguera sans "
            "récupérer de fichier — réenregistrez en cliquant bien sur le lien du relevé."
        )

    console.print("\nAjoutez ceci à votre configuration pour activer le rejeu :\n")
    console.print(_config_snippet(bank, scenario, destination), style="cyan")


def _show_scenario(scenario, notes: list[str]) -> None:
    table = Table(title=f"Parcours {scenario.display_name} — {scenario.summary()}")
    table.add_column("#", justify="right")
    table.add_column("Action")
    table.add_column("Détail")
    for index, step in enumerate(scenario.steps, start=1):
        table.add_row(str(index), step.action.value, step.describe())
    console.print(table)
    for note in notes:
        console.print(f"  [dim]↳ {note}[/dim]")


def _config_snippet(bank: str, scenario, destination: Path) -> str:
    otp = "sms_gateway" if scenario.uses_otp else "manual"
    return f"""banks:
  {bank}:
    connector: scenario
    enabled: true
    label: {scenario.display_name}
    username_env: {bank.upper()}_USERNAME
    password_env: {bank.upper()}_PASSWORD
    otp:
      provider: {otp}
    options:
      scenario_path: {destination}
      account_number: "à renseigner"
"""


@app.command()
def scenarios(
    scenarios_dir: Path = typer.Option(Path("scenarios"), "--scenarios-dir"),
) -> None:
    """Liste les parcours enregistrés."""
    from .recorder import Scenario

    files = sorted(Path(scenarios_dir).glob("*.json")) if Path(scenarios_dir).exists() else []
    if not files:
        console.print(
            f"[yellow]Aucun parcours dans {scenarios_dir}.[/yellow] "
            "Enregistrez-en un avec « bankextract record <banque> »."
        )
        return

    table = Table(title="Parcours enregistrés")
    for column in ("Banque", "Étapes", "Téléchargements", "OTP", "Période", "Enregistré le"):
        table.add_column(column)
    for path in files:
        try:
            scenario = Scenario.load(path)
        except Exception as exc:
            table.add_row(path.stem, f"[red]illisible ({exc})[/red]", "", "", "", "")
            continue
        table.add_row(
            scenario.display_name,
            str(len(scenario.steps)),
            str(len(scenario.downloads)),
            "oui" if scenario.uses_otp else "non",
            "variable" if scenario.uses_period else "figée",
            scenario.created_at.strftime("%d/%m/%Y"),
        )
    console.print(table)


@app.command()
def replay(
    bank: str = typer.Argument(..., help="Banque dont le parcours doit être rejoué."),
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    since: str = typer.Option(None, "--since"),
    until: str = typer.Option(None, "--until"),
    days: int = typer.Option(None, "--days", "-d"),
    show: bool = typer.Option(False, "--headed", help="Afficher le navigateur."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Rejoue une fois le parcours enregistré d'une banque.

    Équivalent à « extract » pour une banque dont le connecteur est « scenario ».
    """
    _setup_logging(verbose)
    settings = load_settings(config)
    if show:
        settings.browser.headless = False
    if bank not in settings.banks:
        console.print(f"[red]Banque « {bank} » absente de {config}.[/red]")
        raise typer.Exit(code=2)

    end = _parse_date(until) or date.today()
    start = _parse_date(since) or (end - timedelta(days=days) if days else None)

    outcomes = run_banks(settings, [bank], start=start, end=end)
    outcome = outcomes[0]
    console.print(outcome.describe())
    for path in outcome.exports:
        console.print(f"  [cyan]→[/cyan] {path}")
    for error in outcome.result.errors:
        console.print(f"  [red]✗ {error}[/red]")
    if not outcome.ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------- récurrences


@app.command()
def schedules(
    schedules_config: Path = typer.Option(
        DEFAULT_SCHEDULES_PATH, "--schedules", "-s", help="Fichier des récurrences."
    ),
    state_path: Path = typer.Option(DEFAULT_STATE_PATH, "--state"),
) -> None:
    """Affiche les extractions programmées et leur prochaine échéance."""
    config = load_scheduler_config(schedules_config)
    if not config.schedules:
        console.print(
            f"[yellow]Aucune récurrence dans {schedules_config}.[/yellow] "
            "Ajoutez-en une sous « schedules: » puis passez « enabled: true »."
        )
        return

    state = SchedulerState(state_path)
    now = datetime.now()

    table = Table(title="Extractions programmées")
    for column in ("Tâche", "Banques", "Récurrence", "Période", "Destinataires", "Prochaine"):
        table.add_column(column)
    for schedule in config.schedules:
        last = state.last_run(schedule.name)
        prochaine = next_run(schedule.recurrence, last or now)
        table.add_row(
            schedule.name if schedule.enabled else f"[dim]{schedule.name} (inactive)[/dim]",
            ", ".join(schedule.banks) or "toutes",
            schedule.recurrence.describe(),
            schedule.period.value,
            ", ".join(schedule.mail.to) if schedule.mail.enabled else "[dim]pas d'envoi[/dim]",
            prochaine.strftime("%d/%m/%Y %H:%M") if schedule.enabled else "—",
        )
    console.print(table)
    console.print(f"[dim]SMTP : {config.smtp.host or 'non configuré'}[/dim]")


@app.command()
def scheduler(
    config: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c"),
    schedules_config: Path = typer.Option(DEFAULT_SCHEDULES_PATH, "--schedules", "-s"),
    state_path: Path = typer.Option(DEFAULT_STATE_PATH, "--state"),
    once: bool = typer.Option(
        False, "--once", help="Vérifier une fois puis sortir (pour un appel par cron)."
    ),
    interval: int = typer.Option(300, "--interval", help="Secondes entre deux vérifications."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Exécute les extractions programmées et envoie les relevés par courriel.

    Sans « --once », la commande tourne en continu ; avec, elle vérifie une fois
    et sort, ce qui convient à une entrée cron horaire.
    """
    import time as _time

    _setup_logging(verbose)
    settings = load_settings(config)
    scheduler_config = load_scheduler_config(schedules_config)
    state = SchedulerState(state_path)

    if not scheduler_config.enabled_schedules():
        console.print(f"[yellow]Aucune récurrence active dans {schedules_config}.[/yellow]")
        raise typer.Exit(code=1)

    if once:
        executed = run_due_schedules(settings, scheduler_config, state)
        if executed:
            console.print(f"[green]Exécutée(s) :[/green] {', '.join(executed)}")
        else:
            console.print("[dim]Aucune tâche échue.[/dim]")
        return

    console.print(
        f"Planificateur démarré — {len(scheduler_config.enabled_schedules())} tâche(s), "
        f"vérification toutes les {interval}s. Ctrl+C pour arrêter."
    )
    try:
        while True:
            executed = run_due_schedules(settings, scheduler_config, state)
            for name in executed:
                console.print(f"[green]{datetime.now():%d/%m %H:%M}[/green] — {name} exécutée")
            _time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Planificateur arrêté.[/dim]")


@app.command(name="mail-test")
def mail_test(
    to: str = typer.Argument(..., help="Adresse de destination."),
    schedules_config: Path = typer.Option(DEFAULT_SCHEDULES_PATH, "--schedules", "-s"),
) -> None:
    """Envoie un message de vérification pour valider la configuration SMTP."""
    from .mailer import MailConfig, Mailer
    from .models import ExtractionResult

    config = load_scheduler_config(schedules_config)
    if not config.smtp.configured:
        console.print(
            f"[red]SMTP non configuré dans {schedules_config}[/red] "
            "(« host » et « sender » sont obligatoires)."
        )
        raise typer.Exit(code=2)

    essai = ExtractionResult(bank="test")
    essai.finished_at = datetime.now()
    report = Mailer(config.smtp).send_result(
        essai,
        MailConfig(
            enabled=True,
            to=[to],
            subject="BankExtract — message de vérification",
            attach_statements=False,
            attach_exports=False,
        ),
    )

    if report.sent:
        console.print(f"[green]Message envoyé[/green] à {to}.")
    else:
        console.print(f"[red]Échec :[/red] {report}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
