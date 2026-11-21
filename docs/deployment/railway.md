# Railway setup reference

[Step-by-step guide with screenshots](mobile-setup-guide.md) · [Documentation](../README.md)

Use the [setup guide](mobile-setup-guide.md) for the complete Railway → Garmin
login → ChatGPT flow. This page lists the values used by that setup.

## Image and storage

| Setting | Value |
| --- | --- |
| Source | Docker Image |
| Image | Copy the image reference from [GitHub Releases](https://github.com/oglu/afterset/releases) |
| Volume mount | `/data` |
| Replicas | `1` |
| Start command | Image default |
| Public domain target port | `8080` |
| Healthcheck path | `/healthz` |
| Healthcheck timeout | `120` seconds |
| Restart policy | On Failure, up to `5` retries |
| Deployment overlap / draining | `0` / `0` seconds |

The volume holds the training database at `/data/afterset.db`, the Garmin
session under `/data/garmin_tokens`, and the optional personal context database
at `/data/personal-context.db`. Railway volume backups cover all three when your
plan supports creating them. The Backups tab may require Pro for new backups.

## Update an existing installation

Update the existing service, keeping its volume, variables, domain and start
command. Keep automatic updates disabled so you can back up before each update.

### Back up before changing the image

Read the release notes and the [upgrade guidance](../configuration.md#backups-and-upgrades).
Finish any running sync or correction before starting, and avoid new writes until
the update is verified. Record the currently deployed image for recovery.

Open the service's **Backups** tab. If your plan supports creating a volume
backup, create one and wait for it to complete. If backups require a plan upgrade,
use the console command below to back up the databases.

Open **Console** and wait for **Connected**. It can briefly show **No running
instances** while loading. Paste this block, then press Enter to execute it:

```sh
/opt/venv/bin/python - <<'PY'
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from afterset.config import database_path, personal_context_path

os.umask(0o077)
folder = Path('/data/backups') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
folder.mkdir(parents=True, exist_ok=False)
for name, source in [('training', database_path()),
                     ('personal-context', personal_context_path())]:
    if not source.exists():
        if name == 'training':
            raise RuntimeError('Training database is missing; stop the update')
        print(name + ': absent; skipped')
        continue
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(folder / (name + '.db'))) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError(name + ': backup verification failed')
    print(name + ': backup verified')
print('Backup directory: ' + str(folder))
PY
```

Save the printed backup directory. Check that each existing database reports
**backup verified**. Stop if there is an error.

These database backups stay on the same volume and exclude Garmin tokens and
Railway settings. Keep them private. Use a separate volume backup for protection
against volume loss.

### Apply the image update

1. Open the service's **Settings → Source**.
2. If Railway offers **Upgrade to** the desired version, select it. Otherwise,
   choose **Edit** beside **Source Image** and enter the reference from
   [GitHub Releases](https://github.com/oglu/afterset/releases).
3. Open **Details** for the staged changes. Confirm the only intended change is
   **Image**, from the current release to the chosen release.
4. Choose **Deploy Changes**.
5. Open **Deployments**. Wait until the new image is **ACTIVE** and shows
   **Deployment successful**.

Check the new image's deployment status; the service can still appear online
while its replacement is starting.

### Verify and recover

Open your existing public domain with `/healthz` appended and check for
`{"status":"ok"}`. Reopen **Console**, wait for the new instance to connect, and
check its installed version:

```sh
/opt/venv/bin/python -c 'from importlib.metadata import version; print(version("afterset"))'
```

Confirm your existing records and corrections remain available through your
usual MCP connection. Log in to Garmin again only if the saved session no longer
works.

If startup or migration fails, retain the logs and backup. Do not simply redeploy
an older image against a migrated database. Stop the service and restore a
compatible pre-upgrade database backup before starting the older version, following
the [recovery guidance](../configuration.md#backups-and-upgrades).

## Variables

| Variable | Value |
| --- | --- |
| `AFTERSET_DB` | `/data/afterset.db` |
| `AFTERSET_TOKEN_STORE` | `/data/garmin_tokens` |
| `AFTERSET_HOST` | `0.0.0.0` |
| `PORT` | `8080` |
| `RAILWAY_RUN_UID` | `0` |
| `AFTERSET_TIMEZONE` | Your timezone, e.g. `Europe/London` |
| `AFTERSET_OAUTH_PASSPHRASE` | Your private passphrase, at least 16 characters |

## Garmin login

In the service's **Console**, run:

```sh
/opt/venv/bin/afterset login
```

Enter your Garmin email and password at the hidden prompts, then complete MFA
if requested. Wait for **Garmin session saved**. The session is stored on the
persistent volume; your password is not saved.

## ChatGPT connection

Enter your service's `https://YOUR-DOMAIN/mcp` address in ChatGPT's
**Plugins → + → Add custom MCP server**, select **OAuth**, and authorize using
your Afterset owner passphrase. Follow [steps 7 and 8](mobile-setup-guide.md#7-connect-chatgpt)
to install the plugin and run your first sync from your phone.
