"""Session notes and append-only workout corrections."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from afterset import workouts
from afterset.connection import connect
from ._shared import head, page, require_activity


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt(conn: sqlite3.Connection, operation: str, key: str | None, fingerprint: str) -> dict | None:
    if key is None:
        return None
    if not key.strip() or len(key) > 200:
        raise ValueError('Request key must be nonempty and at most 200 characters')
    row = conn.execute('SELECT fingerprint,result_json FROM request_receipts WHERE operation=? AND request_key=?', (operation, key)).fetchone()
    if row is None:
        return None
    if row['fingerprint'] != fingerprint:
        raise ValueError('Request key was already used with different input')
    return json.loads(row['result_json'])


def _save_receipt(conn: sqlite3.Connection, operation: str, key: str | None, fingerprint: str, result: dict) -> None:
    if key is not None:
        conn.execute('INSERT INTO request_receipts(operation,request_key,fingerprint,result_json) VALUES(?,?,?,?)',
                     (operation, key, fingerprint, json.dumps(result, allow_nan=False)))


def _review_changes(conn: sqlite3.Connection, before: dict, after: dict, note_id: str, explanation: str) -> dict[str, Any]:
    """Expose bounded value changes and recorded conflicts without inventing missing fields."""
    changes = []
    count = 0
    def walk(old, new, path):
        nonlocal count
        if old == new:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                if key in ('provenance', 'evidence'):
                    continue
                walk(old.get(key), new.get(key), path + [key])
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (a, b) in enumerate(zip(old, new)):
                walk(a, b, path + [index])
        else:
            count += 1
            if len(changes) < 100:
                changes.append({'path': path, 'before': old, 'after': new})
    walk(before, after, [])
    conflicts = list(after.get('conflicts', []))
    unresolved = [{'kind': 'document_conflict', 'conflict': item} for item in conflicts
                  if item.get('status') == 'unresolved']
    unresolved_efforts = set()
    uses = {}
    for bi, block in enumerate(after.get('blocks', [])):
        for ei, effort in enumerate(block.get('efforts', [])):
            if any(item.get('status') == 'unresolved' and item.get('effort_id') == effort.get('id') and effort.get('id') is not None for item in conflicts):
                unresolved_efforts.add((bi, ei))
            for ref in effort.get('segments', []):
                key = (ref['activity_id'], ref['index'])
                uses[key] = uses.get(key, 0) + 1
    watch_evidence = []
    recordings = {}
    for bi, block in enumerate(after.get('blocks', [])):
        for ei, effort in enumerate(block.get('efforts', [])):
            reps = effort.get('reps')
            evidence = effort.get('provenance', {}).get('reps') or {}
            unknown_exercise = effort.get('exercise', '').strip().lower() in ('unknown', 'unidentified', 'unidentified recorded interval')
            if unknown_exercise:
                unresolved.append({'kind': 'unknown_exercise', 'block_index': bi, 'effort_index': ei})
                unresolved_efforts.add((bi, ei))
            if reps is not None and effort.get('reps_basis', 'unknown') == 'unknown':
                unresolved.append({'kind': 'unknown_reps_basis', 'block_index': bi, 'effort_index': ei})
                unresolved_efforts.add((bi, ei))
            for ref in effort.get('segments', []):
                aid = ref['activity_id']
                if aid not in recordings:
                    recordings[aid] = workouts.recording(conn, aid)
                index = ref['index']
                if index >= len(recordings[aid]):
                    continue
                watch = recordings[aid][index].get('repetitionCount')
                if not isinstance(watch, int) or isinstance(watch, bool) or watch < 0:
                    continue
                comparable = (reps is not None and evidence.get('source') == 'user'
                              and effort.get('reps_basis') == 'total' and len(effort.get('segments', [])) == 1
                              and uses[(aid, index)] == 1)
                watch_evidence.append({'block_index': bi, 'effort_index': ei, 'value': watch,
                                       'source': 'watch', 'activity_id': aid, 'segment_index': index,
                                       'comparable': comparable})
                if comparable and watch != reps:
                    conflicts.append({'kind': 'repetition_count', 'block_index': bi, 'effort_index': ei,
                                      'reported': {'value': reps, 'source': 'user'},
                                      'recorded': {'value': watch, 'source': 'watch', 'activity_id': aid, 'segment_index': index}})
    return {'changes': changes, 'change_count': count, 'unresolved_fields': unresolved[:100],
            'unresolved_count': len(unresolved), 'unresolved_effort_count': len(unresolved_efforts),
            'recorded_repetitions': watch_evidence[:100], 'recorded_repetitions_count': len(watch_evidence),
            'conflicts': conflicts[:100], 'conflict_count': len(conflicts),
            'structure_evidence': {'note_id': note_id, 'explanation': explanation}}


def _proposal_status(conn: sqlite3.Connection, proposal: sqlite3.Row, include_workout: bool = False) -> dict[str, Any]:
    current = head(conn, proposal['activity_id'])
    applied = proposal['applied_revision']
    successor = conn.execute('SELECT successor_id FROM proposal_successors WHERE proposal_id=?', (proposal['proposal_id'],)).fetchone()
    lifecycle = 'applied' if applied is not None else 'superseded' if successor else 'draft'
    applicability = None
    if lifecycle == 'draft':
        if current != proposal['base_revision']:
            applicability = 'workout_changed'
        elif proposal['workout_json'] is None or proposal['source_hash'] is None:
            applicability = None
        else:
            source_ids = json.loads(proposal['workout_json'])['source_activity_ids']
            applicability = 'current' if proposal['source_hash'] == workouts.source_hash(conn, source_ids) else 'recording_changed'
    result = {'proposal_id': proposal['proposal_id'], 'activity_id': proposal['activity_id'],
              'base_revision': proposal['base_revision'], 'current_revision': current,
              'applied_revision': applied, 'status': lifecycle, 'lifecycle': lifecycle,
              'applicability': applicability, 'note_id': proposal['note_id'],
              'explanation': proposal['explanation'], 'created_at': proposal['created_at']}
    if proposal['workout_json']:
        result['fingerprint'] = _fingerprint(json.loads(proposal['workout_json']))
    if successor:
        result['successor_id'] = successor['successor_id']
    if include_workout:
        result['workout'] = json.loads(proposal['workout_json']) if proposal['workout_json'] else None
        review = conn.execute('SELECT review_json FROM proposal_reviews WHERE proposal_id=?', (proposal['proposal_id'],)).fetchone()
        if review:
            result.update(json.loads(review['review_json']))
            if applicability in ('workout_changed', 'recording_changed'):
                result['conflicts'] = [*result.get('conflicts', []), {'kind': applicability}]
    return result


def get_proposal(proposal_id: str, include_workout: bool = False) -> dict[str, Any]:
    """Read a proposal's lifecycle and current applicability, optionally including its body."""
    with connect() as conn:
        conn.execute('BEGIN')
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if proposal is None:
            raise ValueError('Unknown proposal')
        return _proposal_status(conn, proposal, include_workout)


