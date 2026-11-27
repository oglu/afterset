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

## Synthetic Codex conversation evaluation

The optional Codex conversation evaluator uses fabricated data and a fake Garmin
sync. It runs separate in-memory Codex threads against the current local MCP
server metadata. It does not exercise ChatGPT discovery or the hosted connection.
It can incur model cost. Run the normal offline pytest suite without enabling it.

Install a supported `codex-cli 0.165.0` and authenticate Codex normally. From the
checkout root, use an explicit case, model and new report path:

```sh
uv run python -m scripts.conversation_eval.runner --live --case english --model MODEL_NAME --repeat 1 --output /tmp/afterset-eval-report.json
```

Omit `--repeat 1` to run the selected case three times. Each case is bounded to
three repetitions and each operation has a timeout. Other case names appear in
`--help`. The model sees the case's user messages and synthetic MCP tools;
scenario assertions remain in the external observer. The validated Codex
configuration can still expose limited built-in code host primitives, including
patch, clock and MCP resource operations. The thread has no environment access
and uses read-only sandboxing. Only the randomly named synthetic MCP server has
automatic tool approval. The runner rejects other approval requests.

The runner uses a fresh thread and temporary synthetic database for each run.
The synthetic MCP server has no Garmin account or network access. The app-server
process needs network access to the selected model provider. It disables shell,
browser, app and other configured MCP surfaces, then checks the effective thread
settings and MCP inventory. It refuses to score if the app-server and server
traces disagree. The tester may still see built-in tool definitions; these
controls are not an operating-system filesystem isolation guarantee. The runner
rejects unknown completed item types and checks synthetic MCP calls against the
observer trace. Do not use this evaluation with real records or credentials as
fixtures.

The report includes the model and Codex version, metadata hashes, synthetic tool
calls and responses, database snapshots and automated checks. A successful automated
check is not a semantic review of the assistant's answer. Review every transcript
for truthful reporting, complete review before approval, correct interpretation,
and unnecessary questions. The runner starts a fresh thread for later recall and
withholds approval if its conservative draft gate fails. A failure to initialize or
verify isolation is reported as unverified, not a passed case. A separate manual
protocol covers hosted ChatGPT and remains necessary after an authorized deployment:
[conversation baseline protocol](../tests/conversation_baseline/README.md).

Automated cases currently cover English and Greek dated retrieval, a general
question, an ambiguous opener, explicit no-use, cached-only and no-save boundaries,
a notes-only request, missing/partial/authentication sync outcomes, and reviewed
reconstruction followed by a fresh-thread comparison. The evaluator records whether
it could run each check; it does not automatically grade all wording, authorization
nuances, comparison interpretations, or every case in the hosted manual matrix.
An infrastructure error or unverified isolation stops that run and returns a nonzero
exit code. A deterministic case failure also returns a nonzero exit code.
