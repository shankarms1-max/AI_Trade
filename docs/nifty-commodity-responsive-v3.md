# NIFTY + Commodity responsive v3 integration

Base: `b1dd8edaabee5a75fe00066d0fa8fc1632b5a43b`.
Commodity source: `e3f84f2d6288509ce486df761fac007d9d2fcc30`.
Branch: `release/nifty-commodity-responsive-v3`.

This release ports the Commodity implementation onto the current NIFTY base. It
is not a merge of the older Commodity lineage. No deployment, production access,
broker authentication, market-data capture or worker activation was performed.

## Runtime boundaries

- NIFTY ACTS, parallel strategies, scalper, risk, pricing, mirror exits, expiry
  lifecycle, six-entry ACTS cap and Phase 15 summary hotfix remain unchanged.
- Required shared integration consists of the Commodity router registration in
  the current `main.py`, Commodity ORM registration in `db/models.py`, and the
  original opt-in Alpha session-clock and generic futures-reference hooks.
- The Alpha clock uses a task/thread-local ContextVar with the existing NSE
  defaults. Scope exits restore the previous clock, including on exceptions.
  No Alpha formula, lookback, threshold or NIFTY session definition changes.
- Commodity strategy/execution modules retain the pinned source behavior,
  including `e3f84f2` single-leg cost accounting. Formatting and static typing
  corrections do not change execution. Shared calculations use the existing ACTS
  V2 implementation; no alternate scoring engine or relaxed entry criteria.
- Both supported instruments use single-short PAPER simulation. Historical
  spreads retain their source recovery/read behavior. No broker order adapter or
  executable live mode is added. Broker capabilities remain restricted to the
  existing read-only interfaces.
- No deployment script, Compose service, restart policy or production environment
  file was ported or changed. All Commodity activation defaults remain false.

## Schema

The sole Alembic head is `0021_commodity_paper_trades`:

```
0019_parallel_paper
  -> 0020_commodity_acts_research
  -> 0021_commodity_paper_trades
```

Both added migrations retain the source semantics. They create only Commodity
prefix tables, indexes, constraints, functions and triggers, and seed Commodity
control/cursor rows. They do not alter or rewrite existing NIFTY tables, rows,
positions or journal hashes. Commodity raw snapshots/quotes and paper fills/events
retain database and repository immutability protection. Migration tests compare
existing schemas/data before and after upgrades and test forbidden mutations.

## Read API and responsive UI

The existing responsive pages and optional independent read-only source selection
are retained. With default production relative URLs, the integrated backend serves:

- `GET /api/commodity-acts/summary`
- `GET /api/commodity-acts/decisions?instrument=CRUDEOILM|NATGASMINI`
- `GET /api/commodity-acts/paper/summary`
- `GET /api/commodity-acts/paper/{positions,orders,fills,events}?instrument=CRUDEOILM|NATGASMINI`

These are read-only projections. Requests neither construct a capture service nor
instantiate a paper engine, run a strategy, authenticate to Kotak or write rows.
The existing `/api/phase15/*` endpoints and performance optimization are preserved.

The additive `capture_state`, `readiness`, `configuration_blockers`,
`session_configured` and `policy_configured` fields distinguish configuration and
observation state from transport failure. Existing `status` fields remain for
compatibility. Examples include RESEARCH_DISABLED, PAPER_DISABLED,
SESSION_UNCONFIGURED, POLICY_UNCONFIGURED/INVALID, GLOBAL_RISK_UNCONFIGURED,
NO_OBSERVATIONS_YET and OUTSIDE_SESSION. Empty migration-seeded cursor JSON is
exposed as `latest: null`, not a fictitious observation.

The paper projection adds causal IST `trades_today`, `realized_gross_today` and
`realized_net_today` fields expected by the current UI. It uses stored actual fill
and exit evidence, not recomputed prices. Future exits are excluded from today's
figures. Missing/nonfinite accounting evidence stays N/A; gross is never labelled
net. Margin and costs are never invented. The instrument views display the new
readiness without replacing the responsive design, navigation or NIFTY views.

