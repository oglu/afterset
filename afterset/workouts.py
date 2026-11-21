"""Versioned workout documents and relational projections of their queryable fields.

The original Garmin payloads and legacy correction JSON remain untouched. Each
correction snapshot owns its recording segments so later syncs cannot rewrite it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


from .workout_models import *  # Re-export the established model API.


SCHEMA = """
CREATE TABLE IF NOT EXISTS workout_sessions (
 activity_id INTEGER PRIMARY KEY REFERENCES activities(activity_id));
CREATE TABLE IF NOT EXISTS workout_versions (
 version_id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL REFERENCES workout_sessions(activity_id),
 revision_id INTEGER NOT NULL CHECK(revision_id>=0), source TEXT NOT NULL,
 source_hash TEXT NOT NULL, document_json TEXT NOT NULL CHECK(json_valid(document_json)),
 summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
 UNIQUE(activity_id,revision_id));
CREATE TABLE IF NOT EXISTS workout_sources (
 version_id INTEGER NOT NULL REFERENCES workout_versions(version_id) ON DELETE CASCADE,
 activity_id INTEGER NOT NULL REFERENCES activities(activity_id), PRIMARY KEY(version_id,activity_id));
CREATE TABLE IF NOT EXISTS workout_blocks (
 block_id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES workout_versions(version_id) ON DELETE CASCADE,
 position INTEGER NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
 details_json TEXT NOT NULL CHECK(json_valid(details_json)), UNIQUE(version_id,position));
CREATE TABLE IF NOT EXISTS workout_efforts (
 effort_id INTEGER PRIMARY KEY, block_id INTEGER NOT NULL REFERENCES workout_blocks(block_id) ON DELETE CASCADE,
 position INTEGER NOT NULL, exercise TEXT NOT NULL, exercise_key TEXT, round INTEGER,
 kind TEXT NOT NULL, reps INTEGER CHECK(reps>=0), reps_basis TEXT NOT NULL,
 load_kg REAL CHECK(load_kg>=0), load_basis TEXT, implement_count INTEGER CHECK(implement_count>0),
 distance_meters REAL CHECK(distance_meters>=0), duration_seconds REAL CHECK(duration_seconds>=0),
 details_json TEXT NOT NULL CHECK(json_valid(details_json)), UNIQUE(block_id,position));
CREATE INDEX IF NOT EXISTS workout_efforts_exercise ON workout_efforts(exercise_key);
CREATE TABLE IF NOT EXISTS recording_segments (
 segment_id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES workout_versions(version_id) ON DELETE CASCADE,
 activity_id INTEGER NOT NULL REFERENCES activities(activity_id), source_index INTEGER NOT NULL,
 start_utc TEXT, duration_seconds REAL, kind TEXT,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), UNIQUE(version_id,activity_id,source_index));
CREATE TABLE IF NOT EXISTS effort_segments (
 effort_id INTEGER NOT NULL REFERENCES workout_efforts(effort_id) ON DELETE CASCADE,
 segment_id INTEGER NOT NULL REFERENCES recording_segments(segment_id) ON DELETE CASCADE,
 PRIMARY KEY(effort_id,segment_id));
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def recording(conn: sqlite3.Connection, activity_id: int) -> list[dict]:
    row = conn.execute('SELECT payload_json FROM activity_exercise_sets_raw WHERE activity_id=?', (activity_id,)).fetchone()
    if row is None:
        return []
    data = json.loads(row[0]).get('exerciseSets') or []
    if not isinstance(data, list) or not all(isinstance(s, dict) for s in data):
        raise ValueError('Unexpected Garmin exercise interval payload')
    return data


def source_hash(conn: sqlite3.Connection, activity_ids: list[int]) -> str:
    values = []
    for activity_id in sorted(activity_ids):
        activity = conn.execute('SELECT * FROM activities WHERE activity_id=?', (activity_id,)).fetchone()
        if activity is None:
            raise ValueError('Unknown source activity ID')
        raw = conn.execute('SELECT payload_json FROM activity_exercise_sets_raw WHERE activity_id=?', (activity_id,)).fetchone()
        values.append([dict(activity), raw[0] if raw else None])
    return hashlib.sha256(dumps(values).encode()).hexdigest()


