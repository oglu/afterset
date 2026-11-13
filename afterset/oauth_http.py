"""Small HTTP adapters around the SDK's OAuth routes and owner consent page."""
import base64
import binascii
import json

from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route, request_response


class OAuthGuard:
    def __init__(self, app, provider):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] in ('/healthz', '/mcp'):
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin-1')
        if host.lower() != urlsplit(self.provider.origin).netloc.lower():
            return await JSONResponse({'error': 'Invalid Host'}, status_code=421)(scope, receive, send)
        if scope['path'] == '/oauth/consent' and scope['method'] == 'POST':
            origin = headers.get(b'origin')
            if origin and origin.decode('latin-1') != self.provider.origin:
                return await JSONResponse({'error': 'Invalid Origin'}, status_code=403)(scope, receive, send)
        if len(scope.get('query_string', b'')) > 8192:
            return await JSONResponse({'error': 'Request too large'}, status_code=413)(scope, receive, send)
        # Registration returns confidential client credentials. Keep all grant
        # responses (including SDK redirects/errors) out of browser/proxy caches.
        original_send = send
        if scope['path'] in ('/register', '/authorize', '/token', '/revoke', '/oauth/consent'):
            async def no_store(message):
                if message['type'] == 'http.response.start':
                    filtered = [(key, value) for key, value in message['headers'] if key.lower() != b'cache-control']
                    message = {**message, 'headers': [*filtered, (b'cache-control', b'no-store')]}
                await original_send(message)
            send = no_store
        if scope['method'] == 'POST':
            if scope['path'] in ('/token', '/revoke'):
                content_types = [value for key, value in scope['headers'] if key.lower() == b'content-type']
                media_type = content_types[0].split(b';', 1)[0].strip().lower() if len(content_types) == 1 else None
                if media_type != b'application/x-www-form-urlencoded':
                    return await JSONResponse(
                        {'error': 'invalid_request', 'error_description': 'Use application/x-www-form-urlencoded'},
                        status_code=415,
                    )(scope, receive, send)
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > 16384:
                    return await JSONResponse({'error': 'Request too large'}, status_code=413)(scope, receive, send)
                if not message.get('more_body'):
                    break
            if scope['path'] in ('/token', '/revoke'):
                form = parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True)
                authorization = headers.get(b'authorization', b'').decode('latin-1')
                if authorization.lower().startswith('basic '):
                    # The SDK requires client_id in the form even when RFC 6749
                    # permits it solely in HTTP Basic credentials.
                    authorization = 'Basic ' + authorization[6:]
                    scope = {**scope, 'headers': [(key, value) for key, value in scope['headers'] if key != b'authorization'] + [(b'authorization', authorization.encode('latin-1'))]}
                    if 'client_id' not in form:
                        try:
                            decoded = base64.b64decode(authorization[6:], validate=True).decode('utf-8')
                            if ':' in decoded:
                                body.extend(('&' + urlencode({'client_id': unquote(decoded.split(':', 1)[0])})).encode())
                        except (ValueError, UnicodeDecodeError, binascii.Error):
                            pass  # The SDK returns the authentication error.
            if scope['path'] == '/register':
                try:
                    json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    return await JSONResponse({'error': 'invalid_client_metadata'}, status_code=400)(scope, receive, send)
            if scope['path'] == '/revoke' and 'client_secret' not in parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True):
                # SDK 1.30's revocation model requires this field even for public clients.
                body.extend(b'&client_secret=')
            if scope['path'] == '/token':
                resources = parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True).get('resource', [])
                if resources and resources != [self.provider.resource]:
                    return await JSONResponse({'error': 'invalid_target'}, status_code=400, headers={'Cache-Control': 'no-store'})(scope, receive, send)
            scope = {**scope, 'headers': [(key, value) for key, value in scope['headers'] if key != b'content-length'] + [(b'content-length', str(len(body)).encode())]}
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


def configure_oauth_app(app, provider, auth):
    # SDK 1.30 implements public-client PKCE but its discovery metadata lists only
    # secret methods. Advertise the implemented 'none' option as well.
    metadata = build_metadata(auth.issuer_url, None, auth.client_registration_options, auth.revocation_options)
    metadata.token_endpoint_auth_methods_supported = ['none', 'client_secret_post', 'client_secret_basic']
    metadata.revocation_endpoint_auth_methods_supported = metadata.token_endpoint_auth_methods_supported
    endpoint = CORSMiddleware(request_response(MetadataHandler(metadata).handle), allow_origins=['*'], allow_methods=['GET', 'OPTIONS'])
    for index, route in enumerate(app.router.routes):
        if getattr(route, 'path', None) == '/.well-known/oauth-authorization-server':
            app.router.routes[index] = Route(route.path, endpoint=endpoint, methods=['GET', 'OPTIONS'])
            break
    app.add_middleware(OAuthGuard, provider=provider)
    return app
