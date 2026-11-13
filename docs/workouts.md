# Structured workouts

[Documentation](README.md) · [Project overview](../README.md)

Workouts use schema version 2: a session contains ordered blocks (warmup, straight
sets, supersets, circuits, AMRAPs, EMOMs or finishers), and each block contains
individual efforts and round numbers. Efforts have explicit distance, repetition basis, load basis,
optional targets/reported metrics, evidence and field-level provenance. A session
can reference multiple source Garmin activities. Recording links are many-to-many:
several exercises can share a combined watch interval without invented timestamps.

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

## Review and apply corrections

Use `propose_workout` to draft a reconstruction, review it, then call
`apply_proposal`. Explicitly excluded whole recording intervals can reduce
effective duration. Heart rate, calories and training load are not recalculated;
those values are flagged for interpretation when an exclusion is present.
