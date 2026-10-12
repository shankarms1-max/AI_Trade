# Phase 15 research-only snapshot collector

`worker/scalper_research_collector.py` is a separate, explicitly selected
market-data process. It uses the existing Kotak read-only client and option
chain capture. It does not instantiate `ScalperService`, the paper engine, the
strategy lab, ACTS_V1, or a broker order adapter. It can run alongside the
existing scalper when `SCALPER_ENABLED=true`.

The process captures at IST seconds `00`, `15`, `30`, and `45` on weekdays,
only within the configured Phase 15 window, from `SCALPER_START_TIME`
through the `SCALPER_FORCED_EXIT_TIME` 15-second bucket (defaults
09:20:00–15:20:00).
Configured collector holidays are excluded. Overlapping and late jobs are
skipped; a failed capture does not stop the next scheduled capture.

Only `scalper_market_snapshots` and `scalper_option_quotes` are inserted. The
snapshot's `context_json` records `mode=RESEARCH_ONLY`, `status=CAPTURED`, and
its 15-second IST bucket. Required `feature_json` and `signal_json` columns
contain `{}` because no features or strategy signal are calculated. The first
capture committed in a bucket wins; a restart or second process in that bucket
returns the existing snapshot ID. Snapshot and quote rows commit atomically.
The existing market-data adapter preserves exact option identities, quotes,
quantities, OI, volume, source times, future identity, and future expiry;
missing values stay missing.

After the image contains this commit, run it as a **one-off** Compose command
on the Phase 15 host. This reads the current Compose configuration but does
not edit it or start the production scalper worker:

```bash
cd /home/ubuntu/AI_Trade
docker compose --env-file .env.production -f docker-compose.prod.yml run \
  --rm --no-deps backend \
  python /app/worker/scalper_research_collector.py --mode RESEARCH_ONLY
```

The PostgreSQL service must already be reachable. Stop the foreground process
with Ctrl-C. It does not backfill missed 15-second buckets. ACTS evaluation is
done separately by the offline strategy lab after capture.
