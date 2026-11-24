# ACTS_V3_MCX_FUTURES — PAPER deployment runbook (additive, reversible)

Status: **NOT DEPLOYED.** The only production credential used in this work was a read-only SSH key, which the
operator has since revoked. Deploying requires explicit write authorisation and a fresh key. Every step below is
additive. None of them touches `acts-paper` (NIFTY ACTS V3), the AST stack on the research host, the research
collector, `ai_trade-backend-1`, `ai_trade-frontend-1` or caddy.

## Rules (from the production preflight)
- **No global compose commands.** Never run `scripts/deploy.sh` or `scripts/update_server.sh`: they run
  `up -d --remove-orphans` across the whole stack. The host compose file carries local edits that must be preserved.
- **New workers are standalone containers.** Use `docker run` with unique names on `ai_trade_app_network`, with an
  immutable image tag, resource limits, `restart=unless-stopped` and a health check. This is the same pattern as
  `acts-paper`.
- **PAPER only.** Set `MCX_FUTURES_MODE=PAPER`; the worker refuses any other value. The worker imports no
  broker order API.
- **Kotak quota.** Each worker makes one quote request per 15 s (≈4/min); the contract lookup runs once a day. The
  shared consumer-key quota was flagged as unconfirmed (`kotak-shared-marketdata-review-20261022`). Confirm it, or use
  a dedicated key, before enabling both instruments.

## Steps
1. **Back up.**
   - `pg_dump` the production database to a timestamped file.
   - Record `docker inspect` (image, start time) for every running container.
   - Record `alembic current`; the expected head is 0024 or lower.
2. **Build the image** for this branch's commit, tagged `ai-trade-mcx-futures:<sha>`, and leave
   `nifty-research-backend:latest` alone. Push it or load it on the host.
3. **Migrate.** Run `alembic upgrade 0025_mcx_futures_paper` once, from a one-off container on the app network
   using the new image.
   - It only creates the `mcx_futures_*` tables and seeds two cursors.
   - The down-migration refuses to drop non-empty journals.
   - If production is below 0024, stop here and report: 0020–0024 belong to Commodity and AST.
4. **Start one worker per instrument**, beginning with CRUDEOILM:
   ```
   docker run -d --name mcx-futures-crudeoilm --network ai_trade_app_network --restart unless-stopped \
     --memory 512m --cpus 0.5 --env-file .env.production \
     -e MCX_FUTURES_MODE=PAPER -e MCX_FUTURES_PAPER_ENABLED=true -e MCX_FUTURES_CRUDEOILM_ENABLED=true \
     -e MCX_FUTURES_HOLIDAYS=<verified MCX holiday list> \
     -v /home/ubuntu/AI_Trade/mcx-control:/app/control \
     --health-cmd "python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument CRUDEOILM --health" \
     --health-interval 60s ai-trade-mcx-futures:<sha> \
     python /app/worker/mcx_futures_paper_worker.py --mode PAPER --instrument CRUDEOILM
   ```
   NATGASMINI is identical apart from the name, its enable flag and `--instrument`. Its scheduler offsets
   (seconds 10/25/40/55) never coincide with CRUDEOILM's (5/20/35/50) or Commodity ACTS's.
5. **Verify** during MCX hours (09:00–23:55 IST in winter):
   - `docker logs` shows `MCX_FUTURES_PAPER_STARTED`.
   - `--status` reports `RUNNING`, with a feed age under 60 s and the contract metadata (lot 10, tick ₹1.00 for
     CRUDEOILM; lot 250, tick ₹0.10 for NATGASMINI).
   - `/api/mcx-futures/status` reports the same.
   - Outside hours the correct state is `MARKET_CLOSED`.
6. **Frontend and API:** these need a new backend image with the routers and the repaired frontend. Swap the
   `ai_trade-backend-1` and `ai_trade-frontend-1` containers individually: rename the old one, start the new one
   with the same name, network and labels, and keep the old one for rollback. Do not use compose for this.

## Kill switch and rollback
- **Immediate stop of new activity:** `touch /home/ubuntu/AI_Trade/mcx-control/MCX_FUTURES_KILL`. The worker
  stops observing within one tick. The journal and any open paper position remain on record.
- **Remove a worker:** `docker stop mcx-futures-<inst> && docker rename mcx-futures-<inst>
  mcx-futures-<inst>-retired-<ts>`. Its tables stay in place: evidence is never deleted.
- **Frontend or backend:** stop the new container, rename it, then rename the previous one back and start it.
- **Migration 0025:** leave it in place. It is inert when no worker runs. The down-migration refuses while journals
  hold events.