def validate_sources(conn: sqlite3.Connection, activity_id: int, workout: Workout, *, legacy_format_notes: bool = False) -> None:
    if activity_id not in workout.source_activity_ids:
        raise ValueError('The primary activity must belong to source_activity_ids')
    available = {aid: recording(conn, aid) for aid in workout.source_activity_ids}
    source_hash(conn, workout.source_activity_ids)  # Also checks existence.
    usages: dict[tuple[int, int], set[bool]] = {}
    for block in workout.blocks:
        for note_id in block.evidence.note_ids:
            row = conn.execute('SELECT activity_id FROM session_notes WHERE note_id=?', (note_id,)).fetchone()
            if row is None or row[0] not in workout.source_activity_ids:
                raise ValueError('Evidence note must belong to a source activity')
        nested_evidence = []
        if block.format and not legacy_format_notes:
            nested_evidence.extend(block.format.provenance.values())
        if isinstance(block, BlockV3):
            nested_evidence.extend(group.evidence for group in block.drop_sets)
        for evidence in nested_evidence:
            for note_id in evidence.note_ids:
                row = conn.execute('SELECT activity_id FROM session_notes WHERE note_id=?', (note_id,)).fetchone()
                if row is None or row[0] not in workout.source_activity_ids:
                    raise ValueError('Evidence note must belong to a source activity')
        for effort in block.efforts:
            for ref in effort.segments:
                if ref.index >= len(available[ref.activity_id]):
                    raise ValueError('Recording segment index does not exist')
                usages.setdefault((ref.activity_id, ref.index), set()).add(effort.kind == 'excluded')
            for evidence in effort.provenance.values():
                for note_id in evidence.note_ids:
                    row = conn.execute('SELECT activity_id FROM session_notes WHERE note_id=?', (note_id,)).fetchone()
                    if row is None or row[0] not in workout.source_activity_ids:
                        raise ValueError('Evidence note must belong to a source activity')
    if any(len(kinds) > 1 for kinds in usages.values()):
        raise ValueError('A whole recording segment cannot be both excluded and included')


def raw_workout(activity_id: int, segments: list[dict]) -> Workout:
    efforts = []
    for index, segment in enumerate(segments):
        candidates = segment.get('exercises') or []
        candidate = candidates[0].get('name') if candidates and isinstance(candidates[0], dict) else None
        reps = segment.get('repetitionCount')
        duration = segment.get('duration')
        efforts.append(Effort(exercise=str(candidate or 'Unidentified recorded interval')[:200],
                              reps=reps if isinstance(reps, int) and reps >= 0 else None,
                              duration_seconds=duration if isinstance(duration, (int, float)) and duration >= 0 else None,
                              kind='rest' if segment.get('setType') == 'REST' else 'unknown',
                              segments=[SegmentRef(activity_id=activity_id, index=index)],
                              evidence='Original Garmin interval. Exercise candidates and detected counts are unconfirmed; raw load units/conventions are not interpreted.',
                              provenance={'recording': Evidence(source='watch', text='Original Garmin exercise interval; no user reconstruction.') }))
    blocks = [Block(label='Original Garmin recording', kind='unclassified', evidence=Evidence(source='watch', text='No reconstructed block or round structure.'), efforts=efforts)] if efforts else []
    return Workout(source_activity_ids=[activity_id], blocks=blocks,
                   warnings=[] if efforts else ['No exercise intervals available; the original activity summary remains available.'])


def legacy_sets(workout: Workout) -> list[dict]:
    """Conservative storage projection for legacy audit columns; never exposed as a tool view."""
    result = []
    for block in workout.blocks:
        for effort in block.efforts:
            weight = None
            if effort.load and effort.load.kg is not None:
                if effort.load.basis == 'total_external':
                    weight = effort.load.kg
                elif effort.load.basis == 'per_hand' and effort.load.implement_count is not None:
                    weight = effort.load.kg * effort.load.implement_count
                elif isinstance(effort.load, LoadV3):
                    weight = effort.load.total_external_kg()
            reps = effort.reps if effort.reps_basis == 'total' else None
            result.append({'exercise': effort.exercise, 'reps': reps, 'weight_kg': weight,
                           'duration_seconds': effort.duration_seconds,
                           'kind': effort.kind if effort.kind != 'excluded' else 'unknown',
                           'evidence': effort.evidence})
    return result


