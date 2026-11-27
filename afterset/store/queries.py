"""Training store queries and compact projections."""
from __future__ import annotations

from datetime import date, timedelta
import json
import math
import sqlite3
from typing import Any, Literal

from afterset import workouts
from afterset.config import timezone_name
from afterset.connection import connect
from ._shared import date_range, head, page, require_activity


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
        details = value.get('details')
        if isinstance(details, dict):
            conditions = {key: item for key, item in details.items()
                          if key in ('variant', 'equipment', 'back_support', 'failure',
                                     'pause', 'pauses', 'range_of_motion')
                          and (isinstance(item, bool) or item is None
                               or isinstance(item, str) and len(item) <= 500
                               or isinstance(item, int) and item.bit_length() <= 128
                               or isinstance(item, float) and math.isfinite(item))}
            if conditions:
                result['comparison_conditions'] = conditions
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


def get_workout(activity_id: int, revision: int | None = None, include_recording_payload: bool = False,
                target_schema_version: Literal[3] | None = None) -> dict[str, Any]:
    """Read structured blocks/efforts and their original recording links."""
    if target_schema_version not in (None, 3):
        raise ValueError('target_schema_version accepts 3 only')
    with connect() as conn:
        conn.execute('BEGIN')
        require_activity(conn, activity_id)
        if revision is None:
            result = workouts.current_snapshot(conn, activity_id, head(conn, activity_id), include_recording_payload)
        else:
            result = workouts.read_snapshot(conn, activity_id, revision, include_recording_payload)
        if target_schema_version == 3:
            original_version = result['workout'].get('schema_version', 2)
            result['workout'] = workouts.convert_workout(
                result['workout'], snapshot_scope=f"{activity_id}:{result['revision']}"
            ).model_dump()
            result['original_schema_version'] = original_version
            result['converted_for_review'] = original_version != 3
        return result
