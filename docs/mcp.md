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
| `propose_workout` | Draft a complete version-2 or version-3 workout for review |
| `get_proposal` | Check a draft or saved correction; optionally retrieve its full workout |
| `list_workout_proposals` | Find drafts and saved corrections for a session |
| `revise_workout_proposal` | Replace one version-3 effort or block in a new draft |
| `get_exercise_history` | Read effective exercise records and training notes over a bounded date range |
| `compare_workouts` | Compare two workouts with explicit matching and measurement limitations |
| `get_activity_payload` | Original summaries, details, laps or chart samples |
| `enrichment_status` | Separate supported detail, latest fetch outcomes and retained data |
| `get_activity_signals` | Read available channels, coverage and optional bounded samples |
| `compare_activity_heart_rate` | Compare recorded summaries and sample-derived HR for two sessions |
| `get_daily_metrics` | Sleep and recovery metrics, at most 366 days |
| `get_timeseries` | Paginated HR, HRV, stress and sleep stages |
| `save_session_note` | Preserve session descriptions and subjective recovery feedback |
| `save_personal_context` | Save illness, injuries, life events and personal explanations in a separate database |
| `get_personal_context` | Explicit analysis-only, paginated read of overlapping personal events (up to 366 days) |
| `apply_proposal` | Apply a reviewed draft, rejecting stale edits |
| `restore_exercise_sets` | Restore an earlier revision; 0 selects raw Garmin |
| `start_garmin_sync` | Start a persisted background sync job |
| `start_activity_enrichment` | Fetch selected detail for up to 50 existing activities |
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
Known short execution conditions, such as equipment and back support, are retained
as `comparison_conditions` when present.
Use `get_workout` for the complete document before drafting a correction.

Unknown numeric values remain null. Loads use kilograms with explicit conventions.
An empty corrected workout is intentional; the `source` field distinguishes it
from an unclassified Garmin recording with no intervals.
Applying and restoring append revisions rather than deleting history. Applying the
same proposal twice does not create duplicate revisions. The tools never write
corrections back to Garmin. Review/authorization is handled by the client workflow;
any client that directly invokes an apply tool can commit its proposal.

## Resuming a correction

The assistant identifies sessions by date and name. Proposal IDs, activity IDs,
hashes and revision numbers are internal coordination details, shown only when you
ask for an audit or troubleshooting. “Note saved,” “Draft ready for review” and
“Correction saved” describe different outcomes. A draft does not change your
effective workout until you approve it and the apply tool succeeds.

Use `list_workout_proposals` to find a session's proposals and `get_proposal` to
check a particular result. Set `include_workout=true` to retrieve the full review.
An applied proposal stays applied even when a later correction changes the session.
A draft can become stale because its workout or source recording changed.

Note and proposal writes accept an optional `request_key`. Reuse that key only when
retrying the exact same write after an uncertain response. The retry returns the
original result without creating another note or draft. Reusing it with different
input fails. A new observation needs a new key, even if its words match an earlier
note. Applying the same proposal again returns its original saved revision.

The assistant checks recorded counts before asking you to recall them and groups
related questions together. Unknown nonessential fields can remain unknown. Once
you approve an unchanged draft, it can be applied without asking again; a changed
draft needs another review. Requested training advice stays separate from records
and is never saved as factual workout data.

## Editing a draft and comparing sessions

Use `get_workout(target_schema_version=3)` for an explicit converted review
document before editing a version-2 session. Saved history does not change.
`revise_workout_proposal` replaces one identified effort or block, with the
expected draft fingerprint and a new evidence note. Changes need updated
provenance. The operation creates a successor draft and supersedes the old one
atomically. A superseded draft cannot be applied. Retries with the same request
key return the same successor.

`get_exercise_history` reads one effective version of each session, not both the
watch record and its correction. Request a date range of at most 366 days; narrow
the range if it contains more than 500 activities. Follow pagination. Training
notes retain their original words and creation time; creation time is not proof
of the date a feeling or event was reported in those words.
History and comparison responses include at most five note excerpts of 500
characters per session, with explicit truncation flags. Read `get_activity` for
the complete notes when a flag is set or the full wording matters.

`compare_workouts(first_activity_id, second_activity_id)` matches unique established
exercise keys. Repeated or uncertain matches stay unresolved unless you provide
explicit `mappings`, each with `first` and `second` block/effort index pairs.
These coordinates are internal. A changed load can remain the same movement;
different assistance or unknown conditions limit what can be concluded. Numeric
differences require compatible measurement conventions. Shared source activities
are reported so overlapping sessions are not counted twice.

## Activity enrichment and signal coverage

Use `start_activity_enrichment` for selected existing sessions instead of repeating
a date-range or wellness sync. It accepts up to 50 activity IDs and optional
`kinds` from `sets`, `detail`, `splits` and `samples`. Existing payloads are skipped
unless `refresh=true`. Poll `sync_status` until completion, interruption or failure.
Good retained data survives a failed refresh. The Personal API adapter does not
provide sets, splits or chart samples; older retained data may still exist.

`enrichment_status` separates capabilities, latest outcomes and retained payload
timestamps. Missing fetch history is unknown. Unsupported or failed fetching does
not mean the database contains no usable older data. Reads and comparisons never
start a Garmin sync themselves.

`get_activity_signals` returns a compact inventory by default. Set
`include_samples=true` for paginated normalized samples and pass its returned
`fingerprint` as `expected_fingerprint` on later pages. If the underlying data
changes, restart the read. Original payload reads also support a fingerprint
guard. Unsupported units and time semantics remain unknown rather than guessed.

Whole-session HR comparisons keep Garmin's recorded summary separate from
statistics calculated from chart samples. Charts may be downsampled. The
time-weighted mean uses linear interpolation only between valid consecutive
samples at most 10 seconds apart. This is a conservative integration policy, not
a claim about Garmin's recording frequency or a pause detector. It does not fill long gaps or extend
the final sample. The denominator is covered time, not the entire session.
Observations use the half-open elapsed window from zero up to the session's end:
an exact-end sample can bound integration but is not counted as an observation.
Missing timestamps, duplicate times and backwards clocks interrupt or prevent
integration rather than being sorted into an invented continuous recording.
Read the returned coverage and limitations before comparing values. Sample
minimum and maximum are observed samples, not guaranteed physiological extrema.
Lower HR alone does not establish improved fitness or a cause for the change.
Coverage below 100% is marked incomplete; this is a completeness flag, not a
medical or measurement-quality threshold. Session duration is reported when known
even if samples are missing. Comparisons flag differences in duration, coverage,
observed sample spacing and effective block/effort counts. Matching counts alone
do not establish matching exercises. HR still covers the raw recording: corrected
exclusions do not recalculate its samples or summary.

Normalization supports a bounded set of known Garmin descriptors, including
object-shaped units for elapsed seconds, HR, speed and distance. Unit metadata
factors are not blindly multiplied into sample values. Recognized UTC timestamps
are interpreted independently; they do not establish timer pauses. Unknown
cadence conventions and other descriptors remain raw data. Coverage can be
unavailable even when the original chart payload exists.

Original FIT ingestion, HR bands, matched-block windows and automatic split
reconstruction are not included. Known distance and speed can constrain duration
but cannot establish an exercise's timestamps.

Queries have bounded dates and pagination. There is no arbitrary SQL or shell tool.
MCP protocol traffic uses stdout; diagnostics use stderr. Sync jobs run while their
server process lives. Shutdown interrupts unfinished jobs; startup marks abandoned
jobs as interrupted, and date ranges can be retried. A file lock allows only one
sync at a time per database across local processes.
