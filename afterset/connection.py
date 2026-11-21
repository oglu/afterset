"""Shared SQLite path and connection policy for CLI, sync and MCP storage."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3

from afterset.config import database_path

DB_PATH = database_path()
SCHEMA_VERSION = 1


_WRITE_ACTIONS = {getattr(sqlite3, name) for name in (
    'SQLITE_INSERT', 'SQLITE_UPDATE', 'SQLITE_DELETE', 'SQLITE_CREATE_INDEX',
    'SQLITE_CREATE_TABLE', 'SQLITE_CREATE_TEMP_INDEX', 'SQLITE_CREATE_TEMP_TABLE',
    'SQLITE_CREATE_TEMP_TRIGGER', 'SQLITE_CREATE_TEMP_VIEW', 'SQLITE_CREATE_TRIGGER',
    'SQLITE_CREATE_VIEW', 'SQLITE_DROP_INDEX', 'SQLITE_DROP_TABLE', 'SQLITE_DROP_TEMP_INDEX',
    'SQLITE_DROP_TEMP_TABLE', 'SQLITE_DROP_TEMP_TRIGGER', 'SQLITE_DROP_TEMP_VIEW',
    'SQLITE_DROP_TRIGGER', 'SQLITE_DROP_VIEW', 'SQLITE_ALTER_TABLE', 'SQLITE_REINDEX',
    'SQLITE_CREATE_VTABLE', 'SQLITE_DROP_VTABLE', 'SQLITE_ANALYZE',
)}


class TrainingCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        try:
            return super().execute(sql, parameters)
        except sqlite3.DatabaseError:
            self.connection._raise_guard_error()
            raise

    def executemany(self, sql, parameters, /):
        try:
            return super().executemany(sql, parameters)
        except sqlite3.DatabaseError:
            self.connection._raise_guard_error()
            raise

    def executescript(self, script, /):
        return self.connection.executescript(script)


class TrainingConnection(sqlite3.Connection):
    """Use SQLite's authorizer to check all writes under the writer lock."""

    def install_guard(self):
        self._guard_error = None
        self._checking_guard = False
        self.set_authorizer(self._authorize)

    def _authorize(self, action, arg1, arg2, database, origin):
        if self._checking_guard:
            return sqlite3.SQLITE_OK
        write_pragma = action == sqlite3.SQLITE_PRAGMA and arg2 is not None
        if action in _WRITE_ACTIONS or write_pragma:
            self._checking_guard = True
            try:
                if not self.in_transaction:
                    super().execute('BEGIN IMMEDIATE')
                version = super().execute('PRAGMA user_version').fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise ValueError(f'Database schema version {version} is newer than supported version {SCHEMA_VERSION}')
            except Exception as exc:
                self._guard_error = exc
                return sqlite3.SQLITE_DENY
            finally:
                self._checking_guard = False
        return sqlite3.SQLITE_OK

    def _raise_guard_error(self):
        error, self._guard_error = self._guard_error, None
        if error is not None:
            raise error

    def cursor(self, factory=TrainingCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script, /):
        # sqlite3.executescript commits first; run complete statements instead so
        # DDL and version checks stay in the caller's transaction.
        explicit = script.lstrip().upper().startswith('BEGIN ')
        started = not explicit and not self.in_transaction
        if started:
            super().execute('BEGIN IMMEDIATE')
        statement = ''
        for char in script:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                self.execute(statement)
                statement = ''
        if statement.strip():
            self.execute(statement)
        if started:
            self.commit()
        return self.cursor()


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=TrainingConnection, cached_statements=0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.install_guard()
        with conn:
            yield conn
    finally:
        conn.close()
