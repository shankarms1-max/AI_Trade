# Phase 15 strategy-lab Alpha bridge

The offline lab keeps Phase 15 `scalper_market_snapshots` and
`scalper_option_quotes` as its observation source. An optional separate
PostgreSQL URL supplies only Phase 14 `alpha_feature_snapshots`. Each database
is opened in its own read-only transaction. No SQL cross-database join,
schema creation, migration, or application write session is used.

Set `ALPHA_RESEARCH_DATABASE_URL` in the one-off lab process, or pass
`--alpha-database-url`. The CLI argument takes precedence. Without either,
the existing same-database Alpha loader and all replay decisions are unchanged.
Use a PostgreSQL account with `SELECT` permission only on
`alpha_feature_snapshots`; the transaction-level read-only setting is a second
guard. Keep this credential separate from Phase 15 `.env.production` and do not
print it in commands or research artifacts.

Identity and time fields are read from the persisted Phase 14
`AlphaFeatureSnapshotRecord.result_json` as `AlphaFeatureSnapshot`. The
corresponding scalar metadata columns are checked for agreement:

| Phase 14 Alpha field | Phase 15 snapshot field | Use |
| --- | --- | --- |
| `reference_instrument_id` | `future_instrument_id` | exact futures identity |
| `reference_expiry` | `future_expiry` | exact futures expiry |
| `expiry` | `expiry` | exact option expiry |
| `timestamp` | `captured_at` | Alpha at or before capture, age at most 180 seconds |
| `feature_calculated_at` | `captured_at` | calculation completed by capture |

The bridge loads a single indexed timestamp range from the earliest requested
capture minus 180 seconds through the latest capture. It sorts by Alpha
timestamp and primary key, then chooses the latest candidate that passes the
existing `ACTS_V1` Alpha eligibility check. This retains the existing version,
`LIVE_ORIGINAL`, `FUTURE`, continuation, validity, source-time, and direction
consistency gates. Missing futures or option identity is reported as
`ALPHA_IDENTITY_INCOMPLETE`; no proxy value is produced. The bridge never
backfills historical Alpha.

With an external source, `alpha_bridge.json`, the manifest, the ACTS summary,
and CLI output include the source and counts. `alpha_rows_loaded` counts raw
rows in the bounded query; `alpha_rows_valid` counts rows passing intrinsic
version, mode, source, identity, continuation, and validity checks. Rejection
counts assign one reason to each unmatched observation, using its latest
candidate within 180 seconds; stale and future-only candidates are reported
when no in-window candidate exists. Same-database CLI diagnostics describe
selected Alpha evidence only, while the default replay artifacts remain
unchanged.

For a prospective Phase 15 AWS one-off lab run, inject the Phase 14 read-only
URL into the one-off container's environment without adding it to the Phase 15
production env file:

```bash
cd /home/ubuntu/AI_Trade
: "${ALPHA_RESEARCH_DATABASE_URL:?provide the read-only Phase 14 URL}"
mkdir -p /tmp/phase15-lab-artifacts
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm --no-deps \
  -e ALPHA_RESEARCH_DATABASE_URL \
  -v /tmp/phase15-lab-artifacts:/research-output backend \
  python /app/scripts/run_scalper_lab.py \
  --start 2026-10-12 --end 2026-10-12 \
  --output /research-output/acts-oct12-alpha
```

The operator must first provide `ALPHA_RESEARCH_DATABASE_URL` to the shell
through an approved secret mechanism. The command above does not display its
value. No collector or pipeline service is started by this one-off invocation.
