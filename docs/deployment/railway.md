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
at `/data/personal-context.db`. Railway volume backups cover all three.

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
