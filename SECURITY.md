# Security policy

Report security problems privately through GitHub. Do not open a public issue.

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the problem, the affected version, and the steps to reproduce it.

Do not include real Garmin credentials, owner passphrases, tokens or training
records in a report. Use synthetic data.

## Supported versions

Only the latest release receives fixes. See [GitHub Releases](https://github.com/oglu/afterset/releases) for the current release.

## Scope

Afterset holds one person's health and training records, plus an optional
timeline of illness and life events. These areas matter most:

| Area | Code |
| --- | --- |
| OAuth authorization server, consent page and token handling | `afterset/oauth.py`, `afterset/oauth_http.py` |
| Bearer-token mode and Host/Origin checks | `afterset/http_auth.py`, `afterset/server.py` |
| Garmin credential, session and Personal API key storage | `afterset/auth.py`, `afterset/personal_api.py` |
| Personal context store and its separation from training data | `afterset/context.py`, `afterset/config.py` |
| Container image and its build allowlist | `Dockerfile`, `.dockerignore` |

Garmin's own services and the third-party `garminconnect` library are out of
scope. Report their problems to their maintainers.
