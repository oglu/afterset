"""Shared SQLite path and connection policy for CLI, sync and MCP storage."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3

from afterset.config import database_path

DB_PATH = database_path()


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            yield conn
    finally:
        conn.close()
