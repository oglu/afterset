# Afterset

Give your AI assistant your _real_ training history.

Garmin records your set timings, but it often gets the exercises and rep counts
wrong. Entering what you actually did on the watch mid-session is fiddly, and
fixing it afterwards in Garmin's app is painful, especially with circuits,
supersets and AMRAPs. Your AI assistant doesn't have that history either, so you
end up explaining your training again whenever you want to plan.

Afterset syncs your Garmin workouts, sleep and recovery data to a database you
own, and makes it available to your assistant through MCP.

When you ask about your training or recovery, the assistant can retrieve relevant
records and sync missing or stale data. You can request cached data only, no sync,
or a discussion without saving. Tool selection depends on your connected assistant.

After training, you tell your assistant what you actually did. It uses your
description and the wearable recording to reconstruct the session for you to
check and approve. This can add exercises and structure even when the watch
recording is accurate. Afterset keeps the original recording, the reviewed
workout and your notes on how you felt for later conversations.

## What you can do

- **Record sessions the way you trained.** Circuits and supersets keep their
  rounds. AMRAPs keep their time cap and score, and EMOMs keep their interval.
  Per-hand loads and reps per leg stay explicit. Each value is tagged by its
  source: you, the watch or inference. See [structured workouts](docs/workouts.md).
- **Ask about training in context.** "How did last week's load compare with my
  sleep and HRV?" Your assistant can retrieve workouts, notes, sleep and HRV together.
- **Keep how it felt.** Session notes, fatigue and RPE sit next to the numbers, so
  a plan can account for more than heart rate.
- **Explain the dips.** Tell your assistant you were ill or travelling, and it
  saves that in a separate personal context database. It reads that database
  only when an analysis needs it, and routine training summaries never include
  it. See [personal context](docs/configuration.md#personal-context).
- **Plan the next block.** With accurate history, your assistant can suggest
  training that fits your recovery and goals.

## What it doesn't do

- It never writes back to Garmin. Corrections live in Afterset.
- It doesn't guess. Missing measurements stay unknown, not zero.
- It isn't a coach and gives no medical advice. Your assistant reasons over the
  records, and Afterset keeps them accurate.

## Where it runs

Run it on Linux or macOS, or host your own private server on Docker or Railway to
use it from your phone. Each installation has one owner, a training database and
an optional personal context database. See the
[phone setup guide](docs/deployment/mobile-setup-guide.md).

Windows is not supported.

## Quick start

Requires **uv**, which can install the Python runtime specified in
[pyproject.toml](pyproject.toml).

By default, records live in your operating system's user-data directory. To reuse
an existing database, [configure its path](docs/configuration.md#storage-and-timezone)
before fetching. For headless hosts, see [Garmin login options](docs/configuration.md#garmin-authentication).

From the checkout root:

```sh
uv sync
uv run afterset auth
uv run afterset fetch --days 7
uv run afterset activities --start 2026-01-01
uv tool install --editable .
```

`afterset auth` saves your Garmin email and password in the system keyring. The
first `fetch` uses them to complete Garmin login and MFA, then caches the session
for the CLI and MCP. `fetch` syncs sleep and recovery. `activities` syncs workouts
and exercise sets from the start date you give, so replace the example date.
The final command installs `afterset` and `afterset-mcp` for your client to
launch. Ensure the uv tool bin directory (`uv tool dir --bin`) is on its PATH.

To keep no password on the machine, run `uv run afterset login` instead of `auth`.
It saves only the Garmin session, which both the CLI and MCP use. The hosted
Docker and Railway guides use `login`. See
[Garmin login options](docs/configuration.md#garmin-authentication).

Connect your client using the [Claude guide](docs/clients/claude.md) or the
[ChatGPT/Codex guide](docs/clients/chatgpt.md). Local clients launch the MCP server
on demand. Then try:

> Summarize my last week of training and recovery, including my session notes and missing data.

## Documentation

- [Configuration and Garmin login](docs/configuration.md)
- [Syncing and analysis](docs/usage.md)
- [MCP tools and data semantics](docs/mcp.md)
- [Structured workouts and corrections](docs/workouts.md)
- [OAuth and HTTP setup](docs/deployment/oauth.md)
- [Docker deployment](docs/deployment/docker.md)
- [Railway deployment](docs/deployment/railway.md)
- [Phone setup with screenshots](docs/deployment/mobile-setup-guide.md)
- [Development and verification](docs/development.md)

See the [documentation index](docs/README.md) for all guides, and the
[changelog](CHANGELOG.md) for what changed in each release.

## License

Copyright (C) 2026 Afterset contributors.

Afterset is free software under the [GNU Affero General Public License v3.0 or
later](LICENSE). If you modify Afterset and distribute it, or let others use a
modified version over a network, you must make your source code available under
the same license.

Afterset is not affiliated with or endorsed by Garmin. By default it syncs through
the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
library, which can break when Garmin changes its service. An opt-in
[Personal API backend](docs/configuration.md#garmin-personal-api-backend) uses
Garmin's documented API instead.

To report a security problem, see [SECURITY.md](SECURITY.md).
