"""Synthetic stdio MCP server. Never connects to Garmin or a personal database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

# Model-provider credentials are never needed in the synthetic MCP child.
os.environ.pop('OPENAI_API_KEY', None)
root = Path(os.environ['AFTERSET_EVAL_ROOT']).resolve()
if not (root / 'fixture.json').is_file() or not (root / 'training.db').is_file():
    raise RuntimeError('A prepared synthetic fixture is required')
os.environ['AFTERSET_DB'] = str(root / 'training.db')
os.environ['AFTERSET_CONTEXT_DB'] = str(root / 'personal-context.db')
os.environ['AFTERSET_TOKEN_STORE'] = str(root / 'disabled-tokens')
os.environ['AFTERSET_TIMEZONE'] = 'UTC'

from afterset import db, store, sync
from afterset.server import create_server

mode = json.loads((root / 'fixture.json').read_text())['mode']
# Defense in depth: accidental real Garmin calls cannot obtain a client or save tokens.
def deny(*_args, **_kwargs):
    raise RuntimeError('Network access is disabled in the synthetic evaluator')
sync._client = deny
from afterset import auth
auth.save_session_tokens = deny
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

def _offline_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        return deny()
    return _original_connect(sock, address)
def _offline_connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        return deny()
    return _original_connect_ex(sock, address)

socket.socket.connect = _offline_connect
socket.socket.connect_ex = _offline_connect_ex
socket.getaddrinfo = deny


def fake_job(request):
    job_id = 'synthetic-' + uuid4().hex
    state = 'completed' if mode in ('fresh', 'missing') else 'partial' if mode == 'partial' else 'failed'
    progress = {'synthetic': True, 'failures': [{'error': 'SyntheticFailure'}] if state == 'partial' else []}
    if mode == 'auth':
        progress.update({'error': 'SyntheticAuthenticationError', 'hint': 'Synthetic authentication unavailable'})
    if state == 'completed' and mode == 'missing' and request.get('scope') == 'activities' and request.get('start', '') <= '2026-01-02' <= request.get('end', ''):
        db.upsert_activities([{'activity_id': 4101, 'date': '2026-01-02', 'name': 'Synthetic strength', 'duration_seconds': 600}])
        db.upsert_exercise_sets(4101, {'exerciseSets': [{'duration': 600, 'setType': 'ACTIVE'}]})
    with store.connect() as conn:
        conn.execute('INSERT INTO sync_jobs(job_id,state,request_json,progress_json) VALUES(?,?,?,?)',
                     (job_id, state, json.dumps(request), json.dumps(progress)))
    return {'job_id': job_id, 'state': 'queued', 'hint': 'Poll sync_status until terminal.'}


def fake_start(start, end, scope='activities', enrich=False, activity_ids=None):
    if activity_ids is not None:
        raise ValueError('Date range and activity IDs cannot be combined')
    store.date_range(start, end, 31)
    if scope not in ('activities', 'wellness'):
        raise ValueError('Invalid scope')
    return fake_job({'start': start, 'end': end, 'scope': scope, 'enrich': enrich})


def fake_enrichment(activity_ids, kinds=None, refresh=False, start=None, end=None):
    if start is not None or end is not None or not activity_ids or len(set(activity_ids)) != len(activity_ids):
        raise ValueError('Invalid synthetic enrichment request')
    with store.connect() as conn:
        for aid in activity_ids:
            store.require_activity(conn, aid)
    return fake_job({'activity_ids': activity_ids, 'kinds': kinds or ['sets', 'detail', 'splits', 'samples'],
                     'refresh': refresh, 'scope': 'activities', 'enrich': True})


def build_server():
    server = create_server()
    manager = server._tool_manager
    manager.get_tool('start_garmin_sync').fn = fake_start
    manager.get_tool('start_activity_enrichment').fn = fake_enrichment
    original = manager.call_tool
    async def audited(name, arguments, context=None, convert_result=False):
        event = {'tool': name, 'arguments': arguments, 'at': time.time()}
        try:
            result = await original(name, arguments, context=context, convert_result=convert_result)
            structured = result[1] if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict) else result
            event.update({'outcome': 'success', 'result': structured})
            return result
        except BaseException as exc:
            event.update({'outcome': 'error', 'error_type': type(exc).__name__})
            raise
        finally:
            with (root / 'calls.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, default=str, allow_nan=False) + '\n')
    manager.call_tool = audited
    return server


if __name__ == '__main__':
    build_server().run(transport='stdio')
