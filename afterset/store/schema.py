"""Training store schema and initialization."""
from __future__ import annotations

from afterset import db, workouts
from afterset.connection import SCHEMA_VERSION, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_payloads (
 activity_id INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
 fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(activity_id,kind));
CREATE TABLE IF NOT EXISTS session_notes (
 note_id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL, note TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS set_revisions (
 revision_id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER NOT NULL,
 parent_revision INTEGER NOT NULL, sets_json TEXT, reason TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS set_heads (activity_id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS set_proposals (
 proposal_id TEXT PRIMARY KEY, activity_id INTEGER NOT NULL, base_revision INTEGER NOT NULL,
 sets_json TEXT NOT NULL, note_id TEXT NOT NULL, explanation TEXT NOT NULL,
 applied_revision INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 workout_json TEXT CHECK(workout_json IS NULL OR json_valid(workout_json)), source_hash TEXT);
CREATE TABLE IF NOT EXISTS sync_jobs (
 job_id TEXT PRIMARY KEY, state TEXT NOT NULL, request_json TEXT NOT NULL,
 progress_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS request_receipts (
 operation TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, PRIMARY KEY(operation, request_key));
CREATE TABLE IF NOT EXISTS proposal_reviews (
 proposal_id TEXT PRIMARY KEY, review_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposal_successors (
 proposal_id TEXT PRIMARY KEY, successor_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS live_raw_revisions (
 activity_id INTEGER NOT NULL, revision_id INTEGER NOT NULL,
 PRIMARY KEY(activity_id,revision_id));
CREATE TABLE IF NOT EXISTS activity_payload_outcomes (
 activity_id INTEGER NOT NULL, kind TEXT NOT NULL, backend TEXT NOT NULL,
 outcome TEXT NOT NULL, attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 fingerprint TEXT, error_type TEXT, PRIMARY KEY(activity_id,kind,backend));
"""


def initialize() -> None:
    with connect() as conn:
        conn.executescript('BEGIN IMMEDIATE;\n' + db.SCHEMA + SCHEMA + workouts.SCHEMA)
        if conn.execute('PRAGMA user_version').fetchone()[0] == 0:
            # Earlier writers treated every raw-mode head as live Garmin.
            conn.execute('INSERT OR IGNORE INTO live_raw_revisions(activity_id,revision_id) '
                         'SELECT activity_id,revision_id FROM set_revisions WHERE sets_json IS NULL')
        workouts.refresh_raw_snapshots(conn)
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
