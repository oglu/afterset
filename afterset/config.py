"""Shared storage and calendar settings for CLI and MCP processes."""
from __future__ import annotations

import os
import tomllib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_config_path, user_data_path


def database_path() -> Path:
    configured = os.environ.get("AFTERSET_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    settings_path = user_config_path("afterset", appauthor=False) / "config.toml"
    if settings_path.is_file():
        settings = tomllib.loads(settings_path.read_text(encoding="utf-8"))
        if "database" in settings:
            database = settings["database"]
            if not isinstance(database, str) or not database.strip():
                raise ValueError(f"database in {settings_path} must be a nonempty absolute path")
            path = Path(database).expanduser()
            if not path.is_absolute():
                raise ValueError(f"database in {settings_path} must be an absolute path")
            return path.resolve()
    return user_data_path("afterset", appauthor=False) / "afterset.db"


def personal_context_path() -> Path:
    configured = os.environ.get("AFTERSET_CONTEXT_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    settings_path = user_config_path("afterset", appauthor=False) / "config.toml"
    if settings_path.is_file():
        settings = tomllib.loads(settings_path.read_text(encoding="utf-8"))
        if "personal_context_database" in settings:
            value = settings["personal_context_database"]
            if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
                raise ValueError("personal_context_database must be a nonempty absolute path")
            return Path(value).expanduser().resolve()
    return database_path().with_name("personal-context.db")


def timezone_name() -> str | None:
    name = os.environ.get("AFTERSET_TIMEZONE")
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("AFTERSET_TIMEZONE must be an IANA timezone, such as Europe/London") from None
    return name


def today() -> date:
    name = timezone_name()
    return datetime.now(ZoneInfo(name)).date() if name else date.today()


def token_store() -> Path:
    return Path(os.environ.get("AFTERSET_TOKEN_STORE", Path.home() / ".garmin_tokens")).expanduser().resolve()
