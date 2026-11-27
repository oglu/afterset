"""Offline tests of the synthetic evaluation observer, never a live Codex run."""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scripts.conversation_eval.fixtures import initialize_fixture
from scripts.conversation_eval.scenarios import score
from scripts.conversation_eval.observer import snapshot


def test_synthetic_server_records_calls_and_fake_sync(tmp_path):
    root = tmp_path / 'synthetic'
    initialize_fixture(root, 'missing')
    async def run():
        env = {**os.environ, 'AFTERSET_EVAL_ROOT': str(root)}
        params = StdioServerParameters(command=sys.executable, args=['-m', 'scripts.conversation_eval.server'], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initial = await session.initialize()
                from afterset.server import INSTRUCTIONS, create_server
                assert initial.instructions == INSTRUCTIONS
                exposed = {tool.name: tool for tool in (await session.list_tools()).tools}
                baseline = create_server()._tool_manager._tools
                assert set(exposed) == set(baseline)
                for name, tool in exposed.items():
                    assert tool.description == baseline[name].description
                    assert tool.inputSchema == baseline[name].parameters
                result = await session.call_tool('start_garmin_sync', {'start': '2026-01-02', 'end': '2026-01-02', 'scope': 'activities'})
                assert not result.isError
                job = result.structuredContent['job_id']
                status = await session.call_tool('sync_status', {'job_id': job})
                assert status.structuredContent['state'] == 'completed'
                read_result = await session.call_tool('list_activities', {'start': '2026-01-02', 'end': '2026-01-02'})
                assert 'Synthetic strength' in str(read_result.structuredContent)
    asyncio.run(run())
    calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
    assert [c['tool'] for c in calls] == ['start_garmin_sync', 'sync_status', 'list_activities']
    assert all(c['outcome'] == 'success' for c in calls)
    assert calls[0]['result']['job_id'] == calls[1]['arguments']['job_id']
    assert calls[1]['result']['state'] == 'completed'
    assert score('missing_sync', calls, [])['checks']['sync_then_read'] == 'pass'
    assert snapshot(root)['counts']['sync_jobs'] == 1


def test_score_requires_order_and_never_marks_semantics_pass():
    calls = [{'tool': 'start_garmin_sync', 'outcome': 'success'}, {'tool': 'get_activity', 'outcome': 'success'},
             {'tool': 'sync_status', 'outcome': 'success'}]
    assert score('missing_sync', calls, [])['checks']['sync_then_read'] == 'fail'
    assert score('general', [], [])['overall'] == 'needs_semantic_review'
    assert score('no_save', [{'tool': 'save_session_note', 'outcome': 'error'}], [])['checks']['no_save'] == 'fail'
    assert score('notes_only', [{'tool': 'save_session_note', 'outcome': 'success'},
                                {'tool': 'propose_workout', 'outcome': 'error'}], [])['checks']['note_only'] == 'fail'


def test_rpc_rejects_server_requests_and_handles_early_completion(tmp_path):
    from scripts.conversation_eval.rpc import AppServer
    script = tmp_path / 'fake_rpc.py'
    script.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m.get("method") == "turn/start":
  print(json.dumps({"id":44,"method":"approval/request","params":{}}),flush=True)
  print(json.dumps({"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed"}}}),flush=True)
  print(json.dumps({"id":m["id"],"result":{"turn":{"id":"u"}}}),flush=True)
 elif m.get("id") == 44:
  assert "error" in m
