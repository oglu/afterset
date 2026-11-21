"""Bounded effective workout history and two-session comparisons."""
from __future__ import annotations

from typing import Annotated, Any
from pydantic import Field

from afterset import comparisons, workouts
from afterset.connection import connect
from ._shared import date_range, head, page, require_activity


def _effective(conn, activity_id):
    revision = head(conn, activity_id)
    snapshot = workouts.current_snapshot(conn, activity_id, revision)
    row = conn.execute('SELECT source_hash FROM workout_versions WHERE activity_id=? AND revision_id=?',
                       (activity_id, 0 if snapshot['source'] == 'garmin' else revision)).fetchone()
    return snapshot, row[0] if row else None


def _notes(conn, activity_id):
    rows = conn.execute('SELECT note,created_at FROM session_notes WHERE activity_id=? ORDER BY created_at,note_id LIMIT 6', (activity_id,)).fetchall()
    return {'items': [{'text': row['note'][:500], 'created_at': row['created_at'], 'truncated': len(row['note']) > 500} for row in rows[:5]], 'truncated': len(rows) > 5}


def get_exercise_history(exercise_key: str, start: str, end: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    date_range(start, end)
    page(limit, offset)
    key = comparisons.exercise_key(exercise_key)
    if not key or len(key) > 200:
        raise ValueError('Provide an exercise key of at most 200 characters')
    with connect() as conn:
        conn.execute('BEGIN')
        activities = conn.execute('SELECT activity_id,date,name FROM activities WHERE date BETWEEN ? AND ? ORDER BY date DESC,start_gmt DESC,activity_id DESC LIMIT 501', (start, end)).fetchall()
        if len(activities) > 500:
            raise ValueError('More than 500 activities; request a shorter date range')
        items = []
        sessions = {}
        for activity in activities:
            snapshot, fingerprint = _effective(conn, activity['activity_id'])
            original_version = snapshot['workout'].get('schema_version', 2)
            document = workouts.convert_workout(snapshot['workout'], snapshot_scope=f"{activity['activity_id']}:{snapshot['revision']}").model_dump(mode='json') if original_version == 2 else snapshot['workout']
            for bi, block in enumerate(document['blocks']):
                for ei, effort in enumerate(block['efforts']):
                    if effort.get('exercise_key') and comparisons.exercise_key(effort['exercise_key']) == key and effort.get('kind') != 'excluded':
                        groups = [g for g in block.get('drop_sets', []) if effort.get('id') in g['effort_ids']]
                        items.append({'activity_id': activity['activity_id'], 'date': activity['date'], 'name': activity['name'],
                                      'revision': snapshot['revision'], 'source_hash': fingerprint, 'schema_version': original_version,
                                      'source_activity_ids': snapshot['workout']['source_activity_ids'],
                                      'block_position': bi, 'effort_position': ei, 'effort': comparisons.present_effort(effort),
                                      'drop_sets': [{'id': g['id'], 'effort_ids': g['effort_ids']} for g in groups]})
                        if activity['activity_id'] not in sessions:
                            sessions[activity['activity_id']] = {'notes': _notes(conn, activity['activity_id'])}
        total = len(items)
        selected = items[offset:offset+limit]
        selected_ids = {item['activity_id'] for item in selected}
        sources = {}
        for item in items:
            for source in item['source_activity_ids']:
                sources.setdefault(source, set()).add(item['activity_id'])
        shared = sorted(source for source, owners in sources.items() if len(owners) > 1)
        return {'items': selected, 'sessions': {aid: sessions[aid] for aid in selected_ids}, 'shared_source_activity_ids': shared, 'shared_source_scope': 'all matching sessions in the requested range', 'total': total,
                'next_offset': offset+limit if offset+limit < total else None,
                'note': 'Sessions sharing source activities may overlap; do not add their durations.'}


def compare_workouts(first_activity_id: int, second_activity_id: int,
                     mappings: Annotated[list[comparisons.EffortMapping] | None, Field(max_length=2000)] = None) -> dict[str, Any]:
    if first_activity_id == second_activity_id:
        raise ValueError('Choose two different activities')
    with connect() as conn:
        conn.execute('BEGIN')
        first_activity = require_activity(conn, first_activity_id)
        second_activity = require_activity(conn, second_activity_id)
        first, first_hash = _effective(conn, first_activity_id)
        second, second_hash = _effective(conn, second_activity_id)
        result = comparisons.compare_documents(first['workout'], second['workout'], mappings,
                                               first_scope=f"{first_activity_id}:{first['revision']}",
                                               second_scope=f"{second_activity_id}:{second['revision']}")
        for name, activity, snapshot, fingerprint in [('first', first_activity, first, first_hash), ('second', second_activity, second, second_hash)]:
            notes = _notes(conn, activity['activity_id'])
            result[name] = {'activity_id': activity['activity_id'], 'date': activity['date'], 'name': activity['name'],
                            'revision': snapshot['revision'], 'source_hash': fingerprint, 'notes': notes}
    return result
