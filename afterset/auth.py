"""Garmin Connect authentication with OAuth token caching."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from afterset.config import token_store

import keyring
import typer
from garminconnect import Garmin, GarminConnectAuthenticationError

TOKEN_STORE = token_store()
KEYCHAIN_SERVICE = "connect.garmin.com"


def get_credentials() -> tuple[str, str]:
    """Return (email, password) from the system keychain."""
    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if email or password:
        if email and password:
            return email, password
        typer.echo("Set both GARMIN_EMAIL and GARMIN_PASSWORD, or unset both to use the system keyring.", err=True)
        raise typer.Exit(1)
    try:
        email = keyring.get_password(KEYCHAIN_SERVICE, "email") or ""
        password = keyring.get_password(KEYCHAIN_SERVICE, "password") or ""
    except keyring.errors.KeyringError:
        _keyring_unavailable()
    if not email or not password:
        typer.echo(
            f"No credentials found in keychain for '{KEYCHAIN_SERVICE}'.\n"
            "Run `afterset auth` to save them.",
            err=True,
        )
        raise typer.Exit(1)
    return email, password


def save_credentials(email: str, password: str) -> None:
    """Store credentials in the system keychain."""
    try:
        keyring.set_password(KEYCHAIN_SERVICE, "email", email)
        keyring.set_password(KEYCHAIN_SERVICE, "password", password)
    except keyring.errors.KeyringError:
        _keyring_unavailable()


def _keyring_unavailable() -> None:
    typer.echo(
        "No usable system keyring. Enable macOS Keychain "
        "or a Linux Secret Service/KWallet backend. For a headless login, set "
        "GARMIN_EMAIL and GARMIN_PASSWORD in the CLI environment, then run afterset fetch.",
        err=True,
    )
    raise typer.Exit(1)


def _prompt_mfa() -> str:
    return typer.prompt("MFA one-time code")


def save_session_tokens(client: Garmin) -> None:
    """Persist renewed tokens privately, without truncating live cache files."""
    TOKEN_STORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN_STORE.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='.renew-', dir=TOKEN_STORE) as staging:
        client.client.dump(staging)
        names = ('garmin_tokens.json',)
        for name in names:
            path = Path(staging) / name
            if not path.is_file():
                raise OSError('Garmin did not produce a complete session cache')
            path.chmod(0o600)
        for name in names:
            os.replace(Path(staging) / name, TOKEN_STORE / name)


def get_garmin_client(email: str | None = None, password: str | None = None) -> Garmin:
    """Return an authenticated Garmin client.

    On first run, performs full SSO login (with optional MFA prompt) and
    caches OAuth tokens in AFTERSET_TOKEN_STORE (default ~/.garmin_tokens). Without
    explicit credentials, first runs read them from the environment or keyring.
    Subsequent runs load the
    cached tokens and persist renewal. A rejected cache requires explicit login;
    old garth-format tokens are retained but cannot be resumed by 0.3.x.
    """
    if (TOKEN_STORE / 'garmin_tokens.json').is_file():
        # Do not give the library credentials while resuming: its internal
        # fallback could otherwise turn a refresh/network failure into SSO.
        client = Garmin()
        try:
            client.login(str(TOKEN_STORE))
        except (GarminConnectAuthenticationError, FileNotFoundError, ValueError):
            raise RuntimeError('Cached Garmin session could not be resumed; run afterset login to authenticate explicitly') from None
        else:
            # Loading an expired access token renews it in memory. Save that
            # renewal so the next process does not repeat the exchange.
            save_session_tokens(client)
            typer.echo("Authenticated via cached tokens.")
            return client

    # Read saved credentials only when no session exists, so a session from
    # `afterset login` is enough for CLI syncs.
    if email is None or password is None:
        email, password = get_credentials()
    typer.echo("Logging in to Garmin Connect (MFA prompt may appear)…")
    client = Garmin(email, password, prompt_mfa=_prompt_mfa)
    client.login()
    save_session_tokens(client)
    typer.echo(f"Session tokens cached to {TOKEN_STORE}")
    return client
