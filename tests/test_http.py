"""Real ASGI transport: reject unauthenticated calls and protect host/origin."""
from contextlib import ExitStack
import asyncio
import os
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from afterset import connection, store
from afterset.http_auth import BearerTokenVerifier
from afterset.server import create_server

TOKEN = 'test-only-token-0123456789abcdef0123456789abcdef'


class TestHttp:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            setting = patch.object(connection, 'DB_PATH', tmp_path / 'records.db')
            stack.enter_context(setting)
            environment = patch.dict(os.environ, {'AFTERSET_HTTP_TOKEN': TOKEN, 'AFTERSET_PUBLIC_URL': 'https://afterset.example/mcp'}, clear=True)
            stack.enter_context(environment)
            store.initialize()
            yield

    def test_bearer_http_tools_missing_invalid_and_valid_tokens(self):
        server = create_server('streamable-http', host='0.0.0.0', port=8080)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'data_coverage', 'arguments': {}}}
        headers = {'Accept': 'application/json, text/event-stream'}
        with TestClient(server.streamable_http_app(), base_url='https://afterset.example') as client:
            assert client.post('/mcp', json=request, headers=headers).status_code == 401
            assert client.post('/mcp', json=request, headers={**headers, 'Authorization': 'Bearer wrong-token'}).status_code == 401
            authorized = {**headers, 'Authorization': 'Bearer ' + TOKEN}
            response = client.post('/mcp', json=request, headers=authorized)
            assert response.status_code == 200, response.text
            # SDK may select SSE or JSON depending on response settings.
            import json
            data = response.json() if response.headers['content-type'].startswith('application/json') else json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: ')))
            assert data['result']['structuredContent']['coverage']['activities']['rows'] == 0
            assert TOKEN not in response.text
            assert client.get('/healthz').json() == {'status': 'ok'}
            assert client.post('/mcp', json=request, headers={**authorized, 'Host': 'attacker.example'}).status_code == 421
            assert client.post('/mcp', json=request, headers={**authorized, 'Origin': 'https://attacker.example'}).status_code == 403
            assert client.post('/mcp', json=request, headers={**authorized, 'Origin': 'https://afterset.example'}).status_code == 200

    def test_remote_requires_token_and_explicit_public_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match='TOKEN'):
                create_server('streamable-http', host='0.0.0.0')
            # Local HTTP can remain unauthenticated, as can stdio.
            assert create_server('streamable-http').settings.auth is None
            with patch.dict(os.environ, {'AFTERSET_HTTP_TOKEN': TOKEN}):
                with pytest.raises(ValueError, match='PUBLIC_URL'):
                    create_server('streamable-http', host='0.0.0.0')
            with patch.dict(os.environ, {'AFTERSET_HTTP_TOKEN': 'short'}):
                with pytest.raises(ValueError, match='32 characters'):
                    create_server('streamable-http')
                assert create_server('stdio').settings.auth is None

    @pytest.mark.parametrize('url', [
        'http://public.example/mcp',
        'https://public.example/wrong',
        'https://user:password@public.example/mcp',
    ])
    def test_invalid_public_url_is_rejected(self, url):
        with patch.dict(os.environ, {'AFTERSET_PUBLIC_URL': url}):
            with pytest.raises(ValueError):
                create_server('streamable-http')

    def test_railway_port_and_token_verification(self):
        with patch.dict(os.environ, {'AFTERSET_HTTP_TOKEN': TOKEN, 'RAILWAY_PUBLIC_DOMAIN': 'afterset.up.railway.app', 'AFTERSET_HOST': '0.0.0.0', 'PORT': '9123'}, clear=True):
            server = create_server('streamable-http')
            assert server.settings.port == 9123
            assert 'afterset.up.railway.app' in server.settings.transport_security.allowed_hosts
        async def check():
            verifier = BearerTokenVerifier(TOKEN)
            assert await verifier.verify_token('nope') is None
            assert (await verifier.verify_token(TOKEN)).client_id == 'afterset-owner'
        asyncio.run(check())