## Configuration requirements

`CommoditySettings` remains separate from NIFTY `Settings`. Existing
COMMODITY_ACTS_PAPER_ENABLED, instrument enable flags, TRADING_ENABLED,
PAPER_POLICIES_JSON, GLOBAL_MAX_POSITIONS, GLOBAL_MAX_DEFINED_RISK,
GLOBAL_MAX_STRESS_RISK and GLOBAL_DAILY_LOSS_LIMIT are preserved. Monetary limits,
contract evidence, costs, margin and MCX session/holiday values have no fabricated
configuration in this release.

Each enabled instrument requires its existing verified `SESSION_START`,
`SESSION_END`, `FORCED_EXIT` and `SESSION_SOURCE` settings. The policy must validate
against the existing single-short `NakedPaperPolicy` schema, including exact dated
contract/expiry/lot/depth evidence and approved risk settings. Single-short
reservations use GLOBAL_MAX_STRESS_RISK; the defined-risk setting remains for
historical spread compatibility and is not silently treated as a naked-loss bound.
Missing/invalid values are reported by reads and still fail closed in execution.

No observations, positions or journals are imported from another server. Workers
and API must use the intended database containing the additive Commodity tables;
pointing a worker at a different database will not populate this API's views.

## Future worker invocation — documentation only

Use a validated backend/scalper image built from **this v3 release**, which contains
`worker/commodity_acts_paper_worker.py`. The older NIFTY-only `fdbbfd8` image does
not contain the worker. This task does not build or publish a new image.

The operator must supply BACKEND_IMAGE (preferably digest-pinned), APP_NETWORK
(the existing network reaching the intended database), and COMMODITY_ENV_FILE
(an approved secret file). Before any authorized activation, that file must have
the intended per-instrument opt-ins and verified policy/session/risk values. The
examples do not set enable flags to true and were not executed.

CRUDEOILM:

```sh
docker run --detach --name commodity-acts-crudeoilm \
  --network "${APP_NETWORK:?}" --env-file "${COMMODITY_ENV_FILE:?}" \
  --env COMMODITY_ACTS_NATGASMINI_ENABLED=false \
  "${BACKEND_IMAGE:?validated v3 backend image required}" \
  python /app/worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM
```

NATGASMINI:

```sh
docker run --detach --name commodity-acts-natgasmini \
  --network "${APP_NETWORK:?}" --env-file "${COMMODITY_ENV_FILE:?}" \
  --env COMMODITY_ACTS_CRUDEOILM_ENABLED=false \
  "${BACKEND_IMAGE:?validated v3 backend image required}" \
  python /app/worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI
```

The worker CLI accepts PAPER only. Existing activation and configuration guards
remain authoritative. These commands are future invocation syntax, not deployment
or activation approval; this integration does not certify live MCX data or worker
capacity.

## Validation evidence

Evidence artifacts are stored in `/workspaces/commodity-responsive-v3-20261024/`.
The local API checks used an empty disposable PostgreSQL 16 schema and no broker
credentials. Every required Commodity endpoint returned 200. CRUDEOILM and
NATGASMINI both reported PAPER_DISABLED with session/policy/global-risk blockers.
Database health was HEALTHY and overall health was IDLE for the empty, closed
session. No running worker was inferred.

Eight mobile route/theme renders used actual local PostgreSQL-backed API responses
through a local test reverse proxy, without synthetic response substitution. They
showed no generic Commodity transport errors or page overflow. No production data,
live quotes or genuine trading outcomes are claimed.

The legacy parallel migration test now targets its own 0019 revision explicitly;
the fresh-bootstrap test expects the new 0021 head. These are test-only lineage
updates, retaining their existing NIFTY evidence/immutability assertions.

## Files changed

