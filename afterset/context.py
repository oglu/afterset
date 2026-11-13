"""Optional personal timeline. Training storage never opens this database."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date as Date
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from typing import Any

from afterset import connection
from afterset.config import personal_context_path

SCHEMA = '''CREATE TABLE IF NOT EXISTS personal_events (
 event_id TEXT PRIMARY KEY, date TEXT NOT NULL, end_date TEXT,
 category TEXT NOT NULL, label TEXT NOT NULL, notes TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);'''
FIELDS = ('date', 'end_date', 'category', 'label', 'notes')


def _path() -> Path:
    path = personal_context_path().resolve()
    training = connection.DB_PATH.resolve()
    if path == training or (path.exists() and training.exists() and path.samefile(training)):
        raise ValueError('Personal context must use a separate database from training')
    return path


@contextmanager
def _connect(*, write: bool = False):
    path = _path()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    elif not path.exists():
        yield None
        return
    conn = sqlite3.connect(path if write else path.as_uri() + '?mode=ro', uri=not write, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            conn.execute(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _event(date: str, end_date: str | None, category: str, label: str, notes: str) -> dict[str, Any]:
    values = dict(zip(FIELDS, (date, end_date, category, label, notes)))
    for key in ('date', 'category', 'label', 'notes'):
        if not isinstance(values[key], str) or not values[key].strip():
            raise ValueError(f'{key} must be a nonempty string')
    for value in (date, end_date):
        if value is not None and (not isinstance(value, str) or Date.fromisoformat(value).isoformat() != value):
            raise ValueError('Dates must use YYYY-MM-DD')
    if end_date is not None and end_date < date:
        raise ValueError('end_date must be on or after date')
    if len(notes) > 20000 or len(label) > 500 or len(category) > 100:
        raise ValueError('Personal context exceeds field limits')
    return values


def save_personal_context(date: str, category: str, label: str, notes: str, end_date: str | None = None) -> dict[str, Any]:
    """Save reported illness, life events or explanations, preserving uncertainty."""
    event = _event(date, end_date, category, label, notes)
    event_id = str(uuid4())
    with _connect(write=True) as conn:
        conn.execute('INSERT INTO personal_events (event_id,date,end_date,category,label,notes) VALUES (?,?,?,?,?,?)',
                     (event_id, *(event[field] for field in FIELDS)))
    return {'event_id': event_id, **event}


def get_personal_context(start: str, end: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """Explicit analysis-only read of events overlapping an inclusive date range."""
    a, b = Date.fromisoformat(start), Date.fromisoformat(end)
    if a.isoformat() != start or b.isoformat() != end or a > b or (b-a).days >= 366:
        raise ValueError('Use YYYY-MM-DD and an ordered date range of at most 366 days')
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError('limit must be 1..500 and offset must be nonnegative')
    with _connect() as conn:
        rows = [] if conn is None else conn.execute(
            '''SELECT * FROM personal_events WHERE date <= ? AND COALESCE(end_date,date) >= ?
               ORDER BY date,event_id LIMIT ? OFFSET ?''', (end, start, limit + 1, offset)).fetchall()
    return {'start': start, 'end': end, 'events': [dict(row) for row in rows[:limit]],
            'next_offset': offset + limit if len(rows) > limit else None}