def _summary(conn: sqlite3.Connection, activity_id: int, workout: Workout, segments: dict[tuple[int, int], dict], source: str) -> dict:
    excluded = {(r.activity_id, r.index) for b in workout.blocks for e in b.efforts if e.kind == 'excluded' for r in e.segments}
    durations = [segments[key].get('duration') for key in excluded]
    known = all(isinstance(d, (int, float)) and d >= 0 for d in durations)
    excluded_seconds = sum(durations) if known else None
    row = conn.execute('SELECT duration_seconds FROM activities WHERE activity_id=?', (activity_id,)).fetchone()
    raw_duration = row[0] if row else None
    unlinked_exclusion = any(e.kind == 'excluded' and not e.segments for b in workout.blocks for e in b.efforts)
    effective = None
    if len(workout.source_activity_ids) == 1 and raw_duration is not None and known and not unlinked_exclusion:
        if raw_duration >= excluded_seconds:
            effective = raw_duration - excluded_seconds
    return {'raw_duration_seconds': raw_duration, 'excluded_duration_seconds': excluded_seconds,
            'effective_duration_seconds': effective,
            'working_efforts': None if source in ('garmin', 'garmin_restore') else sum(e.kind == 'working' for b in workout.blocks for e in b.efforts),
            'unreliable_raw_metrics': ['avg_hr', 'calories', 'training_load'] if excluded or unlinked_exclusion else [],
            'note': 'Effective duration subtracts explicitly excluded whole recording intervals only. It does not retime individual exercises or recalculate heart rate, calories, or training load.'}


def save_snapshot(conn: sqlite3.Connection, activity_id: int, revision: int, workout: Workout, source: str, *, replace_raw: bool = False, frozen_from: int | None = None, legacy_format_notes: bool = False) -> int:
    if frozen_from is None:
        validate_sources(conn, activity_id, workout, legacy_format_notes=legacy_format_notes)
        fingerprint = source_hash(conn, workout.source_activity_ids)
    else:
        frozen = conn.execute('SELECT source_hash,summary_json FROM workout_versions WHERE version_id=? AND activity_id=?', (frozen_from, activity_id)).fetchone()
        if frozen is None:
            raise ValueError('Unknown snapshot to restore')
        fingerprint = frozen['source_hash']
    existing = conn.execute('SELECT version_id,source_hash FROM workout_versions WHERE activity_id=? AND revision_id=?', (activity_id, revision)).fetchone()
    if existing:
        if not replace_raw or existing['source_hash'] == fingerprint:
            return existing['version_id']
        if revision != 0:
            raise ValueError('Correction snapshots are immutable')
        conn.execute('DELETE FROM workout_versions WHERE version_id=?', (existing['version_id'],))
    conn.execute('INSERT OR IGNORE INTO workout_sessions(activity_id) VALUES(?)', (activity_id,))
    if frozen_from is None:
        segments = {(aid, i): segment for aid in workout.source_activity_ids for i, segment in enumerate(recording(conn, aid))}
        summary = _summary(conn, activity_id, workout, segments, source)
    else:
        segments = {(r['activity_id'], r['source_index']): json.loads(r['payload_json']) for r in conn.execute('SELECT * FROM recording_segments WHERE version_id=?', (frozen_from,))}
        summary = json.loads(frozen['summary_json'])
    cursor = conn.execute('INSERT INTO workout_versions(activity_id,revision_id,source,source_hash,document_json,summary_json) VALUES(?,?,?,?,?,?)',
                          (activity_id, revision, source, fingerprint, dumps(workout.model_dump()), dumps(summary)))
    version = cursor.lastrowid
    for aid in workout.source_activity_ids:
        conn.execute('INSERT INTO workout_sources VALUES(?,?)', (version, aid))
    segment_ids = {}
    for (aid, index), segment in segments.items():
        cursor = conn.execute('INSERT INTO recording_segments(version_id,activity_id,source_index,start_utc,duration_seconds,kind,payload_json) VALUES(?,?,?,?,?,?,?)',
                              (version, aid, index, segment.get('startTime'), segment.get('duration'), segment.get('setType'), dumps(segment)))
        segment_ids[(aid, index)] = cursor.lastrowid
    for position, block in enumerate(workout.blocks):
        cursor = conn.execute('INSERT INTO workout_blocks(version_id,position,kind,label,details_json) VALUES(?,?,?,?,?)',
                              (version, position, block.kind, block.label, dumps(block.model_dump(exclude={'efforts'}))))
        block_id = cursor.lastrowid
        for n, effort in enumerate(block.efforts):
            load = effort.load
            cursor = conn.execute('INSERT INTO workout_efforts(block_id,position,exercise,exercise_key,round,kind,reps,reps_basis,load_kg,load_basis,implement_count,distance_meters,duration_seconds,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (block_id, n, effort.exercise, effort.exercise_key, effort.round, effort.kind, effort.reps, effort.reps_basis,
                                   load.kg if load else None, load.basis if load else None, load.implement_count if load else None,
                                   effort.distance_meters, effort.duration_seconds, dumps(effort.model_dump())))
            effort_id = cursor.lastrowid
            for ref in effort.segments:
                conn.execute('INSERT INTO effort_segments VALUES(?,?)', (effort_id, segment_ids[(ref.activity_id, ref.index)]))
    return version


