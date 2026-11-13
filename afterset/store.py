"""Local query and append-only correction storage; no arbitrary SQL tool."""
from __future__ import annotations

from typing import Any

import json
import sqlite3
from datetime import date, timedelta
from typing import Literal
from uuid import uuid4

from afterset import db, workouts

from afterset.config import timezone_name

from afterset.connection import connect

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
"""


def initialize() -> None:
    with connect() as conn:
        conn.executescript('BEGIN IMMEDIATE;\n' + db.SCHEMA + SCHEMA + workouts.SCHEMA)
        workouts.refresh_raw_snapshots(conn)


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


ACTIVITY_SUMMARY_FIELDS = {
    'distance_meters': 'distance', 'average_speed_mps': 'averageSpeed',
    'elevation_gain_meters': 'elevationGain', 'moving_duration_seconds': 'movingDuration',
    'elapsed_duration_seconds': 'elapsedDuration',
}


def _activity_summary(conn: sqlite3.Connection, activity: dict) -> dict:
    raw = conn.execute("SELECT payload_json FROM activity_payloads WHERE activity_id=? AND kind='summary'", (activity['activity_id'],)).fetchone()
    summary = json.loads(raw[0]) if raw else {}
    return {**activity, **{key: summary.get(source) for key, source in ACTIVITY_SUMMARY_FIELDS.items()}}


def coverage() -> dict[str, Any]:
    with connect() as conn:
        result = {}
        for table in ('activities', 'daily_metrics', 'heart_rate_readings', 'hrv_readings', 'stress_readings'):
            result[table] = dict(conn.execute(f'SELECT count(*) AS rows,min(date) AS first_date,max(date) AS last_date FROM {table}').fetchone())
        result['exercise_sets'] = dict(conn.execute('SELECT count(*) AS activities,max(fetched_at) AS last_fetched FROM activity_exercise_sets_raw').fetchone())
        result['activity_payloads'] = [dict(r) for r in conn.execute('SELECT kind,count(*) AS activities FROM activity_payloads GROUP BY kind')]
    return {'coverage': result, 'timezone': timezone_name(), 'note': 'Date bounds do not imply complete coverage; absent/null metrics are unknown, not zero.'}


def list_activities(start: str, end: str, activity_type: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    date_range(start, end)
    page(limit, offset)
    where = 'date BETWEEN ? AND ?'
    args: list = [start, end]
    if activity_type:
        where += ' AND type=?'
        args.append(activity_type)
    with connect() as conn:
        total = conn.execute(f'SELECT count(*) FROM activities WHERE {where}', args).fetchone()[0]
        rows = conn.execute(f'SELECT * FROM activities WHERE {where} ORDER BY date DESC,start_gmt DESC,activity_id DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        items = []
        for row in rows:
            items.append(_activity_summary(conn, dict(row)))
    return {'items': items, 'total': total, 'next_offset': offset+limit if offset+limit < total else None}


def _session(conn: sqlite3.Connection, activity: dict) -> dict[str, Any]:
    activity_id = activity['activity_id']
    revision = head(conn, activity_id)
    structured = workouts.current_snapshot(conn, activity_id, revision)
    notes = [dict(r) for r in conn.execute('SELECT * FROM session_notes WHERE activity_id=? ORDER BY created_at,note_id', (activity_id,))]
    return {'activity': _activity_summary(conn, activity), 'revision': revision, 'source': structured['source'],
            'notes': notes, 'workout': structured['workout'],
            'effective_summary': structured['summary'],
            'recording_changed_since_revision': structured['recording_changed_since_revision']}


def get_activity(activity_id: int, include: list[Literal['raw', 'history', 'proposals']] | None = None) -> dict[str, Any]:
    """Read the effective session; large audit material is explicitly opt-in."""
    requested = set(include or [])
    if requested - {'raw', 'history', 'proposals'}:
        raise ValueError('include accepts raw, history and proposals')
    with connect() as conn:
        conn.execute('BEGIN')
        result = _session(conn, require_activity(conn, activity_id))
        result['workout'] = _compact_workout(result['workout'])
        result['workout_view'] = 'compact'
        result['available_payloads'] = [dict(r) for r in conn.execute('SELECT kind,fetched_at FROM activity_payloads WHERE activity_id=?', (activity_id,))]
        if 'raw' in requested:
            raw = conn.execute('SELECT payload_json FROM activity_exercise_sets_raw WHERE activity_id=?', (activity_id,)).fetchone()
            result['garmin_sets_response'] = json.loads(raw[0]) if raw else None
        if 'history' in requested:
            # Snapshot bodies are available from get_workout(revision=...).
            result['revision_history'] = [dict(r) for r in conn.execute('SELECT revision_id,parent_revision,reason,created_at FROM set_revisions WHERE activity_id=? ORDER BY revision_id', (activity_id,))]
        if 'proposals' in requested:
            proposals = [dict(r) for r in conn.execute('SELECT proposal_id,activity_id,base_revision,note_id,explanation,applied_revision,created_at,workout_json FROM set_proposals WHERE activity_id=? ORDER BY created_at,proposal_id', (activity_id,))]
            for proposal in proposals:
                document = proposal.pop('workout_json')
                proposal['workout'] = json.loads(document) if document else None
            result['proposals'] = proposals
    return result


def _compact_workout(document: dict) -> dict:
    """Analysis view: preserve measurement conventions and links, omit audit bodies."""
    def compact(value):
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: compact(item) for key, item in value.items()
                  if key not in ('details', 'evidence', 'provenance')}
        if isinstance(value.get('evidence'), dict):
            result['classification_source'] = value['evidence']['source']
        if value.get('provenance'):
            result['field_sources'] = {key: evidence['source'] for key, evidence in value['provenance'].items()}
        return result
    return compact(document)


WELLNESS_LINE = ('sleep_score', 'sleep_duration_seconds', 'sleep_hrv', 'sleep_spo2',
                 'resting_hr', 'stress_avg', 'body_battery_start', 'body_battery_end')


def training_summary(start: str, end: str) -> dict[str, Any]:
    """Read all effective sessions, notes and daily recovery over at most 31 days."""
    a, b = date_range(start, end, 31)
    if b == date.max:
        raise ValueError('end must allow a following morning')
    following = (b + timedelta(days=1)).isoformat()
    with connect() as conn:
        conn.execute('BEGIN')
        rows = conn.execute('SELECT * FROM activities WHERE date BETWEEN ? AND ? ORDER BY date,start_gmt,activity_id', (start, end)).fetchall()
        if len(rows) > 500:
            raise ValueError('More than 500 activities; request a shorter date range')
        sessions = []
        for row in rows:
            session = _session(conn, dict(row))
            session['workout'] = _compact_workout(session['workout'])
            session['workout_view'] = 'compact'
            # Keep the range response focused on interpretation rather than HR-zone detail.
            session['activity'] = {key: session['activity'][key] for key in
                                   ('activity_id', 'date', 'name', 'type', 'start_gmt', 'duration_seconds', 'avg_hr', 'max_hr', 'calories', 'training_load', *ACTIVITY_SUMMARY_FIELDS)}
            sessions.append(session)
        wellness = {row['date']: dict(row) for row in conn.execute(
            'SELECT date,' + ','.join(WELLNESS_LINE) + ' FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date', (start, following))}
    def line(day):
        return {'date': day, 'recorded': day in wellness,
                **{key: wellness.get(day, {}).get(key) for key in WELLNESS_LINE}}
    days = [line((a + timedelta(days=i)).isoformat()) for i in range((b-a).days+1)]
    return {'start': start, 'end': end, 'activities': sessions, 'days': days,
            'following_morning': line(following), 'timezone': timezone_name(),
            'note': 'Compact analysis view; get_workout provides full provenance and recording payloads. Sleep uses wake-up date: compare training with the next calendar day, including following_morning for the final day. Null is unknown. Shared source_activity_ids may overlap; do not double-count durations.'}


def get_payload(activity_id: int, kind: Literal['summary', 'detail', 'splits', 'samples'], offset: int = 0, limit: int = 200) -> dict[str, Any]:
    page(limit, offset)
    with connect() as conn:
        require_activity(conn, activity_id)
        row = conn.execute('SELECT * FROM activity_payloads WHERE activity_id=? AND kind=?', (activity_id, kind)).fetchone()
    if row is None:
        return {'available': False, 'hint': 'Run activity sync with enrich=True for this date.'}
    payload = json.loads(row['payload_json'])
    # Garmin samples can be large. Page each top-level array, retaining descriptors.
    result, totals = {}, {}
    for key, value in payload.items():
        if isinstance(value, list) and 'descriptor' not in key.lower():
            totals[key] = len(value)
            result[key] = value[offset:offset+limit]
        else:
            result[key] = value
    return {'available': True, 'payload': result, 'array_totals': totals,
            'next_offset': offset+limit if any(n > offset+limit for n in totals.values()) else None,
            'fetched_at': row['fetched_at']}


def daily_metrics(start: str, end: str) -> dict[str, Any]:
    a, b = date_range(start, end)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM daily_metrics WHERE date BETWEEN ? AND ? ORDER BY date', (start, end))]
    return {'items': rows, 'expected_days': (b-a).days+1, 'recorded_days': len(rows), 'note': 'Sleep is associated with Garmin wake-up date; null means unavailable.'}


def timeseries(start: str, end: str, metric: Literal['heart_rate', 'hrv', 'stress', 'sleep_stages'], limit: int = 500, offset: int = 0) -> dict[str, Any]:
    date_range(start, end, 31)
    page(limit, offset)
    table = {'heart_rate':'heart_rate_readings', 'hrv':'hrv_readings', 'stress':'stress_readings', 'sleep_stages':'sleep_stages'}[metric]
    order = 'date,start_gmt' if metric == 'sleep_stages' else 'timestamp_gmt'
    with connect() as conn:
        total = conn.execute(f'SELECT count(*) FROM {table} WHERE date BETWEEN ? AND ?', (start, end)).fetchone()[0]
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE date BETWEEN ? AND ? ORDER BY {order} LIMIT ? OFFSET ?', (start, end, limit, offset))]
    return {'items': rows, 'total': total, 'next_offset': offset+limit if offset+limit < total else None, 'timestamp_timezone': 'UTC'}


def save_note(activity_id: int, note: str) -> dict[str, Any]:
    if not note.strip() or len(note) > 20000:
        raise ValueError('Provide a nonempty note of at most 20000 characters')
    note_id = str(uuid4())
    with connect() as conn:
        require_activity(conn, activity_id)
        conn.execute('INSERT INTO session_notes(note_id,activity_id,note) VALUES(?,?,?)', (note_id, activity_id, note))
    return {'note_id': note_id, 'activity_id': activity_id}


def _revision(conn: sqlite3.Connection, activity_id: int, parent: int, payload: str | None, reason: str, workout: workouts.Workout | None = None, frozen_from: int | None = None) -> int:
    cursor = conn.execute('INSERT INTO set_revisions(activity_id,parent_revision,sets_json,reason) VALUES(?,?,?,?)', (activity_id, parent, payload, reason))
    revision = cursor.lastrowid
    if workout is None:
        if payload is not None:
            raise ValueError('A correction revision requires its structured workout')
        workout = workouts.raw_workout(activity_id, workouts.recording(conn, activity_id))
    workouts.save_snapshot(conn, activity_id, revision, workout, 'structured_correction' if payload is not None else 'garmin_restore', frozen_from=frozen_from)
    conn.execute('INSERT INTO set_heads VALUES(?,?) ON CONFLICT(activity_id) DO UPDATE SET revision_id=excluded.revision_id', (activity_id, revision))
    return revision


def apply_proposal(proposal_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not proposal:
            raise ValueError('Unknown proposal')
        if proposal['applied_revision'] is not None:
            return {'revision': proposal['applied_revision'], 'already_applied': True}
        if head(conn, proposal['activity_id']) != proposal['base_revision']:
            raise ValueError('Stale proposal; reload the session and propose again')
        workout = workouts.Workout.model_validate_json(proposal['workout_json'])
        if proposal['source_hash'] != workouts.source_hash(conn, workout.source_activity_ids):
            raise ValueError('Recording changed since this proposal; reload and propose again')
        revision = _revision(conn, proposal['activity_id'], proposal['base_revision'], proposal['sets_json'], f"Proposal {proposal_id}: {proposal['explanation']}", workout)
        conn.execute('UPDATE set_proposals SET applied_revision=? WHERE proposal_id=?', (revision, proposal_id))
    return {'revision': revision, 'activity_id': proposal['activity_id']}


def restore_sets(activity_id: int, target_revision: int, expected_revision: int, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError('A reason is required')
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_activity(conn, activity_id)
        current = head(conn, activity_id)
        if current != expected_revision:
            raise ValueError('Revision changed; reload before restoring')
        payload = None
        workout = None
        frozen_from = None
        if target_revision != 0:
            row = conn.execute('SELECT sets_json FROM set_revisions WHERE activity_id=? AND revision_id=?', (activity_id, target_revision)).fetchone()
            if row is None:
                raise ValueError('Revision does not belong to this activity')
            payload = row[0]
            if payload is not None:
                snapshot = conn.execute('SELECT version_id,document_json FROM workout_versions WHERE activity_id=? AND revision_id=?', (activity_id, target_revision)).fetchone()
                workout = workouts.Workout.model_validate_json(snapshot['document_json'])
                frozen_from = snapshot['version_id']
        revision = _revision(conn, activity_id, current, payload, f'Restore {target_revision}: {reason}', workout, frozen_from)
    return {'revision': revision, 'activity_id': activity_id, 'restored_revision': target_revision}


def store_payload(activity_id: int, kind: str, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f'Unexpected {kind} response')
    with connect() as conn:
        conn.execute('INSERT INTO activity_payloads(activity_id,kind,payload_json) VALUES(?,?,?) ON CONFLICT(activity_id,kind) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=CURRENT_TIMESTAMP', (activity_id, kind, json.dumps(payload, allow_nan=False)))


def get_workout(activity_id: int, revision: int | None = None, include_recording_payload: bool = False) -> dict[str, Any]:
    """Read structured blocks/efforts and their original recording links."""
    with connect() as conn:
        conn.execute('BEGIN')
        require_activity(conn, activity_id)
        if revision is None:
            return workouts.current_snapshot(conn, activity_id, head(conn, activity_id), include_recording_payload)
        return workouts.read_snapshot(conn, activity_id, revision, include_recording_payload)


def propose_workout(activity_id: int, note_id: str, workout: workouts.Workout, explanation: str, expected_revision: int) -> dict[str, Any]:
    """Draft a complete structured replacement using the existing review workflow."""
    workout = workouts.Workout.model_validate(workout)
    if not explanation.strip() or len(explanation) > 20000:
        raise ValueError('Provide a nonempty explanation of at most 20000 characters')
    proposal_id = str(uuid4())
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        require_activity(conn, activity_id)
        if head(conn, activity_id) != expected_revision:
            raise ValueError('Revision changed; reload the session and propose again')
        if not conn.execute('SELECT 1 FROM session_notes WHERE note_id=? AND activity_id=?', (note_id, activity_id)).fetchone():
            raise ValueError('Note does not belong to this activity')
        workouts.validate_sources(conn, activity_id, workout)
        payload = workouts.dumps(workouts.legacy_sets(workout))
        document = workouts.dumps(workout.model_dump())
        conn.execute('INSERT INTO set_proposals(proposal_id,activity_id,base_revision,sets_json,note_id,explanation,workout_json,source_hash) VALUES(?,?,?,?,?,?,?,?)',
                     (proposal_id, activity_id, expected_revision, payload, note_id, explanation, document, workouts.source_hash(conn, workout.source_activity_ids)))
    return {'proposal_id': proposal_id, 'activity_id': activity_id, 'base_revision': expected_revision,
            'workout': workout.model_dump(), 'explanation': explanation, 'status': 'draft',
            'apply_tool': 'apply_proposal'}
