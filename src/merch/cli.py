from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
import uvicorn
from alembic.config import Config
from argon2 import PasswordHasher

from alembic import command
from merch.artwork_replacement import (
    reconcile_published_artwork,
    replace_published_artwork_file,
)
from merch.config import get_settings
from merch.copy_refresh import prepare_copy_refresh_batch
from merch.database import get_engine, session_scope
from merch.defaults import fixture_product_template
from merch.models import Base
from merch.observability import configure_observability
from merch.pipeline import (
    finish_publishing,
    health_connectors,
    import_etsy_listing_defaults,
    publish_channel_run,
    record_approval,
    repair_published_etsy_listing,
    run_fixture_pipeline,
    sync_analytics,
)
from merch.repository import ConfigurationRepository, RunRepository
from merch.schemas import ApprovalSignal, Channel, CreativeBrief, RunInput
from merch.temporal import (
    reconcile_schedules,
    resume_researched_run,
    retry_failed_artwork_run,
    run_worker,
    start_manual_run,
)

app = typer.Typer(help="Autonomous POD production operator commands", no_args_is_help=True)


@app.command()
def web(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Start the FastAPI operator console."""
    uvicorn.run("merch.web:app", host=host, port=port, reload=reload, proxy_headers=True)


@app.command()
def worker() -> None:
    """Start the Temporal activity and workflow worker."""
    settings = get_settings()
    configure_observability(settings)
    asyncio.run(run_worker(settings))


@app.command("migrate")
def migrate(revision: str = "head") -> None:
    """Apply Alembic database migrations."""
    command.upgrade(Config("alembic.ini"), revision)


@app.command("init-db")
def init_db() -> None:
    """Create current tables directly, useful only for isolated local fixtures."""
    Base.metadata.create_all(get_engine())
    typer.echo("database initialized")


@app.command("schedule")
def schedule() -> None:
    """Create or update the daily analytics and production schedules at 09:30."""
    asyncio.run(reconcile_schedules())
    typer.echo("schedules reconciled")


@app.command("setup-comfort-colors")
def comfort_colors_setup(
    costs_file: Annotated[Path | None, typer.Option("--costs-file")] = None,
    activate: Annotated[bool, typer.Option("--activate")] = False,
) -> None:
    """Preview Comfort Colors 1717; activate only with reviewed variant costs."""
    from merch.setup_comfort_colors import read_reviewed_costs, setup_comfort_colors

    try:
        costs = read_reviewed_costs(costs_file) if costs_file else None
        result = asyncio.run(setup_comfort_colors(costs, activate=activate))
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("run")
def manual_run() -> None:
    """Start a manual production workflow."""
    value = asyncio.run(start_manual_run())
    typer.echo(f"started {value.run_id}")


@app.command("resume-research")
def resume_research(run_id: str) -> None:
    """Resume a failed run from its saved real research, without repeating the model call."""
    value = asyncio.run(resume_researched_run(run_id))
    typer.echo(f"resumed {value.run_id}")


@app.command("retry-artwork")
def retry_artwork(
    run_id: str, brief_file: Annotated[Path | None, typer.Option("--brief-file")] = None
) -> None:
    """Retry failed QA artwork; repeated defects require a revised brief JSON file."""
    brief = (
        CreativeBrief.model_validate(json.loads(brief_file.read_text()))
        if brief_file is not None
        else None
    )
    value = asyncio.run(retry_failed_artwork_run(run_id, revised_brief=brief))
    typer.echo(f"retrying artwork for {value.run_id}")


@app.command("analytics")
def analytics() -> None:
    """Synchronize configured read-only marketplace analytics."""
    typer.echo(asyncio.run(sync_analytics()))


@app.command("connections")
def connections() -> None:
    """Perform read-only connector checks."""
    typer.echo(asyncio.run(health_connectors()))


@app.command("catalog-sync")
def catalog_sync() -> None:
    """Synchronize the complete supported Printify blueprint/provider catalog."""
    from merch.services.catalog import sync_catalog

    typer.echo(json.dumps(asyncio.run(sync_catalog()), indent=2, default=str))


@app.command("connect-browser")
def connect_browser(provider: str) -> None:
    """Open an interactive provider login and save encrypted browser session state."""
    if provider.casefold() != "printify":
        raise typer.BadParameter("the only browser session currently supported is printify")
    from merch.services.browser_session import connect_printify_browser

    typer.echo(
        json.dumps(asyncio.run(connect_printify_browser()), indent=2, default=str)
    )


@app.command("session-health")
def session_health() -> None:
    """Inspect the encrypted Printify browser session without changing it."""
    from merch.services.browser_session import browser_session_health

    typer.echo(json.dumps(browser_session_health(), indent=2, default=str))


@app.command("source-health")
def marketplace_source_health() -> None:
    """Check direct-collector policy access and configured fallback availability."""
    from merch.services.marketplace_research import source_health

    typer.echo(json.dumps(asyncio.run(source_health()), indent=2, default=str))


@app.command("research-smoke")
def research_smoke(query: str) -> None:
    """Collect listing-specific evidence without creating or publishing a product."""
    from merch.services.marketplace_research import collect_marketplace_evidence

    evidence = asyncio.run(collect_marketplace_evidence(query))
    typer.echo(json.dumps([item.model_dump(mode="json") for item in evidence], indent=2))


@app.command("repair-etsy-listing")
def repair_etsy_listing(run_id: str) -> None:
    """Recheck a completed Etsy run and set its selectors to Size and Color."""
    listing_id = asyncio.run(repair_published_etsy_listing(run_id))
    typer.echo(f"verified Etsy listing {listing_id}: Size, Color")


@app.command("replace-published-artwork")
def replace_published_artwork_command(
    run_id: str,
    artwork_file: Annotated[Path, typer.Option("--artwork-file")],
    apply: Annotated[bool, typer.Option("--apply")] = False,
    confirm: Annotated[str | None, typer.Option("--confirm")] = None,
    quality_attestation: Annotated[
        Path | None, typer.Option("--quality-attestation")
    ] = None,
    mockup_timeout_seconds: Annotated[
        float, typer.Option("--mockup-timeout-seconds", min=30, max=3600)
    ] = 600,
    mockup_interval_seconds: Annotated[
        float, typer.Option("--mockup-interval-seconds", min=1, max=60)
    ] = 5,
) -> None:
    """Preflight or replace artwork on the existing Printify/Etsy product in place."""
    try:
        result = asyncio.run(
            replace_published_artwork_file(
                run_id,
                artwork_file,
                apply=apply,
                confirmation=confirm,
                quality_attestation_file=quality_attestation,
                mockup_timeout_seconds=mockup_timeout_seconds,
                mockup_interval_seconds=mockup_interval_seconds,
            )
        )
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("reconcile-published-artwork")
def reconcile_published_artwork_command(
    run_id: str,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    confirm: Annotated[str | None, typer.Option("--confirm")] = None,
) -> None:
    """Inspect or safely finish a failed published-artwork replacement."""
    try:
        result = asyncio.run(
            reconcile_published_artwork(
                run_id,
                apply=apply,
                confirmation=confirm,
            )
        )
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))


@app.command("prepare-copy-refresh")
def prepare_copy_refresh(
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
) -> None:
    """Stage a reviewable copy update for mapped live Etsy listings."""
    batch_id = asyncio.run(prepare_copy_refresh_batch(run_id=run_id))
    typer.echo(f"Copy refresh ready for review: /copy-refresh (batch {batch_id})")


@app.command("import-etsy-defaults")
def import_etsy_defaults(listing_id: int) -> None:
    """Import validated Etsy listing profiles from a previously published app listing."""
    version = asyncio.run(import_etsy_listing_defaults(listing_id))
    typer.echo(f"Etsy listing defaults saved in template v{version}")


@app.command("fixture")
def fixture(approve: bool = False) -> None:
    """Run the full fake-provider pipeline, optionally through dry-run publishing."""
    settings = get_settings()
    if settings.provider_mode != "fake" or settings.publish_mode != "dry_run":
        raise typer.BadParameter("fixture requires fake providers and dry-run publishing")
    Base.metadata.create_all(get_engine())
    with session_scope() as session:
        try:
            ConfigurationRepository(session).get_template()
        except RuntimeError:
            ConfigurationRepository(session).save_template(fixture_product_template())
    value = RunInput(run_id=uuid4(), scheduled_for=datetime.now(UTC), manual=True)
    asyncio.run(run_fixture_pipeline(value, settings))
    if approve:
        signal = ApprovalSignal(
            channels=[Channel.SHOPIFY, Channel.ETSY, Channel.AMAZON_US],
            expected_version=1,
            ip_attested=settings.ip_check_enabled,
            actor="fixture-operator",
        )
        record_approval(str(value.run_id), signal)
        results = [
            asyncio.run(publish_channel_run(str(value.run_id), channel, settings))
            for channel in signal.channels
        ]
        finish_publishing(str(value.run_id), results)
    with session_scope() as session:
        view = RunRepository(session).view(RunRepository(session).get(str(value.run_id)))
        typer.echo(view.model_dump_json(indent=2))


@app.command("hash-password")
def hash_password() -> None:
    """Interactively create an Argon2id admin password hash."""
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    typer.echo(PasswordHasher().hash(password))


if __name__ == "__main__":
    app()
