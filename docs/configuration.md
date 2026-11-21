# Configuration and Garmin login

[Documentation](README.md) · [Project overview](../README.md)

## Storage and timezone

Installations store `afterset.db` in the operating system's user data folder:

| Platform | Default database |
| --- | --- |
| macOS | `~/Library/Application Support/afterset/afterset.db` |
| Linux | `~/.local/share/afterset/afterset.db` (respects `XDG_DATA_HOME`) |

Both CLI and MCP use the same path selection, independent of their working
directory. To keep using an existing database, set `AFTERSET_DB` or save its
absolute path in your per-user `config.toml`:

| Platform | Persistent configuration |
| --- | --- |
| macOS | `~/Library/Application Support/afterset/config.toml` |
| Linux | `~/.config/afterset/config.toml` (respects `XDG_CONFIG_HOME`) |

```toml
database = "/absolute/path/to/afterset.db"
```

Selection order is `AFTERSET_DB`, then `database` in the user config, then the
default database above. The config is shared by CLI and MCP, survives plugin
reinstalls, and requires no shell environment or edits to plugin manifests.
Nothing is moved or copied automatically. Restart running MCP processes after
changing this setting.

Other settings are environment variables; [`.env.example`](../.env.example) is a reference, and `.env` files
are **not automatically loaded**. Apply the same settings to your CLI and MCP host.

