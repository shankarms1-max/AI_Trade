# Commodity native-entry runtime integration

Base: `eb6af5aeee87c3ed56f1abbf376d176973cb9321`.
Branch: `fix/commodity-entry-v1-runtime-integration`.

This is a source integration and offline deployment proposal. Nothing in this task
accesses production, authenticates to Kotak, deploys, starts a trading worker or
places an order. Tests use synthetic observations and disposable databases.

## Port provenance and semantic resolutions

No cherry-pick or wholesale merge was used. The final commit has a single parent,
`eb6af5a`. The three existing commits are provenance inputs, not merged parents.

- `5449ff7d1d720ecac833625fb3c424fd594ed11f`: streamed causal history, exact
  retention of the Alpha/context dependency inputs, observation-scoped shared
  history, private market-data key-file loading and DB-only worker health.
- `e88bdc29ffe1c78fcda3f178185d1c8bde73b2f1`: actual `pAssetCode` future linkage,
  lot/precision/tick validation, next eligible option expiry, held-contract
  capture, dated evidence renewal and finite read-only session verification.
- `84da46a6620604573a9161dfe2101fc28d5e9b2e`: bounded master refresh, shared
  cross-process request budget, worker renewal hooks and current-evidence reads.

Overlaps were reconciled explicitly:

1. `service._history` now streams the original ordered SQL selection, but its
   native directional semantics, Alpha calculations and predecessor policy-hash
   compatibility from `eb6af5a` remain intact.
2. `paper_engine` retains `self.entry_policy`. `self.base_policy` supplies only the
   approved risk template; automatic renewal changes its dated contract evidence.
   The resulting policy is passed into native candidate qualification. Pending
   fills still recheck the stored native floors at a later observed BID.
3. Existing positions keep their stored risk/entry policy documents and hashes.
   Renewal failure blocks new entries while frozen position management continues.
4. `paper_read` retains the responsive release's readiness states, causal as-of
   totals and unavailable-value handling. Automatic renewal uses the latest
   persisted evidence instead of displaying the expired template as current.
5. The responsive frontend, read-only bridge, unified overview and all NIFTY
   components are already present. No older frontend was copied over them.
6. The old source-bind deployment script is deliberately **not** ported. It would
   recreate the mixed runtime this task removes. The new offline planner can only
   create a private proposal; it has no Docker, network or apply operation.
7. Session verification respects an explicit `market_open=false` from the current
   configured-calendar API, so a holiday does not cause a false stale-feed alarm.
   Older exported responses retain the original finite monitoring window.
8. One release-boundary test now permits exactly the two read-only Commodity
   session monitors and checks that neither imports application/broker modules.
   No NIFTY runtime behavior is changed.

## Native entry remains authoritative

`entry.py` and `paper_naked.py` are byte-identical to `eb6af5a`. No fake long leg,
spread width, credit-to-width or blended spread-quality score is reintroduced.
`COMMODITY_SINGLE_SHORT_ENTRY_V1` and its optional explicit OI/volume/distance/
premium-stress floors remain active. Research provides directional context only.

The integration test enables renewal and nonzero native floors together. New
entry decisions have native qualification and use renewed contract evidence.
A later otherwise executable quote with zero volume cannot fill; a subsequent
valid observation can SELL at its actual BID. The pending policy/hash stays frozen
while the latest renewal evidence advances. BUY marks/exits remain subsequent
observed ASK executions, with unchanged quote freshness/depth and lifecycle rules.

No lot, position, entry-count, cash-stop, stress, daily loss, profit, premium stop,
trailing, holding, forced-exit or pre-expiry setting is tuned. Approved CRUDE/NAT
limits and the global INR14,000/two-position controls remain runtime configuration.

## History, secrets and request budget

The original up-to-5000-row SQL query is streamed with ORM batches of 16; every
row receives the original domain validation. Selection preserves first three-
minute bucket captures for the unchanged Alpha dependency cone, recent fast-
horizon observations, and exact opening/session extrema. There is no 1000-row
truncation or new history-cap failure that could block position management.
Alpha and paper use one history view per observation. ContextVar scope is reset
on success and exception, and does not persist across observations or restarts.

`COMMODITY_ACTS_MARKET_DATA_KEY_FILE` reads a private regular file. Unsafe mode,
symlinks, oversized, empty or placeholder files fail closed. The value is not put
in argv, Docker environment or logs. Existing consumer-key-only read authentication
and its search/quote facade are reused; no login, refresh or order path is added.

Keep the existing runtime values when moving to the integrated image:

- `COMMODITY_ACTS_AUTO_CONTRACT_RENEWAL=true` where already approved/configured.
  Source defaults remain OFF, including all paper activation flags.