- `.env.example`
- `alembic/versions/0020_commodity_acts_research.py`
- `alembic/versions/0021_commodity_paper_trades.py`
- `backend/app/alpha/clock.py`
- `backend/app/alpha/price_alpha.py`
- `backend/app/api/commodity_acts.py`
- `backend/app/commodity_acts/__init__.py`
- `backend/app/commodity_acts/broker.py`
- `backend/app/commodity_acts/config.py`
- `backend/app/commodity_acts/context.py`
- `backend/app/commodity_acts/domain.py`
- `backend/app/commodity_acts/geometry.py`
- `backend/app/commodity_acts/models.py`
- `backend/app/commodity_acts/paper_coordinator.py`
- `backend/app/commodity_acts/paper_engine.py`
- `backend/app/commodity_acts/paper_execution.py`
- `backend/app/commodity_acts/paper_journal.py`
- `backend/app/commodity_acts/paper_margin.py`
- `backend/app/commodity_acts/paper_models.py`
- `backend/app/commodity_acts/paper_naked.py`
- `backend/app/commodity_acts/paper_policy.py`
- `backend/app/commodity_acts/paper_read.py`
- `backend/app/commodity_acts/read.py`
- `backend/app/commodity_acts/repository.py`
- `backend/app/commodity_acts/service.py`
- `backend/app/db/models.py`
- `backend/app/main.py`
- `backend/tests/fixtures/mcx_master_safe_20261020.json`
- `backend/tests/fixtures/mcx_quotes_safe_20261020.json`
- `backend/tests/test_commodity_acts.py`
- `backend/tests/test_commodity_acts_controls.py`
- `backend/tests/test_commodity_acts_migration.py`
- `backend/tests/test_commodity_acts_timing.py`
- `backend/tests/test_commodity_paper.py`
- `backend/tests/test_commodity_paper_controls.py`
- `backend/tests/test_commodity_paper_migration.py`
- `backend/tests/test_commodity_paper_naked.py`
- `backend/tests/test_commodity_paper_postgresql.py`
- `backend/tests/test_commodity_paper_read_recovery.py`
- `backend/tests/test_commodity_v3_integration.py`
- `backend/tests/test_nifty_release_boundary.py`
- `backend/tests/test_parallel_paper_migration.py`
- `backend/tests/test_scalper_migration.py`
- `docs/nifty-commodity-responsive-v3.md`
- `frontend/components/commodity-acts.tsx`
- `frontend/components/commodity-paper.tsx`
- `frontend/tests/commodity-acts.test.tsx`
- `frontend/tests/commodity-paper.test.tsx`
- `frontend/types/commodity-acts.ts`
- `frontend/types/commodity-paper.ts`
- `worker/commodity_acts_paper_worker.py`

## Final validation

- Full backend: **1,988 passed, zero skipped, zero failed**.
- Commodity-focused: **263 passed, zero skipped**, including real disposable
  PostgreSQL 16 migration, restart/recovery and immutable-evidence checks.
- Migration/boundary recheck after updating the new-head expectations: **9 passed**.
- Frontend: **144 passed across 17 files**.
- ESLint, TypeScript, production frontend build, affected-file flake8 and mypy
  (25 source files): **PASS**. Third-party APScheduler has no typing stubs; mypy
  ignores missing third-party stubs, while checking the application modules.
- `git diff --check`: **PASS**.
- Full test run emitted one existing Starlette/httpx deprecation warning.

NIFTY isolation comparison covers 243 protected runtime/worker/migration files:
all are byte-identical to the pinned base. Four required shared integration files
are explicitly reviewed above; existing NIFTY ORM class definitions remain
AST-identical (one long constraint line was wrapped for lint); no NIFTY decision/management module or Phase 15
summary projection changed. Both Commodity migrations are AST-identical to the
pinned source, and all 17 non-projection Commodity modules retain source semantics
after formatting and static-only typing fixes.

Local Phase 15 summary latency on the empty disposable PostgreSQL schema under
test load was median 33.46ms, maximum 68.62ms across ten requests. This is an empty
local-schema measurement, not a production-scale performance claim; the original
bounded-read performance regression tests also remain green.

Only local disposable data was migrated. No production migration, deployment,
worker activation, broker login, broker order or production access occurred.