def list_workout_proposals(activity_id: int, status: Literal['draft', 'applied', 'superseded'] | None = None,
                           limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List bounded, compact proposal statuses for one activity."""
    page(limit, offset)
    if status not in (None, 'draft', 'applied', 'superseded'):
        raise ValueError('status must be draft, applied or superseded')
    where = 'activity_id=?'
    if status == 'applied':
        where += ' AND applied_revision IS NOT NULL'
    elif status == 'draft':
        where += ' AND applied_revision IS NULL AND NOT EXISTS (SELECT 1 FROM proposal_successors s WHERE s.proposal_id=set_proposals.proposal_id)'
    elif status == 'superseded':
        where += ' AND applied_revision IS NULL AND EXISTS (SELECT 1 FROM proposal_successors s WHERE s.proposal_id=set_proposals.proposal_id)'
    with connect() as conn:
        conn.execute('BEGIN')
        require_activity(conn, activity_id)
        total = conn.execute(f'SELECT count(*) FROM set_proposals WHERE {where}', (activity_id,)).fetchone()[0]
        rows = conn.execute(f'SELECT * FROM set_proposals WHERE {where} ORDER BY created_at DESC,proposal_id DESC LIMIT ? OFFSET ?', (activity_id, limit, offset)).fetchall()
        items = [_proposal_status(conn, row) for row in rows]
    return {'items': items, 'total': total, 'next_offset': offset + limit if offset + limit < total else None}


def save_note(activity_id: int, note: str, request_key: str | None = None) -> dict[str, Any]:
    if not note.strip() or len(note) > 20000:
        raise ValueError('Provide a nonempty note of at most 20000 characters')
    note_id = str(uuid4())
    fingerprint = _fingerprint({'activity_id': activity_id, 'note': note})
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = _receipt(conn, 'save_note', request_key, fingerprint)
        if previous is not None:
            return previous
        require_activity(conn, activity_id)
        conn.execute('INSERT INTO session_notes(note_id,activity_id,note) VALUES(?,?,?)', (note_id, activity_id, note))
        result = {'note_id': note_id, 'activity_id': activity_id}
        _save_receipt(conn, 'save_note', request_key, fingerprint, result)
    return result


def _revision(conn: sqlite3.Connection, activity_id: int, parent: int, payload: str | None, reason: str, workout: workouts.WorkoutDocument | None = None, frozen_from: int | None = None, *, legacy_format_notes: bool = False) -> int:
    cursor = conn.execute('INSERT INTO set_revisions(activity_id,parent_revision,sets_json,reason) VALUES(?,?,?,?)', (activity_id, parent, payload, reason))
    revision = cursor.lastrowid
    if workout is None:
        if payload is not None:
            raise ValueError('A correction revision requires its structured workout')
        workout = workouts.raw_workout(activity_id, workouts.recording(conn, activity_id))
    workouts.save_snapshot(conn, activity_id, revision, workout, 'structured_correction' if payload is not None else 'garmin_restore', frozen_from=frozen_from, legacy_format_notes=legacy_format_notes)
    conn.execute('INSERT INTO set_heads VALUES(?,?) ON CONFLICT(activity_id) DO UPDATE SET revision_id=excluded.revision_id', (activity_id, revision))
    return revision


def apply_proposal(proposal_id: str) -> dict[str, Any]:
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not proposal:
            raise ValueError('Unknown proposal')
        if proposal['applied_revision'] is not None:
            return {**_proposal_status(conn, proposal), 'revision': proposal['applied_revision'], 'already_applied': True}
        if conn.execute('SELECT 1 FROM proposal_successors WHERE proposal_id=?', (proposal_id,)).fetchone():
            raise ValueError('Proposal was superseded; review the successor')
        if head(conn, proposal['activity_id']) != proposal['base_revision']:
            raise ValueError('Stale proposal; reload the session and propose again')
        workout = workouts.parse_workout(proposal['workout_json'])
        if proposal['source_hash'] != workouts.source_hash(conn, workout.source_activity_ids):
            raise ValueError('Recording changed since this proposal; reload and propose again')
        legacy = workout.schema_version == 2 and not conn.execute('SELECT 1 FROM proposal_reviews WHERE proposal_id=?', (proposal_id,)).fetchone()
        revision = _revision(conn, proposal['activity_id'], proposal['base_revision'], proposal['sets_json'], f"Proposal {proposal_id}: {proposal['explanation']}", workout, legacy_format_notes=legacy)
        conn.execute('UPDATE set_proposals SET applied_revision=? WHERE proposal_id=?', (revision, proposal_id))
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        result = {**_proposal_status(conn, proposal), 'revision': revision, 'already_applied': False}
    return result


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
            snapshot = conn.execute('SELECT version_id,document_json FROM workout_versions WHERE activity_id=? AND revision_id=?', (activity_id, target_revision)).fetchone()
            if snapshot is not None:
                workout = workouts.parse_workout(snapshot['document_json'])
                frozen_from = snapshot['version_id']
            else:
                raise ValueError('Revision snapshot is unavailable')
        revision = _revision(conn, activity_id, current, payload, f'Restore {target_revision}: {reason}', workout, frozen_from)
        if target_revision == 0:
            conn.execute('INSERT INTO live_raw_revisions(activity_id,revision_id) VALUES(?,?)', (activity_id, revision))
    return {'revision': revision, 'activity_id': activity_id, 'restored_revision': target_revision}


def propose_workout(activity_id: int, note_id: str, workout: workouts.WorkoutDocument, explanation: str, expected_revision: int, request_key: str | None = None) -> dict[str, Any]:
    """Draft a complete structured replacement using the existing review workflow."""
    workout = workouts.parse_workout(workout)
    if not explanation.strip() or len(explanation) > 20000:
        raise ValueError('Provide a nonempty explanation of at most 20000 characters')
    proposal_id = str(uuid4())
    fingerprint = _fingerprint({'activity_id': activity_id, 'note_id': note_id, 'workout': workout.model_dump(mode='json'),
                                'explanation': explanation, 'expected_revision': expected_revision})
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = _receipt(conn, 'propose_workout', request_key, fingerprint)
        if previous is not None:
            return previous
        require_activity(conn, activity_id)
        if head(conn, activity_id) != expected_revision:
            raise ValueError('Revision changed; reload the session and propose again')
        if not conn.execute('SELECT 1 FROM session_notes WHERE note_id=? AND activity_id=?', (note_id, activity_id)).fetchone():
            raise ValueError('Note does not belong to this activity')
        workouts.validate_sources(conn, activity_id, workout)
        before = workouts.current_snapshot(conn, activity_id, expected_revision)['workout']
        if workout.schema_version == 3:
            baseline = workouts.convert_workout(before, snapshot_scope=f'{activity_id}:{expected_revision}').model_dump(mode='json')
            _validate_v3_changes(baseline, workout.model_dump(mode='json'))
        payload = workouts.dumps(workouts.legacy_sets(workout))
        document = workouts.dumps(workout.model_dump())
        conn.execute('INSERT INTO set_proposals(proposal_id,activity_id,base_revision,sets_json,note_id,explanation,workout_json,source_hash) VALUES(?,?,?,?,?,?,?,?)',
                     (proposal_id, activity_id, expected_revision, payload, note_id, explanation, document, workouts.source_hash(conn, workout.source_activity_ids)))
        review = _review_changes(conn, before, workout.model_dump(), note_id, explanation)
        conn.execute('INSERT INTO proposal_reviews(proposal_id,review_json) VALUES(?,?)', (proposal_id, json.dumps(review, allow_nan=False)))
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        result = {**_proposal_status(conn, proposal, include_workout=True),
                  'apply_tool': 'apply_proposal'}
        _save_receipt(conn, 'propose_workout', request_key, fingerprint, result)
    return result


def _validate_v3_changes(before: dict, after: dict) -> None:
    """Allow legacy attribution only while the corresponding value is unchanged."""
    old_blocks = {block['id']: block for block in before['blocks']}
    for block in after['blocks']:
        old_block = old_blocks.get(block['id'], {})
        if old_block and old_block['evidence']['source'] not in ('unknown', 'legacy') and block['evidence']['source'] in ('unknown', 'legacy'):
            raise ValueError('Block evidence cannot be downgraded')
        group_shape = lambda b: [(g['id'], g['effort_ids']) for g in b.get('drop_sets', [])]
        structure_changed = (any(old_block.get(key) != block.get(key) for key in ('kind', 'label'))
                             or group_shape(old_block) != group_shape(block)
                             or [e['id'] for e in old_block.get('efforts', [])] != [e['id'] for e in block['efforts']])
        if structure_changed and block['evidence']['source'] in ('unknown', 'legacy'):
            raise ValueError('Changed block structure requires known evidence')
        if structure_changed and block['evidence'] == old_block.get('evidence'):
            raise ValueError('Changed block structure requires updated evidence')
        old_groups = {group['id']: group for group in old_block.get('drop_sets', [])}
        for group in block.get('drop_sets', []):
            old_group = old_groups.get(group['id'], {})
            if old_group and old_group['evidence']['source'] not in ('unknown', 'legacy') and group['evidence']['source'] in ('unknown', 'legacy'):
                raise ValueError('Drop-set evidence cannot be downgraded')
            if group.get('effort_ids') != old_group.get('effort_ids'):
                if group['evidence']['source'] in ('unknown', 'legacy') or group['evidence'] == old_group.get('evidence'):
                    raise ValueError('Changed drop-set group requires updated known evidence')
        old_format, new_format = old_block.get('format') or {}, block.get('format') or {}
        for field, evidence in old_format.get('provenance', {}).items():
            replacement_evidence = new_format.get('provenance', {}).get(field)
            if new_format and evidence['source'] not in ('unknown', 'legacy') and (not replacement_evidence or replacement_evidence['source'] in ('unknown', 'legacy')):
                raise ValueError(f'Existing format provenance for {field} cannot be removed or downgraded')
        format_removed = bool(old_block.get('format')) and block.get('format') is None
        if format_removed and (block['evidence']['source'] in ('unknown', 'legacy') or block['evidence'] == old_block.get('evidence')):
            raise ValueError('Removing a block format requires updated known block evidence')
        for key in ('time_cap_seconds', 'interval_seconds', 'rounds_completed', 'extra_reps'):
            if format_removed:
                break
            if old_format.get(key) != new_format.get(key):
                evidence = new_format.get('provenance', {}).get(key)
                if not evidence or evidence['source'] in ('legacy', 'unknown'):
                    raise ValueError(f'Changed format field {key} requires known provenance')
                if evidence == old_format.get('provenance', {}).get(key):
                    raise ValueError(f'Changed format field {key} requires updated provenance')
        old_efforts = {effort['id']: effort for effort in old_block.get('efforts', [])}
        for effort in block['efforts']:
            old = old_efforts.get(effort['id'], {})
            for field, evidence in old.get('provenance', {}).items():
                replacement_evidence = effort.get('provenance', {}).get(field)
                if evidence['source'] not in ('unknown', 'legacy') and (not replacement_evidence or replacement_evidence['source'] in ('unknown', 'legacy')):
                    raise ValueError(f'Existing provenance for {field} cannot be removed or downgraded')
            fields = ('reps', 'load', 'distance_meters', 'duration_seconds', 'assistance', 'round', 'details')
            changed = [key for key in fields if old.get(key) != effort.get(key)]
            if not old and not effort.get('details'):
                changed.remove('details')
            for key in ('exercise', 'exercise_key', 'kind', 'reps_basis', 'segments'):
                if old.get(key) != effort.get(key) and (old or effort.get(key) not in (None, [], 'unknown')):
                    changed.append(key)
            for section in ('measured', 'reported', 'targets'):
                old_values, new_values = old.get(section) or {}, effort.get(section) or {}
                changed.extend(f'{section}.{key}' for key in set(old_values) | set(new_values)
                               if old_values.get(key) != new_values.get(key))
            for key in changed:
                evidence = effort.get('provenance', {}).get(key)
                if not evidence or evidence['source'] in ('legacy', 'unknown'):
                    raise ValueError(f'Changed field {key} requires known provenance')
                if evidence == old.get('provenance', {}).get(key):
                    raise ValueError(f'Changed field {key} requires updated provenance')


def _validate_replacement_evidence(before: dict, after: dict, note_id: str) -> None:
    """Require new attribution where an edit changes a recorded field."""
    tracked = ('exercise', 'exercise_key', 'round', 'kind', 'reps', 'reps_basis', 'load',
               'distance_meters', 'duration_seconds', 'assistance', 'segments', 'details')
    changed = [key for key in tracked if before.get(key) != after.get(key)]
    if not before:
        changed = [key for key in changed if after.get(key) not in (None, [], {}, 'unknown')]
    for section in ('measured', 'reported', 'targets'):
        old = before.get(section) or {}
        new = after.get(section) or {}
        changed.extend(f'{section}.{key}' for key in set(old) | set(new) if old.get(key) != new.get(key))
    for key in changed:
        evidence = after.get('provenance', {}).get(key)
        if not evidence or evidence.get('source') in ('unknown', 'legacy'):
            raise ValueError(f'Changed field {key} requires known provenance')
        if evidence.get('source') == 'user' and note_id not in evidence.get('note_ids', []):
            raise ValueError(f'Changed field {key} requires user provenance referencing the evidence note')
        if evidence == before.get('provenance', {}).get(key):
            raise ValueError(f'Changed field {key} requires updated provenance')


def revise_workout_proposal(proposal_id: str, target_kind: Literal['effort', 'block'], target_id: str,
                            replacement: workouts.EffortV3 | workouts.BlockV3, expected_fingerprint: str,
                            evidence_note_id: str, request_key: str | None = None) -> dict[str, Any]:
    """Replace one v3 effort or block in a new, reviewable successor draft."""
    if target_kind not in ('effort', 'block'):
        raise ValueError('target_kind must be effort or block')
    model = workouts.EffortV3 if target_kind == 'effort' else workouts.BlockV3
    replacement = model.model_validate(replacement)
    new_value = replacement.model_dump(mode='json')
    fingerprint = _fingerprint({'proposal_id': proposal_id, 'target_kind': target_kind, 'target_id': target_id,
                                'replacement': new_value, 'expected_fingerprint': expected_fingerprint,
                                'evidence_note_id': evidence_note_id})
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = _receipt(conn, 'revise_workout_proposal', request_key, fingerprint)
        if previous is not None:
            return previous
        proposal = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if proposal is None:
            raise ValueError('Unknown proposal')
        status = _proposal_status(conn, proposal)
        if status['lifecycle'] != 'draft':
            raise ValueError('Only an active draft can be revised')
        if status['applicability'] != 'current':
            raise ValueError('Draft is stale; reload the workout before revising')
        if status['fingerprint'] != expected_fingerprint:
            raise ValueError('Draft changed; reload before revising')
        document = json.loads(proposal['workout_json'])
        if document.get('schema_version', 2) != 3:
            raise ValueError('Convert a version 2 workout explicitly before revising')
        if not conn.execute('SELECT 1 FROM session_notes WHERE note_id=? AND activity_id=?', (evidence_note_id, proposal['activity_id'])).fetchone():
            raise ValueError('Evidence note does not belong to this activity')
        if replacement.id != target_id:
            raise ValueError('Replacement must preserve the target ID')
        matches = []
        for block in document['blocks']:
            if target_kind == 'block' and block['id'] == target_id:
                matches.append((document['blocks'], document['blocks'].index(block), block))
            if target_kind == 'effort':
                for index, effort in enumerate(block['efforts']):
                    if effort['id'] == target_id:
                        matches.append((block['efforts'], index, effort))
        if len(matches) != 1:
            raise ValueError('Target does not identify one item in the draft')
        container, index, before = matches[0]
        if before == new_value:
            raise ValueError('Replacement does not change the draft')
        if target_kind == 'effort':
            _validate_replacement_evidence(before, new_value, evidence_note_id)
        else:
            if new_value['evidence']['source'] in ('unknown', 'legacy') or evidence_note_id not in new_value['evidence']['note_ids']:
                raise ValueError('Replacement block requires known evidence referencing the note')
            old_efforts = {effort['id']: effort for effort in before['efforts']}
            for effort in new_value['efforts']:
                _validate_replacement_evidence(old_efforts.get(effort['id'], {}), effort, evidence_note_id)
        container[index] = new_value
        workout = workouts.parse_workout(document)
        _validate_v3_changes(json.loads(proposal['workout_json']), document)
        workouts.validate_sources(conn, proposal['activity_id'], workout)
        successor_id = str(uuid4())
        payload = workouts.dumps(workouts.legacy_sets(workout))
        conn.execute('INSERT INTO set_proposals(proposal_id,activity_id,base_revision,sets_json,note_id,explanation,workout_json,source_hash) VALUES(?,?,?,?,?,?,?,?)',
                     (successor_id, proposal['activity_id'], proposal['base_revision'], payload, evidence_note_id,
                      proposal['explanation'], workouts.dumps(workout.model_dump()), proposal['source_hash']))
        review = _review_changes(conn, json.loads(proposal['workout_json']), document, evidence_note_id, proposal['explanation'])
        conn.execute('INSERT INTO proposal_reviews(proposal_id,review_json) VALUES(?,?)', (successor_id, json.dumps(review, allow_nan=False)))
        conn.execute('INSERT INTO proposal_successors(proposal_id,successor_id) VALUES(?,?)', (proposal_id, successor_id))
        successor = conn.execute('SELECT * FROM set_proposals WHERE proposal_id=?', (successor_id,)).fetchone()
        result = {**_proposal_status(conn, successor, include_workout=True),
                  'previous_proposal_id': proposal_id, 'diff': {'target_kind': target_kind, 'target_id': target_id,
                                                               'before': before, 'after': new_value},
                  'apply_tool': 'apply_proposal'}
        _save_receipt(conn, 'revise_workout_proposal', request_key, fingerprint, result)
    return result
