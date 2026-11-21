"""Validation and lookups shared by training store operations."""
from __future__ import annotations

from datetime import date
import sqlite3
from typing import Any


def date_range(start: str, end: str, max_days: int = 366) -> tuple[date, date]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a.isoformat() != start or b.isoformat() != end:
        raise ValueError('Dates must use YYYY-MM-DD')
    if a > b or (b-a).days >= max_days:
        raise ValueError(f'Use an ordered date range of at most {max_days} days')
    return a, b


def page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError('limit must be 1..500 and offset must be nonnegative')


def require_activity(conn: sqlite3.Connection, activity_id: int) -> dict[str, Any]:
    row = conn.execute('SELECT * FROM activities WHERE activity_id=?', (activity_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown activity ID')
    return dict(row)


def head(conn: sqlite3.Connection, activity_id: int) -> int:
    row = conn.execute('SELECT revision_id FROM set_heads WHERE activity_id=?', (activity_id,)).fetchone()
    return row[0] if row else 0
