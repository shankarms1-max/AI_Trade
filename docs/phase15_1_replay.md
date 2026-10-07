# Phase 15.1 deterministic replay

`scripts/run_scalper_replay.py` reads the isolated Phase 15 snapshot and quote
tables and reconstructs features, signals, confirmations, candidates, risk
decisions, fills, marks, exits, and accounting in memory. It never writes to
application tables and imports no broker client.

Authoritative replay uses the configured Phase 15 policy unchanged:

```bash
python scripts/run_scalper_replay.py \
  --start 2026-10-07 \
  --end 2026-10-07 \
  --output research-output/scalper/2026-10-07
```

The source database is selected from `DATABASE_URL`. `--database-url` can be
used to point explicitly at a read-only restored database.

The command creates immutable `manifest.json`, `summary.json`, `trades.csv`,
`observation_quality.csv`, `signal_timeline.csv`, and `daily_summary.csv`
artifacts. Existing artifacts are never overwritten.

Research overrides are explicit and are recorded in the manifest. For example:

```bash
python scripts/run_scalper_replay.py \
  --research-start 2026-10-01 \
  --research-end 2026-10-31 \
  --score-grid 70,75,80,85 \
  --confirmation-grid 1,2,3 \
  --output research-output/scalper/october-research
```

Grid execution against an `OUT_OF_SAMPLE` validation partition is rejected.
A frozen single policy can be evaluated using `--validation-start` and
`--validation-end`. Research overrides never change application settings.

Entry fills use the next valid observation: short bid minus long ask. Exit
marks and fills use short ask minus long bid. LTP is never used. Open or pending
positions at the dataset boundary remain unresolved; no closing price is
fabricated.

When statutory costs are not authoritatively configured, gross P&L is reported,
net P&L remains null, and accounting is labeled `GROSS_ONLY`.
