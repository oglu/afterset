"""SDK OAuth client smoke test; run inside a disposable test container only."""
import asyncio
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

URL = 'http://localhost:8080/mcp'
CACHE = Path('/data/test-oauth-client.json')


class Storage:
    def __init__(self):
        self.data = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    def save(self):
        CACHE.write_text(json.dumps(self.data))
        CACHE.chmod(0o600)

    async def get_client_info(self):
        return OAuthClientInformationFull.model_validate(self.data['client']) if self.data.get('client') else None

    async def set_client_info(self, value):
        self.data['client'] = value.model_dump(mode='json')
        self.save()

    async def get_tokens(self):
        return OAuthToken.model_validate(self.data['tokens']) if self.data.get('tokens') else None

    async def set_tokens(self, value):
        self.data['tokens'] = value.model_dump(mode='json')
        self.save()


class Fields(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.values = {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('type') == 'hidden':
            self.values[values['name']] = values['value']


async def main():
    for _ in range(50):
        try:
            with urllib.request.urlopen('http://localhost:8080/healthz', timeout=1):
                break
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.2)
    else:
        raise RuntimeError('Container failed readiness')
    storage = Storage()
    connected_before = bool(storage.data)
    result = {}
    async def browser(url):
        async with httpx.AsyncClient(follow_redirects=False) as client:
            authorize = await client.get(url)
            assert authorize.status_code == 302, authorize.text
            page = await client.get(authorize.headers['location'])
            assert page.status_code == 200
            consent = await client.post('http://localhost:8080/oauth/consent', data={
                **Fields(page.text).values, 'decision': 'approve',
                'passphrase': os.environ['AFTERSET_OAUTH_PASSPHRASE'],
            }, headers={'Origin': 'http://localhost:8080'})
            assert consent.status_code == 303, consent.text
            query = parse_qs(urlsplit(consent.headers['location']).query)
            result['code'], result['state'] = query['code'][0], query['state'][0]

    async def callback():
        return result['code'], result['state']

    auth = OAuthClientProvider(server_url=URL, client_metadata=OAuthClientMetadata(
        redirect_uris=['http://localhost:8081/callback'], token_endpoint_auth_method='none',
        client_name='Disposable container test', scope='afterset'), storage=storage,
        redirect_handler=browser, callback_handler=callback)
    async with httpx.AsyncClient(auth=auth) as client:
        async with streamable_http_client(URL, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {'get_personal_context', 'save_personal_context'} <= names
                summary = await session.call_tool('training_summary', {'start': '2026-01-01', 'end': '2026-01-07'})
                assert not summary.isError
                assert len(summary.structuredContent['activities']) == 1
    if connected_before:
        assert not result, 'Persisted client/token unexpectedly required another owner login'
    # Rotate through the public token endpoint and retain the new token pair for
    # the next process/container restart. Never print any credentials.
    original = await storage.get_tokens()
    info = await storage.get_client_info()
    async with httpx.AsyncClient() as client:
        refresh = await client.post('http://localhost:8080/token', data={
            'grant_type': 'refresh_token', 'client_id': info.client_id,
            'refresh_token': original.refresh_token, 'resource': URL,
        })
        assert refresh.status_code == 200, refresh.text
        rotated = OAuthToken.model_validate(refresh.json())
        assert rotated.refresh_token != original.refresh_token
        await storage.set_tokens(rotated)
    print('SDK OAuth discovery, PKCE, consent, MCP call and refresh rotation passed; persisted grant=' + str(connected_before))


asyncio.run(main())
