# MCP tools and data semantics

[Documentation](README.md) · [Project overview](../README.md)

```sh
uv run afterset-mcp
# Optional HTTP transport: http://127.0.0.1:8765/mcp
uv run afterset-mcp --transport streamable-http
```

Stdio is the default. HTTP defaults to loopback. `AFTERSET_OAUTH_PASSPHRASE` enables the owner OAuth
flow for hosted connectors; `AFTERSET_HTTP_TOKEN` is an alternative for scripts.
Binding beyond loopback requires one authentication mode and a public endpoint URL. See the [deployment guides](README.md#deployment) for hosted setup. Stdio relies on the local host's process permissions.

| Tool | Purpose |
| --- | --- |
| `data_coverage` | Date bounds, counts and available detail coverage |
| `training_summary` | Up to 31 days of effective workouts, notes and daily sleep/HRV in one call |
| `list_activities` | Date/type filters and paginated summaries |
| `get_activity` | Compact effective workout, notes and summary; opt into raw/history/proposals |
| `get_workout` | Structured blocks/efforts, source links, exclusions, effective summary and historical snapshots |
| `propose_workout` | Draft a complete schema-v2 workout for review |
| `get_activity_payload` | Original summaries, details, laps or chart samples |
| `get_daily_metrics` | Sleep and recovery metrics, at most 366 days |
| `get_timeseries` | Paginated HR, HRV, stress and sleep stages |
| `save_session_note` | Preserve session descriptions and subjective recovery feedback |
| `save_personal_context` | Save illness, injuries, life events and personal explanations in a separate database |
| `get_personal_context` | Explicit analysis-only, paginated read of overlapping personal events (up to 366 days) |
| `apply_proposal` | Apply a reviewed draft, rejecting stale edits |
| `restore_exercise_sets` | Restore an earlier revision; 0 selects raw Garmin |
| `start_garmin_sync` | Start a persisted background sync job |
| `sync_status` | Check progress, completion and partial failures |

Garmin measurements and exercise notes stay in training. Personal illness, life
events and explanations for metric changes use the separate [personal context
store](configuration.md#personal-context). Training tools never read that database;
call `get_personal_context` separately only when the requested analysis needs it.
For mixed user reports, separate exercise content from personal explanations before
saving. Do not place private explanations into session notes or workout evidence.

Subjective training feedback is stored in session notes with a `[subjective_feedback v1]`
marker. Capture the user's exact words plus session date, report date (for later
recollections), post-session fatigue, overall assessment, recovery duration, and
nap status **as of the report**. Record session RPE (0–10) only when explicitly
supplied; otherwise leave it unknown. Preserve later updates as additional notes.
An omitted field means unknown, and “no nap yet” does not mean “did not need one.”
For week/month analysis, use `training_summary(start, end)` in chunks of at most
31 days. It returns all sessions and notes, a sleep/HRV line for each requested day
(including missing days), and `following_morning` for the final training day.
Join evening training to the next **calendar** wake-up date. `recorded=false`
means a daily row is absent; a recorded row can still contain unknown/null values.

`get_activity(activity_id)` omits raw Garmin sets, history and proposals by default.
Use `include=["raw"]` when checking the watch recording before a correction, or
`include=["raw", "history", "proposals"]` for an audit. History returns revision
metadata; `get_workout(revision=...)` retrieves a full historical snapshot.
`get_activity` and `training_summary` expose a `workout_view="compact"` projection:
measurement conventions, recording links and field-source labels are retained;
verbose evidence, provenance text and free-form `details` fields are omitted.
Use `get_workout` for the complete document before drafting a correction.

Unknown numeric values remain null. Loads use kilograms with explicit conventions.
An empty corrected workout is intentional; the `source` field distinguishes it
from an unclassified Garmin recording with no intervals.
Applying and restoring append revisions rather than deleting history. Applying the
same proposal twice does not create duplicate revisions. The tools never write
corrections back to Garmin. Review/authorization is handled by the client workflow;
any client that directly invokes an apply tool can commit its proposal.

Queries have bounded dates and pagination. There is no arbitrary SQL or shell tool.
MCP protocol traffic uses stdout; diagnostics use stderr. Sync jobs run while their
server process lives. Shutdown interrupts unfinished jobs; startup marks abandoned
jobs as interrupted, and date ranges can be retried. A file lock allows only one
sync at a time per database across local processes.
