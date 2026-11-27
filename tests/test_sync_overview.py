"""Offline checks for the read-only sync discovery surface."""
import json
from unittest.mock import patch

import pytest

from afterset import connection, store, sync
from afterset.server import create_server


def test_overview_distinguishes_active_attempts_and_coverage(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        jobs = [
            ('success', 'completed', {'start': '2026-01-01', 'end': '2026-01-07', 'scope': 'wellness', 'enrich': False}, {}),
            ('partial', 'partial', {'start': '2026-01-03', 'end': '2026-01-05', 'scope': 'wellness', 'enrich': False}, {'failures': [{'date': '2026-01-04'}]}),
            ('other', 'completed', {'start': '2026-01-01', 'end': '2026-01-07', 'scope': 'activities', 'enrich': False}, {}),
            ('active', 'running', {'activity_ids': [42], 'kinds': ['samples'], 'scope': 'activities', 'refresh': False, 'enrich': True}, {}),
            ('older', 'failed', {'start': '2025-12-01', 'end': '2025-12-02', 'scope': 'wellness', 'enrich': False}, {}),
        ]
        with store.connect() as conn:
            for job_id, state, request, progress in jobs:
                conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)',
                             (job_id, state, json.dumps(request), json.dumps(progress)))
        with patch.object(sync, 'start_sync', side_effect=AssertionError('read started sync')):
            result = sync.sync_overview('2026-01-02', '2026-01-06', 'wellness')
        by_id = {job['job_id']: job for job in result['jobs']}
        assert set(by_id) == {'active', 'partial', 'success'}
        assert by_id['success']['covers_range'] is True
        assert by_id['partial']['covers_range'] is False
        assert by_id['partial']['progress']['failures'] == [{'date': '2026-01-04'}]
        assert by_id['active']['request']['activity_ids'] == [42]
        assert by_id['active']['covers_range'] is False
        assert not result['truncated'] and 'not proof' in result['note']
        assert sync.job_status('active')['state'] == 'running'
        with pytest.raises(ValueError):
            sync.sync_overview('2026-01-01', '2026-02-02', 'wellness')
        with pytest.raises(ValueError):
            sync.sync_overview('2026-01-01', '2026-01-02', 'invalid')


def test_overview_marks_truncation_and_keeps_active_job_visible(tmp_path):
    with patch.object(connection, 'DB_PATH', tmp_path / 'synthetic.db'):
        store.initialize()
        request = json.dumps({'start': '2026-01-01', 'end': '2026-01-02', 'scope': 'activities'})
        with store.connect() as conn:
            conn.execute("INSERT INTO sync_jobs(job_id,state,request_json,progress_json,created_at) VALUES('active','running',?,'{}','2025-01-01')", (request,))
            conn.executemany("INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,'failed',?,'{}')", [(str(i), request) for i in range(105)])
        result = sync.sync_overview('2026-01-01', '2026-01-02', 'activities')
        assert result['truncated'] and len(result['jobs']) == 100
        assert result['jobs'][0]['job_id'] == 'active'


def test_tool_is_registered_read_only():
    tool = create_server()._tool_manager.get_tool('sync_overview')
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.openWorldHint is False
