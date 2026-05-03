"""Typer CLI: serve the dashboard, or run individual jobs once."""

from __future__ import annotations

import asyncio

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from .classifier import run_classifier
from .config import get_env
from .db import init_db
from .logging_setup import configure_logging
from .workers.discovery import run_discovery
from .workers.enrich import run_enrichment
from .workers.snapshot import prune_inactive, run_snapshot
from .export_csv import export_tokens_csv

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _setup() -> None:
    env = get_env()
    configure_logging(env.log_level)


@app.command()
def serve(
    host: str = typer.Option(None, help="Override WEB_HOST"),
    port: int = typer.Option(None, help="Override WEB_PORT"),
    reload: bool = typer.Option(False, help="Reload on code changes (dev)"),
) -> None:
    """Start the FastAPI dashboard + APScheduler in one process."""
    _setup()
    env = get_env()
    uvicorn.run(
        "scrape_coins.web.app:app",
        host=host or env.web_host,
        port=port or env.web_port,
        reload=reload,
        log_level=env.log_level.lower(),
    )


@app.command("init-db")
def init_db_cmd() -> None:
    """Create the SQLite tables (idempotent)."""
    _setup()
    asyncio.run(init_db())
    console.print("[green]✓[/green] db initialized at", get_env().scrape_coins_db)


@app.command()
def discover() -> None:
    """Run the discovery worker once."""
    _setup()
    asyncio.run(init_db())
    counts = asyncio.run(run_discovery())
    _table("discovery results", counts)


@app.command()
def snapshot(
    force: bool = typer.Option(
        False, "--force", help="Bypass the per-token cooldown and re-snapshot everything"
    ),
) -> None:
    """Run the snapshot worker once."""
    _setup()
    asyncio.run(init_db())
    counts = asyncio.run(run_snapshot(force=force))
    _table("snapshot results", counts)


@app.command()
def enrich() -> None:
    """Run the Helius enrichment worker once."""
    _setup()
    asyncio.run(init_db())
    counts = asyncio.run(run_enrichment())
    _table("enrichment results", counts)


@app.command()
def classify() -> None:
    """Run the classifier once over all tracked tokens."""
    _setup()
    asyncio.run(init_db())
    counts = asyncio.run(run_classifier())
    _table("classifier results", counts)


@app.command()
def prune() -> None:
    """Drop tokens we haven't seen in N days (config: snapshot.prune_after_days_inactive)."""
    _setup()
    asyncio.run(init_db())
    n = asyncio.run(prune_inactive())
    console.print(f"[yellow]pruned[/yellow] {n} tokens")


@app.command("export-csv")
def export_csv_cmd(
    out: str = typer.Option(
        "coins_export.csv", "--out", "-o", help="Output CSV path"
    ),
    only_candidates: bool = typer.Option(
        False,
        "--only-candidates",
        help="Only tokens marked as redeploy candidates",
    ),
    only_dexscreener_paid: bool = typer.Option(
        False,
        "--only-dexscreener-paid",
        help="Only tokens seen on DexScreener paid boost feeds (boosts/latest, boosts/top)",
    ),
) -> None:
    """Export tokens (+ classification + enrichment) to a CSV file."""
    _setup()
    asyncio.run(init_db())
    n = asyncio.run(
        export_tokens_csv(
            out,
            only_candidates=only_candidates,
            only_dexscreener_paid_boost=only_dexscreener_paid,
        )
    )
    console.print(f"[green]✓[/green] wrote [bold]{n}[/bold] rows to [cyan]{out}[/cyan]")


@app.command()
def cycle() -> None:
    """Run discovery → snapshot → enrichment → classify once, in order."""
    _setup()
    asyncio.run(init_db())
    for name, fn in [
        ("discovery", run_discovery),
        ("snapshot", run_snapshot),
        ("enrich", run_enrichment),
        ("classify", run_classifier),
    ]:
        console.rule(f"[bold]{name}[/bold]")
        result = asyncio.run(fn())
        _table(name, result)


def _table(title: str, data: dict) -> None:
    t = Table(title=title)
    t.add_column("metric")
    t.add_column("value", justify="right")
    for k, v in (data or {}).items():
        t.add_row(str(k), str(v))
    console.print(t)


if __name__ == "__main__":
    app()
