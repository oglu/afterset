"""CLI entry points for authentication and sync."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import typer

from afterset.config import today

app = typer.Typer(
    name="afterset",
    help="Your Garmin training, sleep, and recovery records.",
    add_completion=False,
)


@app.command()
def auth() -> None:
    """Save Garmin credentials to the system keychain."""
    from afterset.auth import KEYCHAIN_SERVICE, save_credentials

    typer.echo(f"Saving credentials to keychain (service: {KEYCHAIN_SERVICE})")
    email = typer.prompt("Garmin email")
    password = typer.prompt("Garmin password", hide_input=True)
    save_credentials(email, password)
    typer.echo("Credentials saved.")


@app.command()
def login() -> None:
    """Create a fresh Garmin session interactively, without saving a password."""
    from afterset.auth import _prompt_mfa, save_session_tokens
    from garminconnect import Garmin

    email = typer.prompt("Garmin email", hide_input=True)
    password = typer.prompt("Garmin password", hide_input=True)
    try:
        client = Garmin(email, password, prompt_mfa=_prompt_mfa)
        client.login()
        save_session_tokens(client)
    except Exception as exc:
        # HTTP exception text can contain authentication details. Keep the
        # console message limited to the error type and preserve the old cache.
        typer.echo(f"Garmin login failed ({type(exc).__name__}). No automatic retry; check Garmin availability and try later.", err=True)
        raise typer.Exit(1) from None
    typer.echo("Garmin session saved. Password was not saved; no training records were changed.")


@app.command()
def personal_api_setup() -> None:
    """Verify and save an owner-created Personal API key using a hidden prompt."""
    from afterset.personal_api import setup
    key = typer.prompt('Garmin Personal API key', hide_input=True)
    try:
        setup(key)
    except Exception as exc:
        typer.echo(f'Personal API setup failed ({type(exc).__name__}); existing credentials retained.', err=True)
        raise typer.Exit(1) from None
    typer.echo('Personal API key verified and saved privately. Set AFTERSET_GARMIN_BACKEND=personal-api to use it.')


@app.command()
def personal_api_check(renew: bool = typer.Option(False, '--renew', help='Obtain a fresh token to verify hosted renewal.')) -> None:
    """Read-only Personal API verification; prints no records or credentials."""
    from afterset.personal_api import PersonalApiGarmin
    client = None
    try:
        client = PersonalApiGarmin()
        client.verify(renew=renew)
    except Exception as exc:
        typer.echo(f'Personal API check failed ({type(exc).__name__}).', err=True)
        raise typer.Exit(1) from None
    finally:
        if client is not None:
            client.close()
    typer.echo('Personal API authentication and read-only activity request: OK')


@app.command()
def fetch(
    days: int = typer.Option(90, "--days", "-d", help="Days back from today (ignored when --start is given)."),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Start date YYYY-MM-DD."),
    end: Optional[str] = typer.Option(None, "--end", "-e", help="End date YYYY-MM-DD (default: today)."),
) -> None:
    """Fetch Garmin metrics for a date range and store in afterset.db."""
    _sync_range(days, start, end, 'wellness')


@app.command()
def activities(
    start: str = typer.Option(..., "--start", "-s", help="Start date YYYY-MM-DD."),
    end: Optional[str] = typer.Option(None, "--end", "-e", help="End date YYYY-MM-DD."),
    enrich: bool = typer.Option(False, "--enrich", help="Also fetch activity details, splits and samples."),
) -> None:
    """Pull activities and original exercise sets without fetching daily wellness."""
    _sync_range(90, start, end, 'activities', enrich)


def _sync_range(days: int, start: str | None, end: str | None, scope: str, enrich: bool = False) -> None:
    from afterset import connection, store, sync
    from afterset.auth import get_garmin_client

    try:
        end_date = date.fromisoformat(end) if end else today()
        if start is None and days < 1:
            raise ValueError('--days must be positive')
        start_date = date.fromisoformat(start) if start else end_date - timedelta(days=days - 1)
        if start_date > end_date:
            raise ValueError('--start must be on or before --end')
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None

    from afterset.personal_api import configured, PersonalApiGarmin
    if configured():
        client = PersonalApiGarmin()
    else:
        client = get_garmin_client()
    store.initialize()
    failed = False
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=30), end_date)
        typer.echo(f"Syncing {scope}: {current} → {chunk_end}")
        try:
            result = sync.run_sync(current.isoformat(), chunk_end.isoformat(), scope, enrich, client=client)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        progress = result['progress']
        typer.echo(f"  {result['state']}: {progress['completed_items']} items; {len(progress['failures'])} failures")
        if result['state'] != 'completed':
            failed = True
            if progress.get('error'):
                typer.echo(f"  Error: {progress['error']}. {progress['hint']}", err=True)
            for failure in progress['failures']:
                item = failure.get('date', failure.get('activity_id'))
                kind = failure.get('kind', 'wellness')
                typer.echo(f"  {item} ({kind}): {failure['error']}", err=True)
            typer.echo(f"  Inspect job {result['job_id']} with sync_status for failure details.", err=True)
        if progress.get('rate_limited'):
            typer.echo('  Garmin rate limit reached; remaining date ranges were not requested. Retry later.', err=True)
            break
        if chunk_end == end_date:
            break
        current = chunk_end + timedelta(days=1)
    typer.echo(f"Database: {connection.DB_PATH}")
    if failed:
        raise typer.Exit(1)