def refresh_raw_snapshots(conn: sqlite3.Connection) -> None:
    """Rebuild revision-0 Garmin views whose recording changed. Runs in the caller's transaction."""
    for row in conn.execute('SELECT activity_id FROM activities').fetchall():
        aid = row['activity_id']
        existing = conn.execute('SELECT source_hash FROM workout_versions WHERE activity_id=? AND revision_id=0', (aid,)).fetchone()
        if existing is None or existing[0] != source_hash(conn, [aid]):
            save_snapshot(conn, aid, 0, raw_workout(aid, recording(conn, aid)), 'garmin', replace_raw=True)


def read_snapshot(conn: sqlite3.Connection, activity_id: int, revision: int, include_recording_payload: bool = False) -> dict:
    row = conn.execute('SELECT * FROM workout_versions WHERE activity_id=? AND revision_id=?', (activity_id, revision)).fetchone()
    if row is None:
        raise ValueError('Workout revision does not belong to this activity')
    segments = [dict(s) for s in conn.execute('SELECT activity_id,source_index,start_utc,duration_seconds,kind FROM recording_segments WHERE version_id=? ORDER BY activity_id,source_index', (row['version_id'],))]
    if include_recording_payload:
        payloads = {(r['activity_id'], r['source_index']): json.loads(r['payload_json']) for r in conn.execute('SELECT activity_id,source_index,payload_json FROM recording_segments WHERE version_id=?', (row['version_id'],))}
        for segment in segments:
            segment['payload'] = payloads[(segment['activity_id'], segment['source_index'])]
    unmapped = [dict(s) for s in conn.execute('SELECT r.activity_id,r.source_index FROM recording_segments r WHERE r.version_id=? AND NOT EXISTS(SELECT 1 FROM effort_segments e WHERE e.segment_id=r.segment_id) ORDER BY r.activity_id,r.source_index', (row['version_id'],))]
    source_ids = json.loads(row['document_json'])['source_activity_ids']
    return {'activity_id': activity_id, 'revision': revision, 'source': row['source'],
            'recording_changed_since_revision': source_hash(conn, source_ids) != row['source_hash'],
            'workout': json.loads(row['document_json']), 'summary': json.loads(row['summary_json']),
            'recording_segments': segments, 'unmapped_recording_segments': unmapped}


def current_snapshot(conn: sqlite3.Connection, activity_id: int, revision: int, include_recording_payload: bool = False) -> dict:
    # Raw snapshots are refreshed transactionally by sync writes and at startup.
    raw_mode = revision == 0 or conn.execute('SELECT 1 FROM set_revisions WHERE activity_id=? AND revision_id=? AND sets_json IS NULL', (activity_id, revision)).fetchone() is not None
    if raw_mode and (revision == 0 or conn.execute('SELECT 1 FROM live_raw_revisions WHERE activity_id=? AND revision_id=?', (activity_id, revision)).fetchone() is not None):
        result = read_snapshot(conn, activity_id, 0, include_recording_payload)
        result['revision'] = revision
        return result
    return read_snapshot(conn, activity_id, revision, include_recording_payload)