- `COMMODITY_ACTS_MASTER_REFRESH_SECONDS=1800` retains bounded discovery and
  per-observation eligibility reselection. Failed refresh does not retry or use
  expired metadata. Changed linkage, ticks or quantities fail closed.
- `COMMODITY_ACTS_REQUEST_BUDGET_FILE=/app/cache/commodity-market-budget.json`.
  Both workers must share the same writable private cache and compatible UID.
  Kernel locks pace guarded Commodity SDK-operation starts at one second, bound waits to three
  seconds and share a 30-second cooldown after SDK exceptions. No retries are
  introduced. This is not an SDK-internal HTTP or account-wide quota guarantee for the separate NIFTY
  process; NIFTY does not participate and is unchanged.
- Existing 15-second offsets remain CRUDE `3,18,33,48`, NAT `7,22,37,52`.
- Health remains a read-only research/paper freshness check, including the existing
  90-second health window/start grace, dated evidence and outside-session exposure.

## Existing 0021 history

No migration is required or added. Alembic still has the single head
`0021_commodity_paper_trades`. Database models, migrations and immutable-event
triggers are unchanged. Runtime additions use existing JSON/provenance fields.

A clearly marked synthetic pre-eb6 fixture represents four CLOSED positions,
eight orders and eight fills, with original compatibility scores and both known
and unavailable net P&L. SQLite and PostgreSQL 16 checks load policies, verify
journals, read positions/totals and restart the engine without changing stored
bytes, gross/net fields or event hashes. Old records receive no invented native
qualification. New decisions use the native model.

The actual production documents and its 7292+ events were not fetched or claimed
validated locally. Before a later deployment, run the existing full journal
verifiers read-only against the actual database and require PASS.

## Image-only replacement plan — not executed

Build the integration backend in Codespaces with the existing `backend/Dockerfile`;
use `ghcr.io/shankarms1-max/ai_trade/backend:<integration-short-sha>` and verify its
immutable digest. It already COPYs the full backend, worker and scripts trees.
No Docker build belongs on the 2 GB Lightsail instance. No frontend rebuild is
needed: this integration leaves frontend source byte-identical.

Preserve current resource limits:
backend 384 MiB; each worker 512 MiB; PostgreSQL 256 MiB; frontend 256 MiB;
gateway 64 MiB. Preserve CPU/swap limits, networks, commands, restart policies,
secret ownership, existing flags and all risk/session settings.

The offline layout tool takes an operator-supplied local Compose JSON, not a
remote connection or resolved environment dump:

```sh
python scripts/plan_commodity_image_runtime.py \
  --compose-file /path/to/reviewed-compose.commodity.json \
  --backend-image "${COMMODITY_BACKEND_DIGEST:?full backend@sha256 reference}" \
  --output /path/to/new-private-compose.proposed.json
```

It changes only the image and recognized read-only Commodity code bind mounts of
`backend`, `crudeoilm`, `natgasmini`. Ambiguous project/services, build directives,
restart policy, target memory limits, unknown broad source mounts and unpinned
images are refused. Output is a new 0600 file; existing files are never overwritten.
Frontend, gateway, PostgreSQL and top-level volumes/networks are preserved exactly.
Genuine runtime mounts (secret, cache, logs, database and gateway configuration)
remain. Existing release directories are not deleted.

### Future operator sequence

These operations require a separate deployment instruction and verified Ubuntu-1
identity. They are documentation only here.

1. Verify/prefetch the pinned image outside the application change; record current
   container identities, image IDs, private Compose and host timer definitions.
   Review the exact proposal diff, preserving the existing PostgreSQL volume.
2. Use full-table read-only SQL, not a paginated API sample:

   ```sql
   BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
   SELECT version_num FROM alembic_version;
   SELECT instrument_id, state, count(*) FROM commodity_paper_positions
   WHERE state IN ('PENDING_PAPER_ENTRY','OPEN','PENDING_EXIT','UNRESOLVED_EXPOSURE')
   GROUP BY instrument_id, state ORDER BY instrument_id, state;
   COMMIT;
   ```

   Require head 0021 and zero rows for both instruments. Prefer the flat,
   outside-session window. If any exposure exists, leave the current managers
   running and abort the change.
3. Stop only the two Commodity workers, then repeat the zero-exposure SQL to
   close the check/stop race. If a position appeared, restart the old managers
   and abort. Do not cancel, close or repair journals to make the gate pass.
