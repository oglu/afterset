"""Regression coverage for shared storage, nonblocking reads and CLI sync."""
from contextlib import ExitStack
import json
import sqlite3
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from afterset import auth, cli, connection, db, store, sync


class TestCleanup:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            setting = patch.object(connection, 'DB_PATH', tmp_path / 'nested' / 'records.db')
            stack.enter_context(setting)
            store.initialize()
            cache = patch.object(auth, 'save_session_tokens')
            stack.enter_context(cache)
            yield

    def test_reads_do_not_take_writer_lock_or_refresh_raw_snapshot(self):
        db.upsert_activities([{'activity_id': 1, 'date': '2026-10-16', 'name': 'Original'}])
        db.upsert_exercise_sets(1, {'exerciseSets': [{'repetitionCount': 7}]})
        with connection.connect() as writer:
            assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert writer.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE activities SET name='Uncommitted' WHERE activity_id=1")
            original_connect = sqlite3.connect
            def short_timeout(*args, **kwargs):
                kwargs['timeout'] = 0.05
                return original_connect(*args, **kwargs)
            # Old BEGIN IMMEDIATE or an implicit snapshot write fails immediately.
            with patch.object(connection.sqlite3, 'connect', side_effect=short_timeout):
                assert store.get_activity(1)['activity']['name'] == 'Original'
                assert store.get_workout(1)['revision'] == 0
        assert store.get_activity(1)['activity']['name'] == 'Uncommitted'

    def test_wal_reader_has_consistent_snapshot_while_sync_writes(self):
        db.upsert_activities([{'activity_id': 1, 'date': '2026-10-16', 'name': 'Before'}])
        with connection.connect() as reader:
            reader.execute('BEGIN')
            assert reader.execute('SELECT name FROM activities').fetchone()[0] == 'Before'
            db.upsert_activities([{'activity_id': 1, 'date': '2026-10-16', 'name': 'After'}])
            assert reader.execute('SELECT name FROM activities').fetchone()[0] == 'Before'
        assert store.get_activity(1)['activity']['name'] == 'After'

    def client(self):
        client = Mock()
        client.get_activities_by_date.return_value = []
        client.get_hrv_data.return_value = {}
        client.get_sleep_data.return_value = {'dailySleepDTO': {'sleepScores': {'overall': {'value': 80}}}}
        client.get_stress_data.return_value = {'avgStressLevel': 25}
        client.get_body_battery.return_value = []
        client.get_heart_rates.return_value = {'restingHeartRate': 55}
        client.get_stats.return_value = {'totalSteps': 1000}
        return client

    def invoke(self, args, client):
        with patch.object(auth, 'get_credentials', return_value=('email', 'password')), patch.object(auth, 'get_garmin_client', return_value=client), patch.object(sync, '_client', side_effect=AssertionError('CLI must use its authenticated client')):
            return CliRunner().invoke(cli.app, args)

    def test_cli_fetch_chunks_and_preserves_failed_days_and_optional_metrics(self):
        db.upsert_rows([
            {'date': '2026-01-01', 'sleep_score': 90, 'sleep_hrv': 70},
            {'date': '2026-01-02', 'sleep_score': 85, 'sleep_hrv': 65},
        ])
        client = self.client()
        sleep = client.get_sleep_data.return_value
        def get_sleep(day):
            if day == '2026-01-01':
                raise RuntimeError('Garmin unavailable')
            return sleep
        client.get_sleep_data.side_effect = get_sleep
        result = self.invoke(['fetch', '--start', '2026-01-01', '--end', '2026-03-04'], client)
        assert result.exit_code == 1, result.output
        assert 'partial' in result.output
        assert [call.args for call in client.get_activities_by_date.call_args_list] == [
            ('2026-01-01', '2026-01-31'), ('2026-02-01', '2026-03-03'), ('2026-03-04', '2026-03-04'),
        ]
        rows = store.daily_metrics('2026-01-01', '2026-03-04')['items']
        assert len(rows) == 63
        assert (rows[0]['sleep_score'], rows[0]['sleep_hrv']) == (90, 70)
        assert (rows[1]['sleep_score'], rows[1]['sleep_hrv']) == (80, 65)
        with connection.connect() as conn:
            jobs = [dict(row) for row in conn.execute('SELECT * FROM sync_jobs ORDER BY rowid')]
        assert [job['state'] for job in jobs] == ['partial', 'completed', 'completed']
        assert json.loads(jobs[0]['progress_json'])['failures'] == [{'date': '2026-01-01', 'error': 'RuntimeError'}]
        # The foreground job releases both locks even after partial failures.
        handle = sync._process_lock()
        handle.close()
        assert sync._lock.acquire(blocking=False)
        sync._lock.release()

    def test_cli_activities_uses_sync_and_stores_original_sets_and_payloads(self):
        client = self.client()
        client.get_activities_by_date.return_value = [{'activityId': 7, 'startTimeLocal': '2026-01-01 10:00:00'}]
        client.get_activity_exercise_sets.return_value = {'exerciseSets': [{'repetitionCount': 8}]}
        client.get_activity.return_value = {'activityId': 7}
        client.get_activity_splits.return_value = {'lapDTOs': []}
        client.get_activity_details.return_value = {'metricDescriptors': [], 'activityDetailMetrics': []}
        result = self.invoke(['activities', '--start', '2026-01-01', '--end', '2026-01-01', '--enrich'], client)
        assert result.exit_code == 0, result.output
        activity = store.get_activity(7, include=['raw'])
        assert activity['garmin_sets_response'] == client.get_activity_exercise_sets.return_value
        assert {p['kind'] for p in activity['available_payloads']} == {'summary', 'detail', 'splits', 'samples'}
        client.get_sleep_data.assert_not_called()

    @pytest.mark.parametrize('args', (['fetch', '--days', '0'], ['fetch', '--start', '2026-02-30'],
                         ['fetch', '--start', '2026-02-02', '--end', '2026-02-01']))
    def test_invalid_cli_range_fails_before_authentication(self, args):
        with patch.object(auth, 'get_credentials') as credentials:
            result = CliRunner().invoke(cli.app, args)
            assert result.exit_code != 0
            credentials.assert_not_called()

    def test_legacy_lag_columns_are_retained_but_not_updated(self):
        with connection.connect() as conn:
            conn.execute('ALTER TABLE daily_metrics ADD COLUMN stress_avg_lag1 REAL')
            conn.execute("INSERT INTO daily_metrics(date,stress_avg_lag1) VALUES('2026-01-01',42)")
        store.initialize()
        db.upsert_rows([{'date': '2026-01-01', 'sleep_score': 90}])
        assert db.load_all()[0]['sleep_score'] == 90
        with connection.connect() as conn:
            assert conn.execute('SELECT stress_avg_lag1 FROM daily_metrics').fetchone()[0] == 42
