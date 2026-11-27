# Structured workouts

[Documentation](README.md) · [Project overview](../README.md)

Workouts support schema versions 2 and 3: a session contains ordered blocks (warmup, straight
sets, supersets, circuits, AMRAPs, EMOMs or finishers), and each block contains
individual efforts and round numbers. Efforts have explicit distance, repetition basis, load basis,
optional targets/reported metrics, evidence and field-level provenance. A session
can reference multiple source Garmin activities. Recording links are many-to-many:
several exercises can share a combined watch interval without invented timestamps.

## Drop sets and approximate measurements

Version 3 adds stable internal identifiers for blocks and efforts. A block can
contain `drop_sets`: ordered groups of effort IDs, each with its own evidence.
Ordinary sets do not need a group. Each stage keeps its own reps and load; a group
does not add another copy of those measurements or their duration. Several stages
may share a watch interval without dividing it into invented timestamps.

Loads can use `plates_per_side`, with `side_count` and optional `bar_kg`. If the bar
mass is unknown, total external mass stays unknown. An empty bar is not zero load.
Per-hand and total external loads remain separate conventions. Plate mass is not
an estimate of effective landmine resistance.

Power and speed live under `measured`, `reported` or `targets`. In version 3,
`power_watts` and `speed_meters_per_second` accept an exact or approximate scalar,
or a range with `minimum` and `maximum`. An approximate range retains
`approximate=true` and can preserve the original wording in `original_text`.
Ranges never become midpoint measurements. Each populated measurement needs
field-level provenance, such as `reported.power_watts`.

To prepare an older workout for editing, call
`get_workout(target_schema_version=3)`. This returns an explicitly converted review
document without changing the saved workout. Unknown legacy provenance stays
unknown, and conflicting legacy values remain visible. Internal IDs let the
assistant preserve unchanged efforts; ordinary replies use movement names and
set numbers instead.

## Version compatibility

Existing version-2 inputs, including inputs that omit `schema_version`, remain
supported. Existing drafts apply in the version that was reviewed. A historical
restore appends a new revision while preserving the selected snapshot's document
and recording. If that saved snapshot is missing, the restore fails and leaves
the current workout unchanged. Reading a historical workout with a missing
snapshot also fails instead of substituting the current recording.
Requesting revision zero selects the current raw
Garmin view.

Changing a saved workout creates a new reviewed proposal; it does not rewrite
history. See [backup and upgrade guidance](configuration.md#backups-and-upgrades)
before upgrading a persistent installation.

AMRAP and EMOM blocks take an optional `format`. Every field is optional, and an
unknown value stays null.

| Field | AMRAP | EMOM |
| --- | --- | --- |
| `time_cap_seconds` | Time cap | Total duration |
| `interval_seconds` | Not allowed | Interval length, such as `60` or `120` |
| `rounds_completed` | Full rounds finished | Not allowed |
| `extra_reps` | Reps into the unfinished round | Not allowed |

For an EMOM, each effort's `round` is its interval number. An alternating EMOM
gives each movement its own interval numbers. The format's `provenance` records
the source of each field, as it does for efforts.

Queryable values are projected into `workout_sessions`, `workout_versions`,
`workout_sources`, `workout_blocks`, `workout_efforts`, `recording_segments` and
`effort_segments`. Validated JSON retains the full versioned document and flexible
evidence. Revision snapshots retain their own recording payloads; subsequent syncs
do not rewrite correction history. `recording_changed_since_revision` flags a
changed source; `get_workout(include_recording_payload=true)` retrieves the frozen
interval payloads for inspection. `get_workout(revision=...)` reads history.

## Review and save a workout

A wearable recording can be accurate but omit the exercises you performed. Your
assistant can combine it with your account to draft a structured workout. Work
during a watch rest interval and uncertain exercise timing can remain visible
without rewriting the original recording.

Compare the draft with your description before approval. A valid draft may still
have unknown values or missing exercises. Review diagnostics flag some load and
repetition convention gaps and some numeric values left in auxiliary details; they
do not detect every omission. The bounded save receipt shows the applied workout and any
stored review diagnostics. It marks truncation; the full saved revision remains
available through `get_workout`. Older applied revisions may lack a structured snapshot.

Use `propose_workout` to draft a reconstruction, review it, then call
`apply_proposal`. Explicitly excluded whole recording intervals can reduce
effective duration. Heart rate, calories and training load are not recalculated;
those values are flagged for interpretation when an exclusion is present.
