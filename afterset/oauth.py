"""Persistent single-owner OAuth provider for FastMCP's authorization-code flow."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, RegistrationError, TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from afterset.connection import connect

SCHEMA = '''
CREATE TABLE IF NOT EXISTS oauth_settings (id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, key_tag TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, document BLOB NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
 expires INTEGER NOT NULL, csrf TEXT, browser TEXT);
CREATE TABLE IF NOT EXISTS oauth_codes (id TEXT PRIMARY KEY, client_id TEXT NOT NULL, document TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (id TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
 family TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT NOT NULL, expires INTEGER NOT NULL,
 used INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);
CREATE TABLE IF NOT EXISTS oauth_login_limit (id INTEGER PRIMARY KEY CHECK(id=1), window INTEGER NOT NULL, attempts INTEGER NOT NULL);
'''
SCOPE = ['afterset']
ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 30 * 86400
CONSENT_SECONDS = 10 * 60
COOKIE = 'afterset-consent'
PAGE_HEADERS = {
    'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
    'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; style-src 'unsafe-inline'",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class OwnerOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, passphrase: str, resource: str):
        if len(passphrase) < 16 or len(passphrase.encode()) > 1024 or passphrase.strip() != passphrase:
            raise ValueError('AFTERSET_OAUTH_PASSPHRASE must be at least 16 characters, at most 1024 UTF-8 bytes, without surrounding whitespace')
        self.resource = resource
        parsed = urlsplit(resource)
        self.origin = f'{parsed.scheme}://{parsed.netloc}'
        self.issuer = self.origin + '/'
        self.cookie = ('__Host-' if parsed.scheme == 'https' else '') + COOKIE
        with connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            setting = conn.execute('SELECT * FROM oauth_settings WHERE id=1').fetchone()
            salt = setting['salt'] if setting else secrets.token_bytes(32)
            self._key = derive(passphrase, salt)
            tag = digest(base64.urlsafe_b64encode(self._key).decode())
            if setting and not secrets.compare_digest(setting['key_tag'], tag):
                # An explicit owner-passphrase change revokes previous grants/clients.
                for table in ('oauth_clients', 'oauth_pending', 'oauth_codes', 'oauth_tokens', 'oauth_login_limit'):
                    conn.execute(f'DELETE FROM {table}')
            conn.execute('INSERT OR REPLACE INTO oauth_settings VALUES(1,?,?)', (salt, tag))
        self._cipher = Fernet(base64.urlsafe_b64encode(self._key))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with connect() as conn:
            row = conn.execute('SELECT document FROM oauth_clients WHERE client_id=?', (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(self._cipher.decrypt(row[0])) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (client_info.token_endpoint_auth_method not in ('none', 'client_secret_post', 'client_secret_basic')
                or set(client_info.grant_types) != {'authorization_code', 'refresh_token'}
                or client_info.response_types != ['code']
                or not client_info.redirect_uris or len(client_info.redirect_uris) > 10):
            raise RegistrationError('invalid_client_metadata', 'Use authorization_code/refresh_token, code responses, and none or client_secret authentication')
        for uri in client_info.redirect_uris:
            url = urlsplit(str(uri))
            if (url.fragment or url.username or url.password or not url.hostname or
                    not (url.scheme == 'https' or (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')))):
                raise RegistrationError('invalid_redirect_uri', 'Callbacks must use HTTPS or a loopback HTTP address without fragments or userinfo')
        document = client_info.model_dump_json().encode()
        if len(document) > 8192:
            raise RegistrationError('invalid_client_metadata', 'Client metadata exceeds 8 KiB')
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Never expire a previously authorized connector's client registration.
            conn.execute('DELETE FROM oauth_clients WHERE created<? AND client_id NOT IN (SELECT client_id FROM oauth_tokens)', (int(time.time())-86400,))
            full = conn.execute('SELECT count(*) FROM oauth_clients').fetchone()[0] >= 1000
            if not full:
                conn.execute('INSERT INTO oauth_clients VALUES(?,?,?)', (client_info.client_id, self._cipher.encrypt(document), int(time.time())))
        # SDK exceptions are frozen dataclasses; contextlib cannot propagate
        # them through the generator-based connection manager's __traceback__.
        if full:
            raise RegistrationError('invalid_client_metadata', 'Client registration capacity reached')

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource not in (None, self.resource):
            raise AuthorizeError('invalid_request', 'Unexpected resource')
        if (params.scopes not in (None, SCOPE) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.code_challenge)
                or len(params.state or '') > 2048):
            raise AuthorizeError('invalid_request', 'Invalid scopes, state or S256 challenge')
        params = params.model_copy(update={'resource': self.resource, 'scopes': SCOPE})
        pending = secrets.token_urlsafe(32)
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM oauth_pending WHERE expires<=?', (int(time.time()),))
            conn.execute('DELETE FROM oauth_codes WHERE expires<=?', (int(time.time()),))
            full = conn.execute('SELECT count(*) FROM oauth_pending').fetchone()[0] >= 1000
            if not full:
                conn.execute('INSERT INTO oauth_pending(id,client_id,params,expires) VALUES(?,?,?,?)',
                             (digest(pending), client.client_id, params.model_dump_json(), int(time.time())+CONSENT_SECONDS))
        if full:
            raise AuthorizeError('temporarily_unavailable', 'Too many pending requests')
        return self.origin + '/oauth/consent?request=' + pending

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        with connect() as conn:
            row = conn.execute('SELECT document FROM oauth_codes WHERE id=? AND client_id=? AND expires>?',
                               (digest(authorization_code), client.client_id, time.time())).fetchone()
        return AuthorizationCode(code=authorization_code, **json.loads(row[0])) if row else None

    def _issue(self, conn, client_id: str, scopes: list[str], family: str, refresh_expires: int) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        for value, kind, expires in ((access, 'access', min(int(time.time())+ACCESS_SECONDS, refresh_expires)),
                                     (refresh, 'refresh', refresh_expires)):
            conn.execute('INSERT INTO oauth_tokens(id,kind,client_id,family,scopes,resource,expires) VALUES(?,?,?,?,?,?,?)',
                         (digest(value), kind, client_id, family, json.dumps(scopes), self.resource, expires))
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=min(ACCESS_SECONDS, refresh_expires-int(time.time())), scope=' '.join(scopes))

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        tokens = None
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            deleted = conn.execute('DELETE FROM oauth_codes WHERE id=? AND client_id=? AND expires>?',
                                   (digest(authorization_code.code), client.client_id, time.time())).rowcount
            if deleted:
                tokens = self._issue(conn, client.client_id, authorization_code.scopes, secrets.token_urlsafe(24), int(time.time())+REFRESH_SECONDS)
        if tokens is None:
            raise TokenError('invalid_grant', 'Authorization code expired or already used')
        return tokens

    @staticmethod
    def _revoke_family(conn, family):
        conn.execute('UPDATE oauth_tokens SET revoked=1 WHERE family=?', (family,))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM oauth_tokens WHERE id=? AND client_id=? AND kind='refresh'", (digest(refresh_token), client.client_id)).fetchone()
            if not row or row['expires'] <= time.time():
                return None
            if row['used'] or row['revoked']:
                self._revoke_family(conn, row['family'])
                return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=json.loads(row['scopes']),
                            expires_at=row['expires'], resource=row['resource'], subject='owner')

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        tokens = None
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM oauth_tokens WHERE id=? AND client_id=? AND kind='refresh'", (digest(refresh_token.token), client.client_id)).fetchone()
            if row and (row['used'] or row['revoked']):
                self._revoke_family(conn, row['family'])
            elif row and row['expires'] > time.time() and scopes == SCOPE and row['resource'] == self.resource:
                conn.execute('UPDATE oauth_tokens SET used=1 WHERE id=?', (row['id'],))
                conn.execute("UPDATE oauth_tokens SET revoked=1 WHERE family=? AND kind='access'", (row['family'],))
                tokens = self._issue(conn, client.client_id, scopes, row['family'], row['expires'])
        # Raise after committing any replay-triggered family revocation.
        if tokens is None:
            raise TokenError('invalid_grant', 'Refresh token expired, revoked or already used')
        return tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        with connect() as conn:
            row = conn.execute("SELECT * FROM oauth_tokens WHERE id=? AND kind='access' AND revoked=0 AND expires>? AND resource=?",
                               (digest(token), time.time(), self.resource)).fetchone()
        return AccessToken(token=token, client_id=row['client_id'], scopes=json.loads(row['scopes']), expires_at=row['expires'], resource=row['resource'], subject='owner') if row else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with connect() as conn:
            row = conn.execute('SELECT family FROM oauth_tokens WHERE id=? AND client_id=?', (digest(token.token), token.client_id)).fetchone()
            if row:
                self._revoke_family(conn, row['family'])

    def _attempt(self) -> bool:
        now = int(time.time())
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM oauth_login_limit WHERE id=1').fetchone()
            if row and now-row['window'] < 300 and row['attempts'] >= 10:
                return False
            window = row['window'] if row and now-row['window'] < 300 else now
            attempts = row['attempts']+1 if row and window == row['window'] else 1
            conn.execute('INSERT OR REPLACE INTO oauth_login_limit VALUES(1,?,?)', (window, attempts))
        return True

    async def consent(self, request: Request) -> Response:
        data = request.query_params if request.method == 'GET' else await request.form()
        pending = str(data.get('request', ''))
        with connect() as conn:
            row = conn.execute('SELECT * FROM oauth_pending WHERE id=? AND expires>?', (digest(pending), time.time())).fetchone()
        if not row:
            return JSONResponse({'error': 'Expired or unknown consent request'}, status_code=400, headers=PAGE_HEADERS)
        params = AuthorizationParams.model_validate_json(row['params'])
        client = await self.get_client(row['client_id'])
        if client is None:
            return JSONResponse({'error': 'Unknown client'}, status_code=400, headers=PAGE_HEADERS)
        if request.method == 'GET':
            browser = request.cookies.get(self.cookie) or secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            with connect() as conn:
                conn.execute('UPDATE oauth_pending SET csrf=?,browser=? WHERE id=?', (digest(csrf), digest(browser), digest(pending)))
            escape = html.escape
            # Browsers also apply form-action to the redirect after a POST.
            # Permit only this registered client's callback origin, so the
            # authorization response can leave the owner-consent page.
            callback = urlsplit(str(params.redirect_uri))
            callback_origin = f'{callback.scheme}://{callback.netloc}'
            policy = PAGE_HEADERS['Content-Security-Policy'].replace(
                "form-action 'self';", f"form-action 'self' {callback_origin};")
            page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Afterset</title><style>body{{font:17px system-ui;max-width:34em;margin:3em auto;padding:1em}}input,button{{font:inherit;padding:.6em;max-width:100%;box-sizing:border-box}}input{{width:100%}}code{{overflow-wrap:anywhere}}</style>
<h1>Connect to your Afterset</h1><p>Client name (not verified): <strong>{escape(client.client_name or client.client_id)}</strong></p>
<p>Return address: <code>{escape(str(params.redirect_uri))}</code></p>
<p>This grants read and write access to all training and recovery records, notes, workout corrections and personal context (including illness, injuries and life events), and permission to sync Garmin for this installation.</p>
<form method="post" action="/oauth/consent"><input type="hidden" name="request" value="{escape(pending)}"><input type="hidden" name="csrf" value="{csrf}">
<label>Owner passphrase <input name="passphrase" type="password" required autocomplete="current-password" maxlength="1024"></label>
<p><button name="decision" value="approve">Allow access</button> <button name="decision" value="deny" formnovalidate>Cancel</button></p></form></html>'''
            # no-referrer makes browsers send Origin: null on form POSTs.
            # Preserve the origin for this same-origin form while withholding
            # the consent URL from cross-origin navigation.
            response = HTMLResponse(page, headers={**PAGE_HEADERS,
                'Referrer-Policy': 'same-origin', 'Content-Security-Policy': policy})
            response.set_cookie(self.cookie, browser, max_age=CONSENT_SECONDS, secure=self.origin.startswith('https:'), httponly=True, samesite='lax', path='/')
            return response
        if (not row['csrf'] or not row['browser']
                or not secrets.compare_digest(row['csrf'], digest(str(data.get('csrf', ''))))
                or not secrets.compare_digest(row['browser'], digest(request.cookies.get(self.cookie, '')))):
            return JSONResponse({'error': 'Invalid browser consent'}, status_code=403, headers=PAGE_HEADERS)
        decision = data.get('decision')
        if decision not in ('approve', 'deny'):
            return JSONResponse({'error': 'Choose allow or cancel'}, status_code=400, headers=PAGE_HEADERS)
        if decision == 'approve':
            if not self._attempt():
                return JSONResponse({'error': 'Too many sign-in attempts; retry in five minutes'}, status_code=429, headers={**PAGE_HEADERS, 'Retry-After': '300'})
            candidate = str(data.get('passphrase', ''))
            with connect() as conn:
                salt = conn.execute('SELECT salt FROM oauth_settings WHERE id=1').fetchone()[0]
            key = await asyncio.to_thread(derive, candidate, salt)
            if not secrets.compare_digest(key, self._key):
                return JSONResponse({'error': 'Invalid owner passphrase; go back to retry'}, status_code=401, headers=PAGE_HEADERS)
        code = secrets.token_urlsafe(32)
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            deleted = conn.execute('DELETE FROM oauth_pending WHERE id=? AND csrf=? AND browser=? AND expires>?',
                                   (digest(pending), row['csrf'], row['browser'], time.time())).rowcount
            if not deleted:
                return JSONResponse({'error': 'Consent request already used or expired'}, status_code=400, headers=PAGE_HEADERS)
            if decision == 'approve':
                authorization = AuthorizationCode(code=code, scopes=SCOPE, expires_at=int(time.time())+120,
                    client_id=client.client_id, code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource, subject='owner')
                conn.execute('INSERT INTO oauth_codes VALUES(?,?,?,?)', (digest(code), client.client_id,
                    authorization.model_dump_json(exclude={'code'}), authorization.expires_at))
        result = {'code': code} if decision == 'approve' else {'error': 'access_denied'}
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), state=params.state, iss=self.issuer, **result), status_code=303, headers=PAGE_HEADERS)
