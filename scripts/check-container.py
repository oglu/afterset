#!/usr/bin/env python3
"""Smoke-test an Afterset image with synthetic data in a disposable Docker volume."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image', default='afterset:remote-test')
parser.add_argument('--auth', choices=['bearer', 'oauth'], default='bearer')
args = parser.parse_args()
name = 'afterset-check-' + uuid.uuid4().hex[:12]
volume = name + '-data'
credential = 'AFTERSET_OAUTH_PASSPHRASE' if args.auth == 'oauth' else 'AFTERSET_HTTP_TOKEN'
env = {**os.environ, credential: secrets.token_urlsafe(48)}


def docker(*parts, input=None):
    return subprocess.run(['docker', *parts], input=input, text=True, env=env,
                          capture_output=True, check=True).stdout.strip()


probe = '''
import asyncio, importlib.util, os, time, urllib.error, urllib.request
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
url = 'http://localhost:8080'
for attempt in range(50):
    try:
        with urllib.request.urlopen(url + '/healthz', timeout=1) as response:
            assert response.status == 200
        break
    except (OSError, urllib.error.URLError):
        time.sleep(0.2)
else:
    raise RuntimeError('Container failed readiness')
request = urllib.request.Request(url + '/mcp', data=b'{}', headers={'Content-Type': 'application/json'})
try:
    urllib.request.urlopen(request)
    raise AssertionError('MCP accepted an unauthenticated request')
except urllib.error.HTTPError as exc:
    assert exc.code == 401, exc.code
for package in ('pandas', 'seaborn', 'matplotlib'):
    assert importlib.util.find_spec(package) is None
async def check():
    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + os.environ['AFTERSET_HTTP_TOKEN']}) as client:
        async with streamable_http_client(url + '/mcp', http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {'training_summary', 'apply_proposal', 'get_personal_context', 'save_personal_context'} <= names
                assert 'propose_exercise_sets' not in names and 'apply_exercise_sets' not in names
                coverage = await session.call_tool('data_coverage', {})
                assert not coverage.isError
                assert coverage.structuredContent['coverage']['activities']['rows'] == 1
                summary = await session.call_tool('training_summary', {'start':'2026-01-01','end':'2026-01-07'})
                assert not summary.isError
                assert len(summary.structuredContent['activities']) == 1
                activity = await session.call_tool('get_activity', {'activity_id':1})
                assert not activity.isError
                assert 'garmin_sets_response' not in activity.structuredContent
asyncio.run(check())
print('Authenticated HTTP tools, compact summary and persisted synthetic record verified.')
'''

if args.auth == 'oauth':
    probe = Path(__file__).with_name('oauth-container-probe.py').read_text()

try:
    docker('volume', 'create', volume)
    docker('run', '-d', '--network', 'none', '--name', name,
           '--env', credential, '--env', 'AFTERSET_PUBLIC_URL=http://localhost:8080/mcp',
           '--volume', volume + ':/data', args.image)
    docker('exec', '-i', name, 'python', '-', input='''
import time, urllib.request
for attempt in range(50):
    try:
        urllib.request.urlopen('http://localhost:8080/healthz', timeout=1).close()
        break
    except OSError:
        time.sleep(0.2)
else:
    raise RuntimeError('Container failed readiness')
from afterset import db
db.upsert_activities([{'activity_id': 1, 'date': '2026-01-01', 'name': 'Container smoke test'}])
db.upsert_rows([{'date': '2026-01-02', 'sleep_score': 80, 'sleep_hrv': 50}])
''')
    print(docker('exec', '-i', name, 'python', '-', input=probe))
    docker('restart', name)
    print(docker('exec', '-i', name, 'python', '-', input=probe))
    print('Container restart preserved the test database. No personal data was mounted.')
except subprocess.CalledProcessError as exc:
    raise SystemExit(exc.stderr or exc.stdout) from None
finally:
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
