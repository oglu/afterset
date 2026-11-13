# Syncing and analysis

[Documentation](README.md) · [Project overview](../README.md)

Run these commands from the checkout root. Complete [Garmin login](configuration.md#garmin-authentication) first.
The explicit dates below are examples; replace them with your desired range.

## Sync data

Sync wellness or activities with the same engine used by MCP:

```sh
uv run afterset fetch --days 7
uv run afterset activities --start 2026-01-01 --end 2026-01-07
```

Run the same background-job sync used by MCP, waiting for completion locally:

```sh
uv run python scripts/sync-range.py --start 2026-01-01 --end 2026-01-07 --scope activities --enrich
uv run python scripts/sync-range.py --start 2026-01-01 --end 2026-01-07 --scope wellness
```

The CLI automatically splits larger backfills into consecutive jobs of at most
31 days. MCP and `scripts/sync-range.py` accept one job of at most 31 days. CLI
commands exit with a nonzero status for partial or failed jobs. Activity jobs
fetch summaries and exercise sets. `--enrich` additionally fetches activity details,
laps, and Garmin chart samples (potentially downsampled, not full FIT data). Wellness
jobs fetch sleep, HRV, stress, body battery, heart rate, steps and calories. Failed
requests are reported, successful work is kept, and original set responses are
retained if a later request fails. Failed wellness endpoint calls do not overwrite
a day's existing metrics with nulls. Corrections survive all Garmin syncs.

## Exploratory analysis

Analysis dependencies are optional; the CLI and MCP installation excludes pandas,
seaborn and matplotlib. Run the read-only analysis example with explicitly selected
variables and an output directory:

```sh
uv run --extra analysis python examples/analyze.py --predictor stress_avg --outcome sleep_score --lag-days 1 --output-dir output/analysis
```

Lag features are computed in memory using calendar days.
The example exports paired observations, Pearson/Spearman correlations and a
scatter plot. These are exploratory associations; trying many pairs can produce
strong correlations by chance, and repeated daily observations are not independent.

## Example requests

- “Refresh the last seven days of training and sleep.”
- “Yesterday's strength recording is wrong; here is what I did…”
- “Compare my last six easy runs, including pace, heart rate and terrain.”
- “Compare evening training with sleep the following night, showing missing days.”
- “My HRV was low last week because I was ill. Save that, then check how my
  training recovered afterwards.”

Activity dates use local Garmin dates; reading timestamps are UTC.
Configure `AFTERSET_TIMEZONE` to describe your local timezone. Sleep uses wake-up date. Missing data does not mean zero.

For client connections, see [Claude](clients/claude.md) or [ChatGPT and Codex](clients/chatgpt.md).
