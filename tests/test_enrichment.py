import json
from unittest.mock import patch

import pytest
from afterset import connection, db, store, sync
from afterset.store import payloads


def test_targeted_failures_retain_payload(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}])
        store.store_payload(1, 'samples', {'old': True})
        class Client:
            calls = 0
            def get_activity_details(self, activity_id):
                self.calls += 1
                raise TimeoutError('private secret')
        client = Client()
        request = {'activity_ids':[1], 'kinds':['samples'], 'refresh':True, 'scope':'activities', 'enrich':True}
        with store.connect() as conn:
            conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)', ('job', 'queued', json.dumps(request), '{}'))
        sync._lock.acquire()
        with patch('afterset.auth.save_session_tokens'):
            sync._run('job', request, client=client)
        assert client.calls == 1
        status = payloads.enrichment_status(1)
        assert status['latest_outcomes'][0]['outcome'] == 'failed'
        assert status['stored'][0]['kind'] == 'samples'
        assert store.get_payload(1, 'samples')['payload'] == {'old':True}
        assert 'private secret' not in str(sync.job_status('job'))
        assert sync.job_status('job')['state'] == 'partial'


def test_targeted_validation_and_skip_existing(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}])
        store.store_payload(1, 'samples', {'old': True})
        for ids in ([], [1,1], [True], list(range(1,52))):
            with pytest.raises(ValueError):
                sync.start_enrichment(ids)
        class Client:
            def get_activity_details(self, activity_id):
                raise AssertionError('retained data must be skipped')
        request = {'activity_ids':[1], 'kinds':['samples'], 'refresh':False, 'scope':'activities', 'enrich':True}
        with store.connect() as conn:
            conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)', ('job', 'queued', json.dumps(request), '{}'))
        sync._lock.acquire()
        with patch('afterset.auth.save_session_tokens'):
            sync._run('job', request, client=Client())
        assert sync.job_status('job')['state'] == 'completed'
        assert payloads.enrichment_status(1)['latest_outcomes'] == []


def test_invalid_envelopes_do_not_replace_good_data(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}])
        db.upsert_exercise_sets(1, {'exerciseSets':[{'repetitions':4}]})
        with pytest.raises(ValueError, match='envelope'):
            payloads.save_fetched(1, 'sets', 'connect', {'error':'upstream error'})
        assert store.get_activity(1, include=['raw'])['garmin_sets_response']['exerciseSets'][0]['repetitions'] == 4
        with pytest.raises(ValueError, match='match'):
            payloads.save_fetched(1, 'sets', 'connect', {'activityId':2, 'exerciseSets':[]})
        payloads.save_fetched(1, 'sets', 'connect', {'exerciseSets':[]})
        assert store.get_activity(1, include=['raw'])['garmin_sets_response']['exerciseSets'] == []


def test_rate_limit_stops_remaining_requests(tmp_path):
    from garminconnect import GarminConnectTooManyRequestsError
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}, {'activity_id':2, 'date':'2026-01-01'}])
        class Client:
            calls = 0
            def get_activity_details(self, activity_id):
                self.calls += 1
                raise GarminConnectTooManyRequestsError()
        client = Client()
        request = {'activity_ids':[1,2], 'kinds':['samples'], 'refresh':True, 'scope':'activities', 'enrich':True}
        with store.connect() as conn:
            conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)', ('job', 'queued', json.dumps(request), '{}'))
        sync._lock.acquire()
        with patch('afterset.auth.save_session_tokens'):
            sync._run('job', request, client=client)
        assert client.calls == 1
        assert sync.job_status('job')['state'] == 'partial'
        assert sync.job_status('job')['progress']['rate_limited'] is True


@pytest.mark.parametrize('where', ['list', 'activity'])
def test_cli_stops_future_chunks_on_rate_limit(tmp_path, where):
    from garminconnect import GarminConnectTooManyRequestsError
    from typer.testing import CliRunner
    from afterset import auth, cli
    from unittest.mock import Mock
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        client = Mock()
        if where == 'list':
            client.get_activities_by_date.side_effect = GarminConnectTooManyRequestsError()
        else:
            client.get_activities_by_date.return_value = [{'activityId':1, 'startTimeLocal':'2026-01-01 10:00:00'}]
            client.get_activity_exercise_sets.side_effect = GarminConnectTooManyRequestsError()
        with patch.object(auth, 'get_garmin_client', return_value=client), patch.object(auth, 'save_session_tokens'):
            result = CliRunner().invoke(cli.app, ['activities', '--start', '2026-01-01', '--end', '2026-03-10'])
        assert result.exit_code == 1, result.output
        assert client.get_activities_by_date.call_count == 1
        assert 'remaining date ranges were not requested' in result.output
        with store.connect() as conn:
            progress = json.loads(conn.execute('SELECT progress_json FROM sync_jobs').fetchone()[0])
        assert progress['rate_limited'] is True


def test_malformed_sample_rows_do_not_replace_retained_data(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path/'db'):
        store.initialize()
        db.upsert_activities([{'activity_id':1, 'date':'2026-01-01'}])
        original = {'metricDescriptors': [], 'activityDetailMetrics': []}
        payloads.save_fetched(1, 'samples', 'connect', original)
        for bad in ({'metricDescriptors':[None], 'activityDetailMetrics':[]},
                    {'metricDescriptors':[], 'activityDetailMetrics':['error']},
                    {'metricDescriptors':[], 'activityDetailMetrics':[{'metrics':None}]}):
            with pytest.raises(ValueError, match='envelope'):
                payloads.save_fetched(1, 'samples', 'connect', bad)
            assert store.get_payload(1, 'samples')['payload'] == original
