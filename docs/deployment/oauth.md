# OAuth and HTTP setup

[Documentation](../README.md) · [Project overview](../../README.md)

Afterset includes a single-owner OAuth authorization server using FastMCP's
`auth_server_provider` hook. It publishes protected-resource and authorization-server
metadata, supports dynamic client registration (DCR), S256 PKCE, owner consent,
short-lived access tokens, refresh-token rotation and revocation. This is one
owner's database, not a multi-user account service.

| Variable | Purpose |
| --- | --- |
| `AFTERSET_OAUTH_PASSPHRASE` | Owner's consent-page passphrase, at least 16 characters and at most 1024 UTF-8 bytes. Prefer a generated secret. Recommended for hosted/mobile connectors. |
| `AFTERSET_HTTP_TOKEN` | Alternative fixed ASCII bearer token, at least 32 characters, for scripts/IDE clients that can set headers. Mutually exclusive with OAuth mode. |
| `AFTERSET_HOST` | HTTP bind address, default `127.0.0.1`. |
| `AFTERSET_PORT` / `PORT` | Listen port, in that precedence order; default `8765` (`8080` in Docker). |
| `AFTERSET_PUBLIC_URL` | External URL ending in `/mcp`; HTTPS required outside loopback. Defines the resource/audience and public Host/Origin. |
| `RAILWAY_PUBLIC_DOMAIN` | Supplies `https://DOMAIN/mcp` if no explicit public URL is set. |

For an HTTPS reverse proxy on this host, set a private owner passphrase and run
(zsh example; the prompt hides the input):

```sh
read -rs 'AFTERSET_OAUTH_PASSPHRASE?Afterset owner passphrase: '
export AFTERSET_OAUTH_PASSPHRASE
export AFTERSET_PUBLIC_URL=https://afterset.example.com/mcp
uv run afterset-mcp --transport streamable-http
```

The proxy terminates HTTPS and forwards the whole application with its original
public Host: `/mcp`, `/.well-known/*`, `/authorize`, `/register`, `/token`, `/revoke`
and `/oauth/consent`. Forwarding `/mcp` alone breaks login. `/healthz` is public
liveness only and returns no personal data. Host/Origin checks remain enabled.
For a direct container/platform listener, bind `0.0.0.0`.

The browser consent page identifies the client and return address, explains its
read/write/sync access, and asks for the **Afterset owner passphrase**, never a
Garmin password. Consent is browser-bound with CSRF protection, expires after ten
minutes, and requires a fresh passphrase entry for each grant. Failed sign-in
attempts are globally limited to ten per five-minute window, persisted across
restarts. Authorization codes expire after two minutes and can be used once.

OAuth state lives in `oauth_*` tables in the same SQLite database on the persistent
volume. Access and refresh tokens/codes are stored as hashes. Registered client
credentials are encrypted with a salted scrypt-derived owner key. Access tokens
last up to 15 minutes; refresh grants last 30 days from the original login.
Each refresh rotates both tokens and invalidates the previous access token.
Reusing an old refresh token revokes the entire grant family. `/revoke` revokes
the corresponding access/refresh family.

Keep the passphrase and public domain stable across deployments. Changing the
passphrase deliberately invalidates all OAuth clients and grants at next startup;
remove/re-add connectors to register again. Back up the database, including its
OAuth state, and keep the passphrase in the host's secret store. Registered clients
that obtained a grant remain registered; abandoned registrations and pending
consent requests are bounded. Stdio continues to use local process permissions.

Use the owner OAuth flow for hosted [Claude](../clients/claude.md#claudeai-remote-connector)
or [ChatGPT](../clients/chatgpt.md#hosted-oauth-webmobile) connections. Fixed bearer
mode is available for clients that can send an Authorization header.

Continue with [Docker](docker.md) or [Railway](railway.md) deployment.
