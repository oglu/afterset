# Changelog

This file lists notable changes in each release. Versions match the
`ghcr.io/oglu/afterset` image tags.

## 0.2.9 — first public release

Afterset syncs your Garmin training, sleep and recovery data to a database you
own. It makes that data available to Claude, ChatGPT and Codex through MCP.

### Garmin sync

- Syncs activities, exercise sets, sleep, HRV, stress, body battery, heart rate,
  steps and calories. Optional enrichment adds activity details, laps and chart
  samples.
- Syncs run as background jobs of up to 31 days. Partial failures are reported
  and keep the work that succeeded. The CLI splits longer backfills.
- Logs in with `afterset login`, which saves only the Garmin session, or with
  `afterset auth`, which saves credentials in the system keyring.
- An opt-in [Personal API backend](docs/configuration.md#garmin-personal-api-backend)
  uses Garmin's documented API instead of the unofficial Connect login.

### Workout corrections

- Your assistant matches your description of a session to the watch's set
  timings and drafts a corrected workout. You review it before it's applied.
- [Structured workouts](docs/workouts.md) hold blocks, rounds and efforts, with
  explicit load and rep conventions. Each value records its source: you, the
  watch or inference.
- Block kinds cover warmups, straight sets, supersets, circuits, AMRAPs, EMOMs
  and finishers. AMRAPs keep their time cap and score. EMOMs keep their
  interval.
- Every correction is a new revision. The original Garmin recording is kept,
  and you can restore any earlier revision. Nothing is written back to Garmin.

### Context for analysis

- `training_summary` returns up to 31 days of workouts, session notes and daily
  sleep and HRV in one call, including missing days.
- Session notes keep subjective feedback, fatigue and RPE next to the numbers.
- A separate [personal context](docs/configuration.md#personal-context) database
  holds illness, life events and explanations for metric changes. Routine
  training tools never read it.

### Running Afterset

- Runs locally over stdio for Claude Desktop, Claude Code and Codex, with plugin
  marketplaces for Claude Code and Codex.
- Runs as a single-owner HTTP server with OAuth for claude.ai and ChatGPT, using
  the prebuilt image on [Docker](docs/deployment/docker.md) or
  [Railway](docs/deployment/railway.md).
- Licensed under the AGPL-3.0-or-later. See [SECURITY.md](SECURITY.md) to report
  a vulnerability.
