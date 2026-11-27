# Changelog

This file lists notable changes in each release. Versions match the
`ghcr.io/oglu/afterset` image tags.

## 0.4.2 — connector-safe workout validation

- Let nested workout and replacement objects reach Afterset's strict validator,
  so connector validation no longer hides field-level repair feedback.
- Show canonical workout, evidence and load examples to connected assistants.
  Reject invalid load fields without guessing or silently converting them.
- Check the connector-facing schema and repaired saves over MCP. Add a loaded
  workout conversation case with a conservative review and approval gate.

## 0.4.1 — reliable structured workout drafts

- Clarify that recording exercises, sets and rounds requires a structured draft;
  a session note alone does not save the workout. Validate the draft before review.
- Give bounded, actionable feedback for invalid draft fields and missing source
  evidence. Keep uncertain write outcomes distinct from rejected requests so
  assistants check saved status before retrying.
- Log draft failure categories without workout text or health values, and flag
  rejected draft attempts in synthetic conversation checks even after recovery.

## 0.4.0 — workout reconstruction and conversation workflows

- Help assistants retrieve personal training records and use targeted sync when
  needed, while respecting requests to avoid syncing or saving. Inspect active
  jobs and scoped sync attempts without starting a fetch.
- Reconstruct wearable-backed workouts from your description, even when the watch
  recording is accurate but lacks exercise structure. Review load conventions,
  unresolved values and a bounded receipt from the saved revision.
- Preserve numeric execution conditions in compact workout views and keep
  uncertainty, sources and shared recording links visible for later comparisons.
- Add an opt-in synthetic Codex conversation evaluator with isolated test records,
  a fake Garmin backend and separate deterministic checks and semantic review.

## 0.3.0 — workout history and comparisons

- Publish an optional latest Docker tag for stable releases without allowing older
  releases to move it backwards. Recommend explicit updates with database backups.
- Resume correction drafts, check whether they were saved, and retry note or
  proposal writes without duplicates. Replace one effort or block in a reviewed
  successor draft while preserving the rest of the workout.
- Record drop-set stages, plates per side and approximate power or speed ranges
  with version-3 workouts. Existing version-2 records and review history remain
  available through explicit conversion and historical restore.
- Retrieve effective exercise history and compare two workouts with matching,
  source and measurement limitations kept visible.
- Enrich selected activities, inspect fetch outcomes separately from retained
  data, and compare whole-session HR with sample coverage and gap handling.
- Separate the training store into focused modules and guard database writes
  against unsupported newer schemas.

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
