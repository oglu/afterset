import asyncio
import copy
import os
import sqlite3
import sys
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from afterset import connection, db, store


def test_review_resume_convert_replace_and_compare_through_mcp(tmp_path):
    path = tmp_path / 'training.db'
    private = tmp_path / 'personal.db'
    with patch.object(connection, 'DB_PATH', path):
        store.initialize()
        db.upsert_activities([
            {'activity_id': 71, 'date': '2026-04-02', 'name': 'Synthetic strength'},
            {'activity_id': 72, 'date': '2026-04-09', 'name': 'Synthetic strength'},
        ])

    async def check():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'afterset.server'],
            env={**os.environ, 'AFTERSET_DB': str(path), 'AFTERSET_CONTEXT_DB': str(private)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, result.content
                    return result.structuredContent

                note = await call('save_session_note', {'activity_id': 71,
                    'note': 'Synthetic press, six reps.', 'request_key': 'note-first'})
                assert note == await call('save_session_note', {'activity_id': 71,
                    'note': 'Synthetic press, six reps.', 'request_key': 'note-first'})
                # Omitted schema_version remains a compatible v2 request.
                legacy = {'source_activity_ids': [71], 'blocks': [{'label': 'Press',
                    'evidence': {'source': 'user', 'text': 'Synthetic report', 'note_ids': [note['note_id']]},
                    'efforts': [{'exercise': 'Synthetic press', 'exercise_key': 'synthetic_press',
                        'reps': 6, 'reps_basis': 'total', 'evidence': 'Synthetic report'}]}]}
                invalid = copy.deepcopy(legacy)
                invalid['blocks'][0]['kind'] = 'private-invalid-kind'
                invalid['blocks'][0]['private-secret-key'] = 'private-secret-value'
                rejected = await session.call_tool('propose_workout', {'activity_id': 71, 'note_id': note['note_id'],
                    'workout': invalid, 'explanation': 'Synthetic reconstruction', 'expected_revision': 0})
                assert rejected.isError
                assert 'invalid choice' in str(rejected.content)
                assert 'private-invalid-kind' not in str(rejected.content)
                assert 'private-secret' not in str(rejected.content)
                assert (await call('list_workout_proposals', {'activity_id': 71}))['total'] == 0
                proposal = await call('propose_workout', {'activity_id': 71, 'note_id': note['note_id'],
                    'workout': legacy, 'explanation': 'Synthetic reconstruction', 'expected_revision': 0,
                    'request_key': 'draft-first'})
                applied = await call('apply_proposal', {'proposal_id': proposal['proposal_id']})
                status = await call('get_proposal', {'proposal_id': proposal['proposal_id']})
                assert status['lifecycle'] == 'applied'
                converted = await call('get_workout', {'activity_id': 71, 'target_schema_version': 3})
                assert converted['workout']['schema_version'] == 3
                draft = await call('propose_workout', {'activity_id': 71, 'note_id': note['note_id'],
                    'workout': converted['workout'], 'explanation': 'Prepare explicit converted draft',
                    'expected_revision': applied['revision'], 'request_key': 'draft-v3'})
                correction_note = await call('save_session_note', {'activity_id': 71,
                    'note': 'Correction: seven reps.', 'request_key': 'note-correction'})
                replacement = copy.deepcopy(draft['workout']['blocks'][0]['efforts'][0])
                replacement['reps'] = 7
                replacement['provenance']['reps'] = {'source': 'user', 'text': 'User corrected reps to seven.',
                    'note_ids': [correction_note['note_id']]}
                arguments = {'proposal_id': draft['proposal_id'], 'target_kind': 'effort',
                    'target_id': replacement['id'], 'replacement': replacement,
                    'expected_fingerprint': draft['fingerprint'], 'evidence_note_id': correction_note['note_id'],
                    'request_key': 'replace-first'}
                bad_arguments = copy.deepcopy(arguments)
                bad_arguments['replacement']['kind'] = 'private-invalid-kind'
                rejected = await session.call_tool('revise_workout_proposal', bad_arguments)
                assert rejected.isError and 'invalid choice' in str(rejected.content)
                assert 'private-invalid-kind' not in str(rejected.content)
                assert (await call('get_proposal', {'proposal_id': draft['proposal_id']}))['lifecycle'] == 'draft'
                missing_source = copy.deepcopy(arguments)
                missing_source['replacement']['provenance'].pop('reps')
                rejected = await session.call_tool('revise_workout_proposal', missing_source)
                assert rejected.isError and 'provenance' in str(rejected.content)
                assert (await call('get_proposal', {'proposal_id': draft['proposal_id']}))['lifecycle'] == 'draft'
                unknown_source = copy.deepcopy(arguments)
                unknown_source['replacement']['provenance']['reps']['source'] = 'unknown'
                rejected = await session.call_tool('revise_workout_proposal', unknown_source)
                assert rejected.isError and 'Changed field reps' in str(rejected.content)
                assert (await call('get_proposal', {'proposal_id': draft['proposal_id']}))['lifecycle'] == 'draft'
                successor = await call('revise_workout_proposal', arguments)
                assert successor == await call('revise_workout_proposal', arguments)
                superseded = await call('get_proposal', {'proposal_id': draft['proposal_id']})
                assert superseded['lifecycle'] == 'superseded'
                await call('apply_proposal', {'proposal_id': successor['proposal_id']})
                replay = await call('apply_proposal', {'proposal_id': proposal['proposal_id']})
                assert replay['revision'] == applied['revision']
                assert replay['already_applied']
                history = await call('get_exercise_history', {'exercise_key': 'synthetic_press',
                    'start': '2026-04-01', 'end': '2026-04-10'})
                assert history['total'] == 1
                assert history['items'][0]['effort']['reps'] == 7
                comparison = await call('compare_workouts', {'first_activity_id': 71, 'second_activity_id': 72})
                assert comparison['pairs'] == []
                signals = await call('get_activity_signals', {'activity_id': 71})
                assert not signals['available']
                heart = await call('compare_activity_heart_rate', {'first_activity_id': 71, 'second_activity_id': 72})
                assert heart['mean_difference_bpm'] is None
                assert not private.exists()

    asyncio.run(check())


def test_mixed_circuit_through_mcp(tmp_path):
    path = tmp_path / 'synthetic.db'
    with patch.object(connection, 'DB_PATH', path):
        store.initialize()
        db.upsert_activities([{'activity_id': 91, 'date': '2026-05-01', 'name': 'Synthetic circuit'}])
        db.upsert_exercise_sets(91, {'exerciseSets': [{'duration': 300, 'setType': 'ACTIVE'}]})

    async def run():
        params = StdioServerParameters(command=sys.executable, args=['-m', 'afterset.server'],
            env={**os.environ, 'AFTERSET_DB': str(path)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result.content
                    return result.structuredContent
                note = await call('save_session_note', {'activity_id': 91, 'note': 'Synthetic circuit report.'})
                evidence = {'source': 'user', 'text': 'Synthetic report', 'note_ids': [note['note_id']]}
                workout = {'schema_version': 3, 'source_activity_ids': [91], 'blocks': [
                    {'id': 'circuit', 'kind': 'circuit', 'label': 'Circuit', 'evidence': evidence, 'efforts': [
                        {'id': 'row-1', 'exercise': 'Synthetic row', 'round': 1, 'distance_meters': 150,
                         'segments': [{'activity_id': 91, 'index': 0}], 'evidence': 'Synthetic report'},
                        {'id': 'press-1', 'exercise': 'Synthetic press', 'round': 1, 'reps': 4,
                         'load': {'kg': 12, 'basis': 'total_external'},
                         'segments': [{'activity_id': 91, 'index': 0}], 'evidence': 'Synthetic report'},
                        {'id': 'row-2', 'exercise': 'Synthetic row', 'round': 2, 'distance_meters': 150,
                         'segments': [{'activity_id': 91, 'index': 0}], 'evidence': 'Synthetic report'}]},
                    {'id': 'finisher', 'kind': 'finisher', 'label': 'Finish', 'evidence': evidence, 'efforts': [
                        {'id': 'squat-1', 'exercise': 'Synthetic squat', 'reps': 3, 'evidence': 'Synthetic report'}]}]}
                for block in workout['blocks']:
                    for effort in block['efforts']:
                        effort['provenance'] = {key: evidence for key in
                            ('exercise', 'round', 'distance_meters', 'segments', 'reps', 'load') if key in effort}
                        effort['provenance']['kind'] = evidence
                invalid = copy.deepcopy(workout)
                invalid['blocks'][0]['efforts'][0]['measured'] = {'power_watts': {'kind': 'bad', 'value': 80}}
                rejected = await session.call_tool('propose_workout', {'activity_id': 91, 'note_id': note['note_id'],
                    'workout': invalid, 'explanation': 'Synthetic circuit', 'expected_revision': 0})
                assert rejected.isError and 'measurement kinds: exact, approximate, range' in str(rejected.content)
                assert 'block kinds:' not in str(rejected.content) and 'effort kinds:' not in str(rejected.content)
                args = {'activity_id': 91, 'note_id': note['note_id'], 'workout': workout,
                        'explanation': 'Synthetic circuit', 'expected_revision': 0, 'request_key': 'mixed-draft'}
                proposal = await call('propose_workout', args)
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE request_receipts SET result_json=? WHERE operation=? AND request_key=?",
                                 ('invalid synthetic receipt', 'propose_workout', 'mixed-draft'))
                retry = await session.call_tool('propose_workout', args)
                assert retry.isError and 'Could not confirm' in str(retry.content)
                assert 'created no draft' not in str(retry.content)
                assert (await call('list_workout_proposals', {'activity_id': 91}))['total'] == 1
                assert proposal['workout']['blocks'][0]['efforts'][0]['duration_seconds'] is None
                saved = await call('apply_proposal', {'proposal_id': proposal['proposal_id']})
                result = await call('get_workout', {'activity_id': 91, 'revision': saved['revision']})
                assert len(result['workout']['blocks']) == 2
                assert result['workout']['blocks'][0]['efforts'][1]['load']['kg'] == 12
                assert result['workout']['blocks'][0]['efforts'][2]['round'] == 2
    asyncio.run(run())


def test_uncertain_conversion_failure_is_private():
    from mcp.server.fastmcp.exceptions import ToolError
    from afterset.server import _call_with_workout_feedback

    def convert_result():
        raise ValueError('private result content')

    async def failing(*args, **kwargs):
        try:
            convert_result()
        except ValueError as exc:
            raise ToolError('private result content') from exc

    async def run():
        try:
            await _call_with_workout_feedback(failing, 'propose_workout', {})
        except ToolError as exc:
            assert 'Could not confirm' in str(exc)
            assert 'private result content' not in str(exc)
        else:
            assert False, 'Expected uncertain error'
    asyncio.run(run())


@pytest.mark.parametrize('message, rejected', [
    ('Changed block structure requires updated evidence', True),
    ('Changed drop-set group requires updated known evidence', True),
    ('Changed format field interval_seconds requires known provenance', True),
    ('Existing format provenance for time_cap_seconds cannot be removed or downgraded', True),
    ('Changed field measured.power_watts requires known provenance', True),
    ('Changed field reported.pace_seconds_per_500m requires known provenance', True),
    ('Existing provenance for reported.speed_meters_per_second cannot be removed or downgraded', True),
    ('Changed field private.secret requires known provenance', False),
])
def test_domain_diagnostics_recognize_only_schema_fields(message, rejected):
    from mcp.server.fastmcp.exceptions import ToolError
    from afterset.server import _call_with_workout_feedback

    def propose_workout():
        raise ValueError(message)

    async def failing(*args, **kwargs):
        try:
            propose_workout()
        except ValueError as exc:
            raise ToolError('private') from exc

    async def run():
        with pytest.raises(ToolError) as error:
            await _call_with_workout_feedback(failing, 'propose_workout', {})
        if rejected:
            assert 'this request created no draft' in str(error.value)
            assert message in str(error.value)
        else:
            assert 'Could not confirm' in str(error.value)
            assert 'private.secret' not in str(error.value)
    asyncio.run(run())
