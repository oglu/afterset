# Development and verification

[Documentation](README.md) · [Project overview](../README.md)

Run these commands from the checkout root.

```sh
uv sync --group dev
uv run pytest -v
```

Test dependencies live in the `dev` dependency group, which uv includes by
default. Run one module with `uv run pytest tests/test_workouts.py -v`, or select
tests with `uv run pytest -k workout`. Tests use plain assertions, pytest fixtures
and parametrization. Mocks and patches use Python's `unittest.mock` module.
The Docker build excludes development dependencies with `--no-dev`.

Workflow linting retains a narrow compatibility exception for the supported
GitHub Actions `concurrency.queue` field, which actionlint 1.7.12 does not yet
recognize. Remove that exception when upgrading to a linter that supports it.

Tests use temporary databases and a real MCP stdio client. They cover review/apply/
restore, stale and cross-session edits, raw-data retention, pagination, input
validation, partial sync failures, and protocol discovery and tool calls.
Portability tests cover storage selection, timezone boundaries, headless/keyring
login, process locking, and plugin launches from another working directory.
GitHub Actions runs the suite and builds the package on Linux and macOS
using the Python matrix in [the workflow](../.github/workflows/tests.yml), plus
authenticated Docker smoke tests on Linux.
Tests use synthetic data and do not call Garmin. HTTP tests cover bearer mode,
Host/Origin enforcement, discovery, DCR, consent/CSRF, PKCE, client/resource binding,
code/refresh replay and expiry, revocation and restart persistence. The OAuth
container smoke uses the SDK client through its real discovery/PKCE flow and
reuses the persisted grant after restart. Bearer smoke also excludes analysis dependencies:

```sh
docker build -t afterset:remote-test .
python3 scripts/check-container.py
python3 scripts/check-container.py --auth oauth
```

## Maintainer scripts

These scripts in `scripts/` support releases and hosted setups. The sync helper is also available for advanced local usage.

| Script | Purpose |
| --- | --- |
| `check-image.py` | Classifies registry lookups and promotes tested stable images without moving the latest tag backwards; errors stop publication. |
| `check-container.py` | Smoke-tests a built image with synthetic data in a disposable volume. CI runs it. |
| `oauth-container-probe.py` | Runs inside a test container. `check-container.py --auth oauth` calls it. |
| `sync-range.py` | Runs one MCP-style sync job and waits for it. See [Syncing and analysis](usage.md#sync-data). |
| `check-garmin-login.py` | Read-only check that the pinned `garminconnect` version can log in and renew a session. Pass `--tokens` with a scratch directory. |
| `import-hosted.py` | Imports a local database and Garmin session into an empty hosted install. Stop the server and sync writers, then run it inside the container. It refuses to overwrite existing training records. Add `--context` and `--context-sha256` to install a separate `personal-context.db` as well. |

## Release checks

```sh
uv build --out-dir dist/release
uv run python scripts/check-release.py --dist dist/release
```

The release check rejects private/generated files in Git candidates and package
archives, checks documentation links, and verifies package/plugin/image metadata
agree. CI also runs [Gitleaks](https://github.com/gitleaks/gitleaks) against the
full Git history with findings redacted. Never use real health records in fixtures.
Screenshots require manual review before adding them; automated scans cannot
reliably identify private details in images.

Publishing a stable GitHub Release runs these checks again before building the
image and attaching release artifacts. See [Publishing](maintainers/publishing.md).
