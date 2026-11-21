"""Original activity payload storage and retrieval."""
from __future__ import annotations

import json
import hashlib
from typing import Any, Literal

from afterset.connection import connect
from ._shared import page, require_activity


def get_payload(activity_id: int, kind: Literal['summary', 'detail', 'splits', 'samples'], offset: int = 0, limit: int = 200, expected_fingerprint: str | None = None) -> dict[str, Any]:
    page(limit, offset)
    with connect() as conn:
        require_activity(conn, activity_id)
        row = conn.execute('SELECT * FROM activity_payloads WHERE activity_id=? AND kind=?', (activity_id, kind)).fetchone()
    if row is None:
        return {'available': False, 'hint': 'Run activity sync with enrich=True for this date.'}
    payload = json.loads(row['payload_json'])
    digest = fingerprint(payload)
    if expected_fingerprint is not None and expected_fingerprint != digest:
        raise ValueError('Payload changed during pagination; restart the read')
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
            'fetched_at': row['fetched_at'], 'fingerprint': digest}


def store_payload(activity_id: int, kind: str, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f'Unexpected {kind} response')
    with connect() as conn:
        conn.execute('INSERT INTO activity_payloads(activity_id,kind,payload_json) VALUES(?,?,?) ON CONFLICT(activity_id,kind) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=CURRENT_TIMESTAMP', (activity_id, kind, json.dumps(payload, allow_nan=False)))


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def record_outcome(activity_id: int, kind: str, backend: str, outcome: str, digest: str | None = None, error_type: str | None = None) -> None:
    with connect() as conn:
        conn.execute("""INSERT INTO activity_payload_outcomes(activity_id,kind,backend,outcome,fingerprint,error_type) VALUES(?,?,?,?,?,?)
            ON CONFLICT(activity_id,kind,backend) DO UPDATE SET outcome=excluded.outcome, attempted_at=CURRENT_TIMESTAMP, fingerprint=excluded.fingerprint, error_type=excluded.error_type""",
            (activity_id, kind, backend, outcome, digest, error_type))


def enrichment_status(activity_id: int) -> dict[str, Any]:
    with connect() as conn:
        conn.execute('BEGIN')
        require_activity(conn, activity_id)
        outcomes = [dict(r) for r in conn.execute('SELECT * FROM activity_payload_outcomes WHERE activity_id=? ORDER BY kind,backend', (activity_id,))]
        rows = conn.execute('SELECT kind,payload_json,fetched_at FROM activity_payloads WHERE activity_id=?', (activity_id,)).fetchall()
        retained = [{'kind': r['kind'], 'fetched_at': r['fetched_at'], 'fingerprint': fingerprint(json.loads(r['payload_json']))} for r in rows]
        sets = conn.execute('SELECT payload_json,fetched_at FROM activity_exercise_sets_raw WHERE activity_id=?', (activity_id,)).fetchone()
        if sets:
            retained.append({'kind': 'sets', 'fetched_at': sets['fetched_at'], 'fingerprint': fingerprint(json.loads(sets['payload_json']))})
    return {'activity_id': activity_id, 'stored': retained, 'latest_outcomes': outcomes,
            'capabilities': {'connect': ['sets','detail','splits','samples'], 'personal-api': ['detail']},
            'note': 'Absent outcomes have unknown fetch history. Stored data may predate or outlive the latest attempt.'}


def save_fetched(activity_id: int, kind: str, backend: str, payload: dict) -> None:
    """Atomically retain a validated payload and its successful fetch outcome."""
    from afterset import db
    if not isinstance(payload, dict):
        raise ValueError('Unexpected response')
    if 'activityId' in payload and payload['activityId'] != activity_id:
        raise ValueError('Response activity does not match request')
    if kind == 'sets':
        valid = isinstance(payload.get('exerciseSets'), list)
    elif kind == 'samples':
        descriptors = payload.get('metricDescriptors')
        rows = payload.get('activityDetailMetrics')
        valid = (isinstance(descriptors, list) and all(isinstance(d, dict) for d in descriptors)
                 and isinstance(rows, list) and all(isinstance(r, dict) and isinstance(r.get('metrics'), list) for r in rows))
    elif kind == 'splits':
        valid = isinstance(payload.get('lapDTOs'), list)
    elif kind == 'detail':
        valid = payload.get('activityId') == activity_id or isinstance(payload.get('summaryDTO'), dict)
    else:
        valid = False
    if not valid:
        raise ValueError('Unexpected response envelope')
    encoded = json.dumps(payload, allow_nan=False)
    digest = fingerprint(payload)
    with connect() as conn:
        require_activity(conn, activity_id)
        if kind == 'sets':
            conn.execute('INSERT INTO activity_exercise_sets_raw(activity_id,payload_json) VALUES(?,?) ON CONFLICT(activity_id) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=CURRENT_TIMESTAMP', (activity_id, encoded))
            db._refresh_workout(conn, activity_id)
        else:
            conn.execute('INSERT INTO activity_payloads(activity_id,kind,payload_json) VALUES(?,?,?) ON CONFLICT(activity_id,kind) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=CURRENT_TIMESTAMP', (activity_id, kind, encoded))
        conn.execute('''INSERT INTO activity_payload_outcomes(activity_id,kind,backend,outcome,fingerprint) VALUES(?,?,?,?,?)
            ON CONFLICT(activity_id,kind,backend) DO UPDATE SET outcome=excluded.outcome,attempted_at=CURRENT_TIMESTAMP,fingerprint=excluded.fingerprint,error_type=NULL''', (activity_id, kind, backend, 'success', digest))