''')
    client = AppServer([sys.executable, str(script)], cwd=str(tmp_path), env=os.environ.copy())
    try:
        response = client.request('turn/start', {}, 2)
        assert response['turn']['id'] == 'u'
        assert client.wait_turn('t', 'u', 2)['status'] == 'completed'
    finally:
        client.close()


def test_approval_gate_requires_visible_draft_and_no_early_apply():
    from scripts.conversation_eval.scenarios import ready_for_approval
    ref = [{'activity_id': 4101, 'index': 0}]
    evidence = {'reps': {'source': 'user'}}
    workout = {'blocks': [{'kind': 'circuit', 'efforts': [{'exercise': 'Squat', 'reps': 10, 'segments': ref, 'provenance': evidence},
                                       {'exercise': 'Row', 'reps': 8, 'segments': ref, 'provenance': evidence}]}]}
    draft = [{'tool': 'propose_workout', 'outcome': 'success', 'turn': 0, 'result': {'activity_id': 4101, 'proposal_id': 'p', 'workout': workout}}]
    before = {'counts': {'set_revisions': 0}}
    after = {'counts': {'set_revisions': 0}, 'proposals': [{'proposal_id': 'p', 'activity_id': 4101, 'applied_revision': None, 'workout': workout}]}
    assert ready_for_approval(draft, 'Review: squat 10, row 8.', before, after)
    from copy import deepcopy
    extra = deepcopy(draft)
    extra[0]['result']['workout']['blocks'][0]['efforts'].append({'exercise': 'Invented'})
    altered = deepcopy(after)
    altered['proposals'][0]['workout'] = extra[0]['result']['workout']
    assert not ready_for_approval(extra, 'Review: squat 10, row 8.', before, altered)
    assert not ready_for_approval(draft, 'Draft ready.', before, after)
    assert not ready_for_approval(draft + [{'tool': 'apply_proposal', 'outcome': 'error', 'turn': 0}], 'squat 10, row 8', before, after)


def test_sync_scorer_requires_matching_terminal_job_and_exact_scope():
    start = {'tool': 'start_garmin_sync', 'outcome': 'success', 'arguments': {'start': '2026-01-02', 'end': '2026-01-02', 'scope': 'activities'}, 'result': {'job_id': 'a'}}
    poll = {'tool': 'sync_status', 'outcome': 'success', 'arguments': {'job_id': 'b'}, 'result': {'state': 'completed'}}
    read = {'tool': 'list_activities', 'outcome': 'success'}
    assert score('missing_sync', [start, poll, read], [])['checks']['sync_then_read'] == 'fail'
    poll['arguments']['job_id'] = 'a'
    poll['result']['state'] = 'running'
    assert score('missing_sync', [start, poll, read], [])['checks']['sync_then_read'] == 'fail'
    poll['result']['state'] = 'completed'
    start['arguments']['scope'] = 'wellness'
    assert score('missing_sync', [start, poll, read], [])['checks']['sync_then_read'] == 'fail'


def test_trace_requires_complete_matching_mcp_and_rejects_other_tools():
    from scripts.conversation_eval.runner import verify_trace
    import pytest
    event = {'method': 'item/completed', 'params': {'threadId': 't', 'turnId': 'u',
             'item': {'type': 'mcpToolCall', 'server': 'afterset', 'tool': 'get_activity', 'status': 'completed'}}}
    verify_trace([event], 't', 'u', [{'tool': 'get_activity'}])
    with pytest.raises(RuntimeError, match='differ'):
        verify_trace([event], 't', 'u', [])
    event['params']['item']['type'] = 'commandExecution'
    with pytest.raises(RuntimeError, match='execution'):
        verify_trace([event], 't', 'u', [])
    event['params']['item']['type'] = 'unknownNewTool'
    with pytest.raises(RuntimeError, match='unknown item'):
        verify_trace([event], 't', 'u', [])


def test_runner_disables_all_globals_uses_random_alias_and_scrubs_environment(tmp_path, monkeypatch):
    from scripts.conversation_eval import runner
    monkeypatch.setattr(runner, 'disabled_user_servers', lambda: ['afterset', 'other'])
    monkeypatch.setenv('AFTERSET_DB', '/private/owner.db')
    monkeypatch.setenv('GARMIN_PASSWORD', 'private')
    args = runner.command('codex', tmp_path, tmp_path, 'afterset_eval_123')
    assert 'mcp_servers.afterset.enabled=false' in args
    assert 'mcp_servers.other.enabled=false' in args
    assert 'mcp_servers.afterset_eval_123.enabled=true' in args
    assert 'mcp_servers.afterset_eval_123.default_tools_approval_mode="approve"' in args
    assert sum('default_tools_approval_mode' in arg for arg in args) == 1
    assert 'GARMIN_PASSWORD' not in runner.child_environment()
    assert 'AFTERSET_DB' not in runner.child_environment()


def test_ambiguous_opening_fails_on_any_tool_and_unrelated_read_does_not_pass():
    assert score('ambiguous', [{'tool': 'list_activities', 'outcome': 'success'}], [])['checks']['no_tools'] == 'fail'
    wrong = {'tool': 'list_activities', 'outcome': 'success', 'arguments': {'start': '2026-01-09', 'end': '2026-01-09'}, 'result': {'items': [{'activity_id': 4102}]}}
    assert score('english', [wrong], [])['checks']['read'] == 'fail'


def test_recall_requires_applied_revision_notes_and_other_activity_across_reads():
    from scripts.conversation_eval.scenarios import recall_evidence
    workout = {'blocks': [{'kind': 'circuit', 'efforts': [{'exercise': 'Squat', 'reps': 10}, {'exercise': 'Row', 'reps': 8}]}]}
    calls = [
        {'turn': 2, 'outcome': 'success', 'tool': 'get_workout', 'arguments': {'activity_id': 4101}, 'result': {'activity_id': 4101, 'revision': 5, 'workout': workout}},
        {'turn': 2, 'outcome': 'success', 'tool': 'get_activity', 'arguments': {'activity_id': 4101}, 'result': {'activity': {'activity_id': 4101}, 'revision': 5, 'notes': [{'note': 'Synthetic feedback: felt steady.'}]}},
        {'turn': 2, 'outcome': 'success', 'tool': 'get_activity', 'arguments': {'activity_id': 4102}, 'result': {'activity': {'activity_id': 4102}}},
    ]
    assert recall_evidence(calls, 5)
    assert not recall_evidence(calls, 6)
    assert not recall_evidence(calls[:2], 5)
    calls[1]['result']['notes'] = [{'note': 'No feedback available.'}]
    assert not recall_evidence(calls, 5)


def test_fresh_cases_forbid_unneeded_sync_and_persistence():
    assert score('english', [{'tool': 'start_garmin_sync', 'outcome': 'error'}], [])['checks']['no_sync'] == 'fail'
    assert score('greek', [{'tool': 'propose_workout', 'outcome': 'error'}], [])['checks']['no_save'] == 'fail'


def test_cli_reports_deterministic_failure_with_nonzero_exit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts.conversation_eval import runner
    monkeypatch.setattr(runner.shutil, 'which', lambda _: '/fake/codex')
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='codex-cli 0.165.0\n'))
    monkeypatch.setattr(runner, 'run_case', lambda *a: {'assessment': {'overall': 'fail'}})
    report = tmp_path / 'report.json'
    result = runner.main(['--live', '--case', 'english', '--model', 'synthetic-model', '--repeat', '1', '--output', str(report)])
    assert result == 1
    assert json.loads(report.read_text())['runs'][0]['assessment']['overall'] == 'fail'


def test_synthetic_server_blocks_inet_connections_without_calling_garmin(tmp_path):
    import subprocess
    root = tmp_path / 'fixture'
    initialize_fixture(root, 'fresh')
    code = '''