4. Apply the reviewed image-only Compose file. Recreate only backend and workers:

   ```sh
   docker compose -p commodity_acts --project-directory /srv/commodity-acts \
     --env-file /srv/commodity-acts/.env.production \
     -f /srv/commodity-acts/compose.commodity.json \
     up -d --no-deps --no-build --pull never --wait backend crudeoilm natgasmini
   ```

   Do not run general `up`, `down`, prune, migration or database restore commands.
   Verify actual image IDs and absence of source-code overlays after replacement.
   Compare protected PostgreSQL/frontend/gateway identities and restart counts.
5. Validate health, current contract evidence, native fields, monotonic observations,
   next-observation fills if naturally generated and both full journal chains.
   Alpha warm-up or no natural entry is not a fabricated failure or trade.

### Session timers and source mounts

The backend image contains `worker/commodity_session_verify.py`; the host monitor
continues `docker exec commodity_acts-backend-1 python -B
/app/worker/commodity_session_verify.py`. Removing that code bind mount is safe
because the same path now exists in the image.

`commodity_session_monitor.py` is a **host-side** finite process with Ubuntu-1
identity verification and a host Docker CLI. Do not run it inside the backend,
mount a Docker socket into the app or delete its current timer target. Preserve
existing timers while the old host launcher remains. For a coherent subsequent
launcher update, export the exact monitor from the pinned image into a versioned
host control directory (a stopped `docker create --network none` plus `docker cp`
is sufficient), verify its checksum against the commit inventory, then update the
host timer's script path separately. The monitor uses stdlib and read-only API
results; it does not import strategy code. This host control file is not an
application code overlay. Existing launcher/release files remain for rollback.

### Rollback

Keep the old images, original private Compose and old read-only source directories.
If replacement fails while both instruments are still flat, restore the exact old
Compose/image/source references and recreate only the same three services with
`--no-deps --no-build --pull never`. Preserve the database, cache, journals,
frontend and gateway; never downgrade/restore/rewrite trading data.

If the integrated version has any pending/open/unresolved exposure, do not blindly
roll back to the compatibility scorer: an old runtime could bypass the new frozen
native floors. Keep compatible managers for existing exposure, block further
entries using the existing approved controls if needed, and wait for a separately
reviewed recovery/flat-window plan. Historical native and legacy records remain
append-only evidence regardless of rollout direction.

## Validation and file inventory

Final local results and exact changed paths follow below. No production deployment
or live MCX/network-latency validation is claimed by these synthetic tests.

Final results:

- Full backend **2096 passed**, zero failures/skips.
- All Commodity tests **371 passed**, zero failures/skips, with disposable PostgreSQL 16.
- Native entry: **30 passed** (also included above).
- History **11**, contract renewal **30**, health **3**, secret file **8**,
  session verification **4**, request budget **8**, image planner **10** and
  runtime integration **2** tests passed; PostgreSQL compatibility/native-floor
  aliases also passed. Exact per-module counts are in the validation artifact.
- Frontend **146 passed**; TypeScript and ESLint PASS. No frontend production
  rebuild required because all frontend source remains byte-identical.
- Mypy **28 source files PASS**; flake8 all changed Python PASS; diff check PASS.
- Native entry files and 335 protected NIFTY/shared runtime files remain
  byte-identical. Existing migrations and all frontend source remain unchanged.

Exact changed files:

- `.env.example`
- `backend/app/commodity_acts/broker.py`
- `backend/app/commodity_acts/config.py`
- `backend/app/commodity_acts/contracts.py`
- `backend/app/commodity_acts/history.py`
- `backend/app/commodity_acts/paper_coordinator.py`
- `backend/app/commodity_acts/paper_engine.py`
- `backend/app/commodity_acts/paper_read.py`
- `backend/app/commodity_acts/request_budget.py`
- `backend/app/commodity_acts/service.py`
- `backend/tests/fixtures/commodity_pre_eb6_closed.json`
- `backend/tests/test_commodity_contracts.py`
- `backend/tests/test_commodity_health.py`
- `backend/tests/test_commodity_history.py`
- `backend/tests/test_commodity_image_plan.py`
- `backend/tests/test_commodity_paper_postgresql.py`
- `backend/tests/test_commodity_request_budget.py`
- `backend/tests/test_commodity_runtime_integration.py`
- `backend/tests/test_commodity_secret.py`
- `backend/tests/test_commodity_session_verify.py`
- `backend/tests/test_nifty_release_boundary.py`
- `docs/commodity-entry-runtime-integration.md`
- `scripts/plan_commodity_image_runtime.py`
- `worker/commodity_acts_health.py`
- `worker/commodity_acts_paper_worker.py`
- `worker/commodity_session_monitor.py`
- `worker/commodity_session_verify.py`
