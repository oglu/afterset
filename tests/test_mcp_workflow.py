import asyncio
import copy
import os
import sys
from unittest.mock import patch

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
