from contextlib import ExitStack
import asyncio
import json
import os
import sys
from unittest.mock import patch

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from afterset import connection, db, store, sync, workouts


class TestStorage:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            stack.enter_context(patch.object(connection, 'DB_PATH', tmp_path / 'afterset.db'))
            store.initialize()
            db.upsert_activities([{'activity_id': 1, 'date': '2026-10-16', 'type': 'running'}, {'activity_id': 2, 'date': '2026-10-16'}])
            db.upsert_exercise_sets(1, {'exerciseSets': [{'repetitions': 7}]})
            yield

    def proposal(self, revision=0):
        note = store.save_note(1, 'Squat, 8 reps at 60 kg')
        document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(kind='straight_sets', label='Squat', evidence=workouts.Evidence(source='user', text='User description', note_ids=[note['note_id']]), efforts=[workouts.Effort(exercise='Squat', reps=8, reps_basis='total', load=workouts.Load(kg=60, basis='total_external'), evidence='User: 8 reps at 60 kg')])])
        return store.propose_workout(1, note['note_id'], document, 'Correct missed squat set', revision)

    def test_draft_apply_undo_and_original_retention(self):
        proposal = self.proposal()
        assert store.get_activity(1)['source'] == 'garmin'
        result = store.apply_proposal(proposal['proposal_id'])
        assert store.apply_proposal(proposal['proposal_id'])['revision'] == result['revision']
        session = store.get_activity(1, include=['raw'])
        assert session['workout']['blocks'][0]['efforts'][0]['reps'] == 8
        assert session['garmin_sets_response']['exerciseSets'][0]['repetitions'] == 7
        undo = store.restore_sets(1, 0, result['revision'], 'Undo')
        assert store.get_activity(1)['source'] == 'garmin'
        store.restore_sets(1, result['revision'], undo['revision'], 'Redo')
        assert store.get_activity(1)['workout']['blocks'][0]['efforts'][0]['load']['kg'] == 60

    def test_stale_and_cross_activity_revisions_rejected(self):
        first, stale = self.proposal(), self.proposal()
        revision = store.apply_proposal(first['proposal_id'])['revision']
        with pytest.raises(ValueError, match='Stale'):
            store.apply_proposal(stale['proposal_id'])
        with pytest.raises(ValueError):
            store.restore_sets(1, 0, 0, 'stale undo')
        with pytest.raises(ValueError):
            store.restore_sets(2, revision, 0, 'wrong activity')

    def test_empty_corrected_workout_is_distinct_from_raw(self):
        note = store.save_note(1, 'No exercises; accidental recording')
        proposal = store.propose_workout(1, note['note_id'], workouts.Workout(source_activity_ids=[1], blocks=[]), 'Remove accidental sets', 0)
        store.apply_proposal(proposal['proposal_id'])
        session = store.get_activity(1, include=['raw'])
        assert session['workout']['blocks'] == []
        assert session['source'] == 'structured_correction'

    def test_validation_and_query_pagination(self):
        with pytest.raises(ValueError):
            workouts.Effort(exercise='Squat', reps=-1, evidence='note')
        with pytest.raises(ValueError):
            workouts.Effort(exercise='Squat', distance_meters=float('nan'), evidence='note')
        with pytest.raises(ValueError):
            store.list_activities('2026-10-17', '2026-10-16')
        page = store.list_activities('2026-10-16', '2026-10-16', limit=1)
        assert page['total'] == 2
        assert page['next_offset'] == 1
        assert store.list_activities('2026-10-16', '2026-10-16', "' OR 1=1--")['total'] == 0
        store.store_payload(1, 'samples', {'measurementDescriptors': [{'unit': 'bpm'}], 'activityDetailMetrics': list(range(5))})
        payload = store.get_payload(1, 'samples', offset=2, limit=2)
        assert payload['payload']['activityDetailMetrics'] == [2, 3]
        assert payload['next_offset'] == 4
        assert payload['payload']['measurementDescriptors'] == [{'unit': 'bpm'}]

    def test_failed_wellness_day_does_not_erase_data(self):
        db.upsert_rows([{'date':'2026-10-16', 'sleep_score':90}])
        class BrokenClient:
            def get_hrv_data(self, day):
                raise RuntimeError('Garmin unavailable')
        with pytest.raises(RuntimeError):
            sync._wellness_day(BrokenClient(), '2026-10-16', [])
        assert store.daily_metrics('2026-10-16', '2026-10-16')['items'][0]['sleep_score'] == 90

    def test_partial_sync_and_corrections_survive(self):
        proposal = self.proposal()
        store.apply_proposal(proposal['proposal_id'])
        class Client:
            def get_activities_by_date(self, start, end):
                return [{'activityId':1, 'startTimeLocal':'2026-10-16 10:00:00'}]
            def get_activity_exercise_sets(self, activity_id):
                raise RuntimeError('failed')
        job = 'test-job'
        request = {'start':'2026-10-16', 'end':'2026-10-16', 'scope':'activities', 'enrich':False}
        with store.connect() as conn:
            conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)', (job, 'queued', json.dumps(request), '{}'))
        sync._lock.acquire()
        with patch.object(sync, '_client', return_value=Client()), patch('afterset.auth.save_session_tokens'):
            sync._run(job, request)
        assert sync.job_status(job)['state'] == 'partial'
        assert store.get_activity(1)['workout']['blocks'][0]['efforts'][0]['reps'] == 8
        assert store.get_activity(1, include=['raw'])['garmin_sets_response']['exerciseSets'][0]['repetitions'] == 7

    def test_process_lock_and_live_jobs_survive_other_server_startup(self):
        with store.connect() as conn:
            conn.execute("INSERT INTO sync_jobs VALUES('live','running','{}','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        handle = sync._process_lock()
        try:
            with pytest.raises(ValueError):
                sync._process_lock()
            sync.mark_interrupted()
            assert sync.job_status('live')['state'] == 'running'
        finally:
            handle.close()
        sync.mark_interrupted()
        assert sync.job_status('live')['state'] == 'interrupted'

    def test_mcp_protocol_discovery_query_and_error(self):
        async def check():
            params = StdioServerParameters(command=sys.executable, args=['-m','afterset.server'], env={**os.environ, 'AFTERSET_DB':str(connection.DB_PATH), 'AFTERSET_CONTEXT_DB':str(connection.DB_PATH.with_name('personal-context.db'))})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == 'Afterset'
                    tools = (await session.list_tools()).tools
                    names = {tool.name for tool in tools}
                    assert 'training_summary' in names
                    assert 'apply_proposal' in names
                    assert 'get_personal_context' in names
                    assert 'save_personal_context' in names
                    private_path = connection.DB_PATH.with_name('personal-context.db')
                    assert not private_path.exists()
                    saved = await session.call_tool('save_personal_context', {
                        'date': '2026-10-01', 'category': 'illness', 'label': 'Reported illness',
                        'notes': 'I was sick; that may explain lower HRV.'})
                    assert not saved.isError
                    personal = await session.call_tool('get_personal_context', {'start': '2026-10-01', 'end': '2026-10-02'})
                    assert not personal.isError
                    assert personal.structuredContent['events'][0]['event_id'] == saved.structuredContent['event_id']
                    training = await session.call_tool('training_summary', {'start': '2026-10-01', 'end': '2026-10-02'})
                    assert 'Reported illness' not in str(training.structuredContent)
                    assert 'propose_exercise_sets' not in names
                    assert 'apply_exercise_sets' not in names
                    result = await session.call_tool('data_coverage', {})
                    assert not result.isError
                    assert result.structuredContent['coverage']['activities']['rows'] == 2
                    error = await session.call_tool('get_activity', {'activity_id':999})
                    assert error.isError
                    result = await session.call_tool('save_session_note', {'activity_id':1, 'note':'8 squats at 60 kg'})
                    note_id = result.structuredContent['note_id']
                    document = workouts.Workout(source_activity_ids=[1], blocks=[workouts.Block(label='Squat', evidence=workouts.Evidence(source='user', text='User note', note_ids=[note_id]), efforts=[workouts.Effort(exercise='Squat', reps=8, reps_basis='total', evidence='User note')])])
                    proposal = await session.call_tool('propose_workout', {'activity_id':1,'note_id':note_id,'workout':document.model_dump(), 'explanation':'Correct workout','expected_revision':0})
                    assert not proposal.isError
                    applied = await session.call_tool('apply_proposal', {'proposal_id':proposal.structuredContent['proposal_id']})
                    assert not applied.isError
                    structured = await session.call_tool('get_workout', {'activity_id': 1})
                    assert not structured.isError
                    draft = await session.call_tool('propose_workout', {'activity_id': 1, 'note_id': note_id, 'workout': structured.structuredContent['workout'], 'explanation': 'Round-trip structured workout through MCP', 'expected_revision': applied.structuredContent['revision']})
                    assert not draft.isError
                    result = await session.call_tool('apply_proposal', {'proposal_id': draft.structuredContent['proposal_id']})
                    assert not result.isError
        asyncio.run(check())