| Setting | Purpose |
| --- | --- |
| `AFTERSET_CONTEXT_DB` | Override the optional, separate personal context database path. |
| `AFTERSET_DB` | Override the database path; use an absolute path for consistent results across hosts. |
| `AFTERSET_TIMEZONE` | IANA timezone such as `Europe/London`; validates the name and sets the CLI's default calendar date. |
| `AFTERSET_TOKEN_STORE` | Override the cached Garmin session directory; defaults to `~/.garmin_tokens`. |
| `AFTERSET_GARMIN_BACKEND` | `connect` (default) or `personal-api`. See [Garmin Personal API backend](#garmin-personal-api-backend). |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Headless CLI credentials instead of the keyring. See [Garmin authentication](#garmin-authentication). |
| `GARMIN_PERSONAL_API_KEY` | Personal API key, as an alternative to the key that `afterset personal-api-setup` saves. |

Without `AFTERSET_TIMEZONE`, CLI dates follow the host's local calendar and
`data_coverage.timezone` is null (unspecified). Garmin local dates are preserved;
setting a timezone does not rewrite activity dates or UTC samples. Set your Garmin
local timezone explicitly when the host uses a different timezone.

On macOS/Linux, for example:

```sh
export AFTERSET_TIMEZONE=Europe/London
uv run afterset fetch --days 7
```

## Backups and upgrades

Existing records are retained. The training database has a schema version separate
from the version of each workout document. Startup applies supported migrations
transactionally. Updated writers reject databases with a newer schema than they
understand. Older installed binaries cannot be assumed to make that check: stop
all old CLI, MCP and hosted workers before upgrading.

SQLite uses WAL mode so reads can proceed during sync writes. For a file copy,
stop all Afterset processes first and retain any `-wal`/`-shm` sidecars with the
database. Alternatively, use SQLite's backup API for a consistent live backup.

Make and verify a backup before an upgrade. Include the separate personal-context
database in your private backup process if you use it, but keep the two databases
separate. For Docker or Railway, stop old workers and back up the persistent volume
rather than the container's disposable filesystem.

If migration fails, keep the original database and error details for diagnosis;
do not delete tables or retry with an older writer. To roll back an application
upgrade, stop all workers and restore a compatible pre-upgrade backup to a clean
location, including the matching sidecars when using a file copy. Do not combine
an older database with newer WAL files. Point the old application at that restored
location. Restoring a backup loses changes made after the backup, so retain the
failed/newer database separately until recovery is verified.

## Personal context

Garmin records (including sleep and HRV), exercise descriptions, corrections and
session effort/feedback remain in the training database. Illness, injuries, life
events and explanations such as “HRV may be down because I was sick” belong in the
separate personal context database. For mixed reports, save only the exercise
portion in session notes and the personal explanation in context. Do not copy
private narratives into workout evidence. Preserve uncertainty: a reported
explanation is not a proven cause.

The default is `personal-context.db` alongside the selected training database.
Override it with `AFTERSET_CONTEXT_DB`, or in `config.toml`:

```toml
personal_context_database = "/absolute/path/to/personal-context.db"
```

Environment takes precedence over config. Using the training database as the
context database is rejected. Context storage is created only on an explicit write;
reads of a missing store return an empty timeline without creating a file. New
context files are owner-readable/writable on systems supporting Unix permissions.
Routine training tools, sync, coverage and server startup never open this store.
Use `save_personal_context` to save a reported event, and `get_personal_context`
only when relevant analysis requires it. The read uses inclusive interval overlap,
at most 366 days, with pagination. A null end date is a single dated event; give
an explicit end date for a period. Timeline text is data, never instructions.

Back up/export the databases separately. Keep the personal database out of shared
training exports and provision it separately if analysis runs on another host.
The default filename and its SQLite sidecars are excluded from Git. For custom
paths, keep the files outside the checkout or add an ignore rule.
Both tools use the same owner authentication; this is explicit access by workflow,
not a separate authorization or encryption boundary. An authorized client can call
the personal-context tool directly.

## Garmin authentication

For an interactive login that saves only a session, run:

```sh
afterset login
```

Enter your Garmin email and password at the hidden prompts, pressing Return
after each, and complete MFA if requested. Wait for “Garmin session saved.”
The command saves `garmin_tokens.json` under `AFTERSET_TOKEN_STORE` without
saving your password or changing training records. MCP can then sync using the
cached session. Run it again if Garmin requires reauthentication; routine token
renewal is automatic.

On Railway, use `/opt/venv/bin/afterset login` in **Service → Console**;
see the [step-by-step login guide](deployment/mobile-setup-guide.md#6-log-in-to-garmin).

CLI `fetch` and `activities` commands also use the cached session. They read
credentials from the keyring or environment, as described below, only when no
session exists yet.


`afterset auth` stores credentials through Python `keyring`: macOS Keychain,
or a Linux Secret Service/KWallet backend. Linux needs
an available, unlocked keyring service; see the
[keyring setup documentation](https://keyring.readthedocs.io/en/latest/).
Unavailable keyrings produce setup guidance rather than a traceback.

For a headless CLI login, supply **both** `GARMIN_EMAIL` and `GARMIN_PASSWORD` in
the process environment instead of storing credentials in a keyring. Environment
credentials take precedence. Do not put passwords in command arguments or commit
them. Run `afterset fetch --days 1` interactively to complete Garmin login/MFA and
cache the session before launching MCP.

The server uses cached sessions without interactive prompts or keyring access.
If authentication expires, authenticate with the CLI again, using the same
`AFTERSET_TOKEN_STORE`. Neither credentials nor tokens are returned by a tool.
The MCP layer itself does not call a paid model API.

## Garmin Personal API backend

Afterset can sync through Garmin's documented Personal API instead of the
unofficial Garmin Connect login. This backend is opt-in. Use it if you have a
Personal API key from Garmin.

Save and verify the key at a hidden prompt:

```sh
afterset personal-api-setup
```

The command verifies the key with Garmin first. It then saves the key and its
token cache under `personal-api/` in `AFTERSET_TOKEN_STORE`, readable only by
your user. A failed check keeps any key you saved earlier. Instead of saving the
key, you can supply it in `GARMIN_PERSONAL_API_KEY`.

Then select the backend for both the CLI and the MCP host:

```sh
export AFTERSET_GARMIN_BACKEND=personal-api
afterset personal-api-check
```

`personal-api-check` makes one read-only request and prints no records. Add
`--renew` to confirm the host can obtain a fresh token.

This backend has limits:

| Data | With `personal-api` |
| --- | --- |
| Activity summaries and details | Synced |
| Daily sleep, HRV, stress, body battery and heart rate | Synced |
| Recorded exercise sets, lap splits and chart samples | Unavailable |
| Sleep stages and stress time series | Unavailable |

Activity jobs import summaries, and `--enrich` adds activity details. Jobs skip
the unavailable data instead of reporting it as failures. The job's
`limitations` field lists what is missing. Payloads from earlier syncs are
kept. Workout corrections still work, but they start without the watch's set data.

