"""Single-user OAuth or bearer authentication and HTTP transport settings.

Bearer mode accepts a pre-shared secret. OAuth mode uses the persistent owner
provider and the SDK authorization-code, discovery, registration and PKCE hooks.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}


class BearerTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        if len(token) < 32 or token.strip() != token or not token.isascii():
            raise ValueError('AFTERSET_HTTP_TOKEN must be an ASCII token of at least 32 characters without surrounding whitespace')
        self._digest = hashlib.sha256(token.encode()).digest()

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).digest()
        if not secrets.compare_digest(digest, self._digest):
            return None
        return AccessToken(token=token, client_id='afterset-owner', scopes=['afterset'])


def http_settings(host: str | None = None, port: int | None = None) -> dict:
    host = host or os.environ.get('AFTERSET_HOST', '127.0.0.1')
    port = port if port is not None else int(os.environ.get('AFTERSET_PORT', os.environ.get('PORT', '8765')))
    if not 1 <= port <= 65535:
        raise ValueError('HTTP port must be 1..65535')
    token = os.environ.get('AFTERSET_HTTP_TOKEN')
    passphrase = os.environ.get('AFTERSET_OAUTH_PASSPHRASE')
    if token is not None and passphrase is not None:
        raise ValueError('Choose AFTERSET_OAUTH_PASSPHRASE or AFTERSET_HTTP_TOKEN, not both')
    verifier = BearerTokenVerifier(token) if token is not None else None
    if host not in LOOPBACK_HOSTS and verifier is None and passphrase is None:
        raise ValueError('AFTERSET_OAUTH_PASSPHRASE or AFTERSET_HTTP_TOKEN is required beyond loopback')
    public_url = os.environ.get('AFTERSET_PUBLIC_URL')
    if not public_url and os.environ.get('RAILWAY_PUBLIC_DOMAIN'):
        public_url = 'https://' + os.environ['RAILWAY_PUBLIC_DOMAIN'] + '/mcp'
    if not public_url:
        if host not in LOOPBACK_HOSTS:
            raise ValueError('Set AFTERSET_PUBLIC_URL to the externally reachable /mcp URL')
        public_url = f'http://localhost:{port}/mcp'
    url = urlsplit(public_url)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.path != '/mcp' or url.query or url.fragment):
        raise ValueError('AFTERSET_PUBLIC_URL must be an absolute HTTP(S) URL ending in /mcp')
    if url.scheme != 'https' and url.hostname not in LOOPBACK_HOSTS:
        raise ValueError('AFTERSET_PUBLIC_URL must use HTTPS outside loopback')
    if url.hostname not in LOOPBACK_HOSTS and verifier is None and passphrase is None:
        raise ValueError('AFTERSET_OAUTH_PASSPHRASE or AFTERSET_HTTP_TOKEN is required for a public URL')
    public_url = str(AnyHttpUrl(public_url))
    url = urlsplit(public_url)
    origin = f'{url.scheme}://{url.netloc}'
    options = {
        'host': host, 'port': port, 'token_verifier': verifier,
        # Bearer-only mode intentionally advertises no OAuth discovery.
        'auth': AuthSettings(issuer_url=origin, resource_server_url=None, required_scopes=['afterset']) if verifier else None,
        'transport_security': TransportSecuritySettings(
            allowed_hosts=['127.0.0.1:*', 'localhost:*', '[::1]:*', url.netloc],
            allowed_origins=[origin],
        ),
    }

    if passphrase is not None:
        from afterset.oauth import OwnerOAuthProvider
        provider = OwnerOAuthProvider(passphrase, public_url)
        options['auth_server_provider'] = provider
        options.pop('token_verifier')
        options['auth'] = AuthSettings(
            issuer_url=provider.issuer, resource_server_url=public_url,
            validate_token_resource=True, required_scopes=['afterset'],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=['afterset'], default_scopes=['afterset']),
            revocation_options=RevocationOptions(enabled=True),
        )
    return options
