"""End-to-end OAuth HTTP flow against temporary SQLite storage."""
from contextlib import ExitStack
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from html.parser import HTMLParser
import os
import secrets
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import TokenError
from starlette.testclient import TestClient
from afterset import connection, store
from afterset.server import create_server
from afterset.oauth import digest

PASSPHRASE = 'test-only-owner-passphrase-not-a-real-secret'
ORIGIN = 'https://afterset.example'
RESOURCE = ORIGIN + '/mcp'
CALLBACK = 'https://client.example/callback'


class Fields(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.values = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('type') == 'hidden':
            self.values[values['name']] = values['value']


class TestOAuth:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with ExitStack() as stack:
            setting = patch.object(connection, 'DB_PATH', tmp_path / 'records.db')
            stack.enter_context(setting)
            environment = patch.dict(os.environ, {'AFTERSET_OAUTH_PASSPHRASE': PASSPHRASE, 'AFTERSET_PUBLIC_URL': RESOURCE}, clear=True)
            stack.enter_context(environment)
            store.initialize()
            self.server = create_server('streamable-http', host='0.0.0.0')
            self.client = TestClient(self.server.streamable_http_app(), base_url=ORIGIN, follow_redirects=False)
            stack.enter_context(self.client)
            yield

    def register(self, method='none', **extra):
        response = self.client.post('/register', json={
            'redirect_uris': [CALLBACK], 'client_name': 'Phone connector',
            'token_endpoint_auth_method': method, **extra,
        })
        assert response.status_code == 201, response.text
        return response.json()

    def consent_form(self, client, **extra):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        response = self.client.get('/authorize', params={
            'client_id': client['client_id'], 'response_type': 'code', 'redirect_uri': CALLBACK,
            'scope': 'afterset', 'state': 'roundtrip-state', 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'resource': RESOURCE, **extra,
        })
        assert response.status_code == 302, response.text
        assert urlsplit(response.headers['location']).hostname == 'afterset.example'
        page = self.client.get(response.headers['location'])
        assert page.status_code == 200, page.text
        assert 'no-store' in page.headers['cache-control']
        assert "frame-ancestors 'none'" in page.headers['content-security-policy']
        assert 'HttpOnly' in page.headers['set-cookie']
        assert page.headers['referrer-policy'] == 'same-origin'
        assert "form-action 'self' https://client.example;" in page.headers['content-security-policy']
        return {**Fields(page.text).values, 'decision': 'approve', 'passphrase': PASSPHRASE}, verifier

    def grant(self, client):
        form, verifier = self.consent_form(client)
        response = self.client.post('/oauth/consent', data=form, headers={'Origin': ORIGIN})
        assert response.status_code == 303, response.text
        callback = urlsplit(response.headers['location'])
        assert callback.hostname == 'client.example'
        query = parse_qs(callback.query)
        assert query['state'] == ['roundtrip-state']
        assert query['iss'] == [ORIGIN + '/']
        return {'grant_type': 'authorization_code', 'code': query['code'][0],
                'code_verifier': verifier, 'redirect_uri': CALLBACK,
                'client_id': client['client_id'], 'resource': RESOURCE,
                **({'client_secret': client['client_secret']} if client.get('client_secret') else {})}

    def tokens(self, client):
        response = self.client.post('/token', data=self.grant(client))
        assert response.status_code == 200, response.text
        return response.json()

    def refresh_form(self, client, tokens):
        return {'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                'client_id': client['client_id'], 'resource': RESOURCE,
                **({'client_secret': client['client_secret']} if client.get('client_secret') else {})}

    def mcp(self, token):
        return self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'data_coverage', 'arguments': {}}},
                                headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'})

    def test_discovery_registration_pkce_consent_and_authenticated_mcp(self):
        denied = self.client.post('/mcp', json={})
        assert denied.status_code == 401
        assert '/.well-known/oauth-protected-resource/mcp' in denied.headers['www-authenticate']
        resource = self.client.get('/.well-known/oauth-protected-resource/mcp').json()
        assert resource['resource'] == RESOURCE
        assert resource['authorization_servers'] == [ORIGIN + '/']
        metadata = self.client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert 'none' in metadata['token_endpoint_auth_methods_supported']
        assert metadata['registration_endpoint'] == ORIGIN + '/register'
        client = self.register()
        request = self.grant(client)
        mismatched = self.client.post('/token', data={**request, 'redirect_uri': 'https://attacker.example/callback'})
        assert mismatched.status_code == 400
        invalid = self.client.post('/token', data={**request, 'code_verifier': 'wrong'})
        assert invalid.status_code == 400
        assert invalid.json()['error'] == 'invalid_grant'
        bad_resource = self.client.post('/token', data={**request, 'resource': 'https://other.example/mcp'})
        assert bad_resource.status_code == 400
        assert bad_resource.json()['error'] == 'invalid_target'
        response = self.client.post('/token', data=request)
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens['scope'] == 'afterset'
        assert self.mcp(tokens['access_token']).status_code == 200
        assert self.client.post('/token', data=request).json()['error'] == 'invalid_grant'
        with connection.connect() as conn:
            saved = '\n'.join(str(tuple(row)) for table in ('oauth_settings', 'oauth_clients', 'oauth_pending', 'oauth_codes', 'oauth_tokens') for row in conn.execute(f'SELECT * FROM {table}'))
        for secret in (PASSPHRASE, tokens['access_token'], tokens['refresh_token'], request['code']):
            assert secret not in saved

    def test_confidential_clients_and_client_binding(self):
        client = self.register('client_secret_post')
        tokens = self.tokens(client)
        other = self.register()
        stolen = self.client.post('/token', data={**self.refresh_form(other, tokens)})
        assert stolen.status_code == 400
        assert self.mcp(tokens['access_token']).status_code == 200
        wrong_secret = self.client.post('/token', data={**self.refresh_form(client, tokens), 'client_secret': 'wrong'})
        assert wrong_secret.status_code == 401
        with connection.connect() as conn:
            row = conn.execute('SELECT document FROM oauth_clients WHERE client_id=?', (client['client_id'],)).fetchone()
        assert client['client_secret'].encode() not in row[0]

    def test_standard_basic_authentication_without_body_client_credentials(self):
        client = self.register('client_secret_basic')
        request = self.grant(client)
        request.pop('client_id')
        request.pop('client_secret')
        basic = base64.b64encode((client['client_id'] + ':' + client['client_secret']).encode()).decode()
        response = self.client.post('/token', data=request, headers={'Authorization': 'Basic ' + basic})
        assert response.status_code == 200, response.text
        tokens = response.json()
        form = self.refresh_form(client, tokens)
        form.pop('client_id')
        form.pop('client_secret')
        refreshed = self.client.post('/token', data=form, headers={'Authorization': 'Basic ' + basic})
        assert refreshed.status_code == 200, refreshed.text

    def test_canonical_public_url_preserves_existing_grants(self):
        tokens = self.tokens(self.register())
        with patch.dict(os.environ, {'AFTERSET_PUBLIC_URL': 'https://AFTERSET.EXAMPLE:443/mcp'}):
            server = create_server('streamable-http', host='0.0.0.0')
        with TestClient(server.streamable_http_app(), base_url=ORIGIN) as http:
            response = http.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/list'},
                                 headers={'Authorization':'Bearer ' + tokens['access_token'], 'Accept':'application/json, text/event-stream'})
            assert response.status_code == 200, response.text

    def test_refresh_rotation_replay_revokes_family_and_revocation(self):
        client = self.register()
        original = self.tokens(client)
        refresh = self.refresh_form(client, original)
        response = self.client.post('/token', data=refresh)
        assert response.status_code == 200, response.text
        rotated = response.json()
        assert original['refresh_token'] != rotated['refresh_token']
        assert self.mcp(original['access_token']).status_code == 401
        assert self.mcp(rotated['access_token']).status_code == 200
        assert self.client.post('/token', data=refresh).status_code == 400
        assert self.mcp(rotated['access_token']).status_code == 401
        assert self.client.post('/token', data=self.refresh_form(client, rotated)).status_code == 400
        new = self.tokens(client)
        revoked = self.client.post('/revoke', data={'client_id': client['client_id'], 'token': new['refresh_token']})
        assert revoked.status_code == 200, revoked.text
        assert self.mcp(new['access_token']).status_code == 401

    def test_restart_persists_clients_tokens_and_owner_change_revokes(self):
        client = self.register('client_secret_post')
        tokens = self.tokens(client)
        restarted = create_server('streamable-http', host='0.0.0.0')
        with TestClient(restarted.streamable_http_app(), base_url=ORIGIN) as http:
            response = http.post('/token', data=self.refresh_form(client, tokens))
            assert response.status_code == 200, response.text
        with patch.dict(os.environ, {'AFTERSET_OAUTH_PASSPHRASE': PASSPHRASE + '-rotated'}):
            create_server('streamable-http', host='0.0.0.0')
        assert self.mcp(response.json()['access_token']).status_code == 401

    def test_consent_passphrase_csrf_origin_denial_and_rate_limit(self):
        client = self.register()
        form, _ = self.consent_form(client)
        assert self.client.post('/oauth/consent', data={**form, 'csrf': 'wrong'}).status_code == 403
        assert self.client.post('/oauth/consent', data=form, headers={'Origin': 'https://attacker.example'}).status_code == 403
        assert self.client.post('/oauth/consent', data=form, headers={'Origin': 'null'}).status_code == 403
        assert self.client.post('/oauth/consent', data={**form, 'passphrase': 'wrong'}).status_code == 401
        self.client.cookies.clear()
        assert self.client.post('/oauth/consent', data=form).status_code == 403
        form, _ = self.consent_form(client)
        denial = self.client.post('/oauth/consent', data={**form, 'decision': 'deny', 'passphrase': ''})
        assert parse_qs(urlsplit(denial.headers['location']).query)['error'] == ['access_denied']
        assert self.client.post('/oauth/consent', data=form).status_code == 400
        form, _ = self.consent_form(client)
        for _ in range(9):
            assert self.client.post('/oauth/consent', data={**form, 'passphrase': 'wrong'}).status_code == 401
        # Limit survives provider/server reconstruction; a new request cannot reset it.
        create_server('streamable-http', host='0.0.0.0')
        assert self.client.post('/oauth/consent', data=form).status_code == 429

    def test_redirects_scopes_expiry_and_public_input_limits(self):
        response = self.client.post('/register', json={'redirect_uris': ['http://attacker.example/cb']})
        assert response.status_code == 400
        client = self.register(client_name='<script>alert("x")</script>')
        form, _ = self.consent_form(client)
        page = self.client.get('/oauth/consent', params={'request': form['request']})
        assert '<script>' not in page.text
        with connection.connect() as conn:
            conn.execute('UPDATE oauth_pending SET expires=0')
        assert self.client.post('/oauth/consent', data=form).status_code == 400
        params = {'client_id': client['client_id'], 'response_type': 'code', 'redirect_uri': 'https://attacker.example/cb',
                  'code_challenge': 'x'*43, 'code_challenge_method': 'S256'}
        assert self.client.get('/authorize', params=params).status_code == 400
        params['redirect_uri'] = CALLBACK
        params['resource'] = 'https://other.example/mcp'
        result = self.client.get('/authorize', params=params)
        assert parse_qs(urlsplit(result.headers['location']).query)['error'] == ['invalid_request']
        assert self.client.post('/register', content='x'*16385).status_code == 413
        assert self.client.post('/register', content='{broken').status_code == 400
        assert self.client.get('/.well-known/oauth-authorization-server', headers={'Host': 'attacker.example'}).status_code == 421
        tokens = self.tokens(client)
        with connection.connect() as conn:
            conn.execute("UPDATE oauth_tokens SET expires=0 WHERE kind='access'")
        assert self.mcp(tokens['access_token']).status_code == 401

    def test_expired_code_refresh_and_scope_escalation_are_rejected(self):
        client = self.register()
        request = self.grant(client)
        with connection.connect() as conn:
            conn.execute('UPDATE oauth_codes SET expires=0')
        assert self.client.post('/token', data=request).status_code == 400
        tokens = self.tokens(client)
        form = self.refresh_form(client, tokens)
        escalation = self.client.post('/token', data={**form, 'scope': 'afterset admin'})
        assert escalation.json()['error'] == 'invalid_scope'
        with connection.connect() as conn:
            conn.execute("UPDATE oauth_tokens SET expires=0 WHERE kind='refresh'")
        assert self.client.post('/token', data=form).status_code == 400

    def test_authentication_configuration_fails_closed(self):
        with patch.dict(os.environ, {'AFTERSET_PUBLIC_URL': RESOURCE}, clear=True):
            with pytest.raises(ValueError, match='required for a public URL'):
                create_server('streamable-http', host='127.0.0.1')
        with patch.dict(os.environ, {'AFTERSET_OAUTH_PASSPHRASE': ''}):
            with pytest.raises(ValueError, match='at least 16 characters'):
                create_server('streamable-http')
            assert create_server('stdio').settings.auth is None
        with patch.dict(os.environ, {'AFTERSET_HTTP_TOKEN': 'x'*48}):
            with pytest.raises(ValueError, match='not both'):
                create_server('streamable-http')

    def test_registration_capacity_returns_oauth_error_without_inserting_client(self):
        registered = self.register()
        provider = self.server.owner_oauth
        with connection.connect() as conn:
            conn.executemany('INSERT INTO oauth_clients VALUES(?,?,?)', [
                (f'capacity-{index}', provider._cipher.encrypt(json.dumps({**registered, 'client_id': f'capacity-{index}'}).encode()), int(time.time()))
                for index in range(999)
            ])
        response = self.client.post('/register', json={'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'none'})
        assert response.status_code == 400, response.text
        assert response.json()['error'] == 'invalid_client_metadata'
        assert 'capacity' in response.json()['error_description']
        with connection.connect() as conn:
            assert conn.execute('SELECT count(*) FROM oauth_clients').fetchone()[0] == 1000

    def test_pending_capacity_redirects_with_temporarily_unavailable(self):
        client = self.register()
        self.consent_form(client)
        with connection.connect() as conn:
            row = conn.execute('SELECT client_id,params,expires FROM oauth_pending').fetchone()
            conn.executemany('INSERT INTO oauth_pending(id,client_id,params,expires) VALUES(?,?,?,?)',
                             [(f'capacity-{index}', *tuple(row)) for index in range(999)])
        response = self.client.get('/authorize', params={
            'client_id': client['client_id'], 'redirect_uri': CALLBACK, 'response_type': 'code',
            'scope': 'afterset', 'state': 'capacity-state', 'code_challenge': 'x'*43,
            'code_challenge_method': 'S256', 'resource': RESOURCE,
        })
        assert response.status_code == 302, response.text
        callback = urlsplit(response.headers['location'])
        assert callback.hostname == 'client.example'
        query = parse_qs(callback.query)
        assert query['error'] == ['temporarily_unavailable']
        assert query['state'] == ['capacity-state']
        with connection.connect() as conn:
            assert conn.execute('SELECT count(*) FROM oauth_pending').fetchone()[0] == 1000

    @pytest.mark.parametrize('condition', ('consumed', 'expired'))
    def test_code_consumed_or_expired_after_loading_returns_invalid_grant(self, condition):
        provider = self.server.owner_oauth
        client = self.register()
        original_load = provider.load_authorization_code
        request = self.grant(client)
        async def load_then_invalidate(client_info, code):
            loaded = await original_load(client_info, code)
            assert loaded is not None
            with connection.connect() as conn:
                if condition == 'consumed':
                    conn.execute('DELETE FROM oauth_codes WHERE id=?', (digest(code),))
                else:
                    conn.execute('UPDATE oauth_codes SET expires=0 WHERE id=?', (digest(code),))
            return loaded
        with patch.object(provider, 'load_authorization_code', side_effect=load_then_invalidate):
            response = self.client.post('/token', data=request)
        assert response.status_code == 400, response.text
        assert response.json()['error'] == 'invalid_grant'
        with connection.connect() as conn:
            assert conn.execute('SELECT count(*) FROM oauth_tokens').fetchone()[0] == 0

    def test_preloaded_code_concurrent_exchange_issues_one_pair(self):
        provider = self.server.owner_oauth
        client = self.register()
        request = self.grant(client)
        client_info = asyncio.run(provider.get_client(client['client_id']))
        code = asyncio.run(provider.load_authorization_code(client_info, request['code']))
        assert code is not None
        def exchange(_):
            try:
                asyncio.run(provider.exchange_authorization_code(client_info, code))
                return 'issued'
            except TokenError as error:
                return error.error
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(exchange, range(2)))
        assert sorted(results) == ['invalid_grant', 'issued']
        with connection.connect() as conn:
            assert conn.execute('SELECT count(*) FROM oauth_tokens').fetchone()[0] == 2
            assert conn.execute('SELECT count(DISTINCT family) FROM oauth_tokens').fetchone()[0] == 1

    def test_token_and_revocation_require_urlencoded_forms_before_consuming_grants(self):
        client = self.register()
        code_request = self.grant(client)
        for resource in (RESOURCE, 'https://other.example/mcp'):
            response = self.client.post('/token', files={key: (None, value) for key, value in {**code_request, 'resource': resource}.items()})
            assert response.status_code == 415, response.text
            assert response.json()['error'] == 'invalid_request'
            assert response.headers['cache-control'] == 'no-store'
        # Reject other encodings too; a valid code stays available afterwards.
        assert self.client.post('/token', json=code_request).status_code == 415
        assert self.client.post('/token', content='grant_type=authorization_code').status_code == 415
        assert self.client.post('/token', content='grant_type=authorization_code', headers={'Content-Type': 'text/plain'}).status_code == 415
        multipart = self.client.build_request('POST', '/token', files={
            key: (None, value) for key, value in {**code_request, 'resource': 'https://other.example/mcp'}.items()
        })
        multipart_type = multipart.headers['content-type']
        multipart_body = multipart.read()
        # The guard and Starlette must not choose different Content-Type headers.
        for types in ((multipart_type, 'application/x-www-form-urlencoded'),
                      ('application/x-www-form-urlencoded', multipart_type)):
            response = self.client.post('/token', content=multipart_body, headers=[('Content-Type', value) for value in types])
            assert response.status_code == 415, response.text
        response = self.client.post('/token', data=code_request, headers={'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})
        assert response.status_code == 200, response.text
        tokens = response.json()
        form = self.refresh_form(client, tokens)
        for resource in (RESOURCE, 'https://other.example/mcp'):
            response = self.client.post('/token', files={key: (None, value) for key, value in {**form, 'resource': resource}.items()})
            assert response.status_code == 415, response.text
            assert self.mcp(tokens['access_token']).status_code == 200
        response = self.client.post('/token', data=form)
        assert response.status_code == 200, response.text
        rotated = response.json()
        revoke = {'client_id': client['client_id'], 'token': rotated['refresh_token']}
        for encoding in ('multipart', 'json'):
            response = self.client.post('/revoke', **({'files': {key: (None, value) for key, value in revoke.items()}} if encoding == 'multipart' else {'json': revoke}))
            assert response.status_code == 415, response.text
            assert response.headers['cache-control'] == 'no-store'
            assert self.mcp(rotated['access_token']).status_code == 200
        assert self.client.post('/revoke', data=revoke).status_code == 200
        assert self.mcp(rotated['access_token']).status_code == 401

    def test_concurrent_refresh_has_one_winner_and_detects_reuse(self):
        client = self.register()
        tokens = self.tokens(client)
        form = self.refresh_form(client, tokens)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post('/token', data=form), range(2)))
        assert sorted(r.status_code for r in responses) == [200, 400]
        winner = next(r.json() for r in responses if r.status_code == 200)
        assert self.mcp(winner['access_token']).status_code == 401