import socket
import scripts.conversation_eval.server
for operation in (lambda: socket.socket(socket.AF_INET).connect(('127.0.0.1', 1)),
                  lambda: socket.socket(socket.AF_INET).connect_ex(('127.0.0.1', 1)),
                  lambda: socket.getaddrinfo('example.invalid', 443)):
    try: operation()
    except RuntimeError: pass
    else: raise AssertionError('network access was not blocked')
'''
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'AFTERSET_EVAL_ROOT': str(root)}, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()


def test_saved_structure_requires_applied_snapshot_head_and_exact_circuit():
    from scripts.conversation_eval.scenarios import structured_correct
    provenance = {'reps': {'source': 'user'}}
    ref = [{'activity_id': 4101, 'index': 0}]
    workout = {'blocks': [{'kind': 'circuit', 'efforts': [
        {'exercise': 'Squat', 'reps': 10, 'segments': ref, 'provenance': provenance},
        {'exercise': 'Row', 'reps': 8, 'segments': ref, 'provenance': provenance}]}]}
    assert structured_correct(workout)
    base = {'counts': {'session_notes': 1, 'set_proposals': 0, 'set_revisions': 0, 'sync_jobs': 2}}
    drafted = {'counts': {'session_notes': 2, 'set_proposals': 1, 'set_revisions': 0, 'sync_jobs': 2}}
    after = {'counts': {'session_notes': 2, 'set_proposals': 1, 'set_revisions': 1, 'sync_jobs': 2},
             'proposals': [{'proposal_id': 'p', 'activity_id': 4101, 'applied_revision': 5, 'workout': workout}],
             'heads': {4101: 5}, 'versions': [{'activity_id': 4101, 'revision': 5, 'workout': workout}]}
    calls = [{'tool': 'propose_workout', 'outcome': 'success', 'turn': 0, 'result': {'proposal_id': 'p'}},
             {'tool': 'apply_proposal', 'outcome': 'success', 'turn': 1, 'arguments': {'proposal_id': 'p'}, 'result': {'revision': 5}}]
    assert score('reconstruct', calls, ['review'], [base, drafted, after])['checks']['saved_structure'] == 'pass'
    after['heads'][4101] = 6
    assert score('reconstruct', calls, ['review'], [base, drafted, after])['checks']['saved_structure'] == 'fail'
    workout['blocks'][0]['efforts'].append({'exercise': 'Invented'})
    assert not structured_correct(workout)


def test_fresh_fixture_has_recent_available_payloads(tmp_path):
    from unittest.mock import patch
    from afterset import connection
    from afterset.store.payloads import enrichment_status
    root = tmp_path / 'fresh'
    initialize_fixture(root, 'fresh')
    with patch.object(connection, 'DB_PATH', root / 'training.db'):
        status = enrichment_status(4101)
    assert {item['kind'] for item in status['stored']} >= {'sets', 'detail', 'splits', 'samples'}
    assert all(item['outcome'] == 'success' for item in status['latest_outcomes'])
