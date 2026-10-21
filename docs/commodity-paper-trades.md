# Commodity ACTS V2: automated paper lifecycle

Development branch: `feat/commodity-acts-paper-trades`, based on `c76677f`.
This implementation is paper-only, disabled by default, and has not been deployed.
Synthetic regression results are not MCX execution validation or a profitability claim.

## What was missing and what is reused

V1 already discovers exact MCX mini contracts, batches read-only quotes, captures at
15-second intervals, computes the original causal 3-minute Alpha, and journals
original ACTS directional/scoring research. V1 remains immutable, score-only evidence.
It previously had no orders, executable spread candidates, fills, position lifecycle,
exit recovery, trading risk reservations or trading P&L.

V2 composes that service rather than replacing it. It calls the unchanged
`acts_paper.acts_v2` market-state/watch/scorer/entry-validation functions and consumes
the V1 Alpha actually known at processing time. Original weights (28/18/16/16/8/14),
bands (40/55/70/85), positioning, strong-opposite-Alpha handling and resumption
qualification are unchanged. Warmup remains honestly unavailable; it is not
artificially upgraded or made an extra veto beyond the original scorer.

Executable books reuse `scalper.strategy.validate_book`. Candidate ranking reuses
`strategy.construction.short_leg_quality`, `construction_key` and premium retention.
No NIFTY implementation, fixed-400 policy, thresholds, journal, worker or deployment
is changed. NIFTY-specific session/expiry wrappers are deliberately not reused.

## Practical broker margin verdict

| Instrument | Same-short standalone / nearby hedge / farther hedge | Verdict |
| --- | --- | --- |
| CRUDEOILM | No reliable current two-leg Kotak portfolio estimate available | `MARGIN_UNVERIFIED` |
| NATGASMINI | No reliable current two-leg Kotak portfolio estimate available | `MARGIN_UNVERIFIED` |

The installed `kotakneoapi` 3.0.7 `margin_required` interface and
`neo_api_client/services/margin.py` accept **one** token, side and quantity, and require
completed trade-session `edit_token`/`edit_sid`. They do not expose an atomic two-leg
portfolio basket in this interface. The existing commodity facade intentionally
exposes only `search_scrip` and `quotes`, with no authenticated order capability.
Adding together single-leg estimates cannot establish hedge margin relief.
No order, margin API call, position creation, new authentication flow or live
credential use was performed to manufacture an answer.

Primary references inspected:

- [Official SDK margin interface](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/portfolio/margin_required.md)
- [Official SDK implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/neo_api.py)
- [Kotak portfolio margin documentation](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/portfolio-and-funds-apis/margins.md)

No numerical broker-margin saving, economic rejection or verified return on capital
can be claimed for either commodity. Maximum theoretical spread loss is **not**
broker margin. Hedge premium, spread credit and theoretical loss are computed only
from observed books and an independently confirmed cash multiplier.

Optional dated `MarginEvidence` records allow a verified external broker estimate
for the exact instrument/expiry/lot/short/hedge/NRML portfolio. The same short alone,
nearby hedge and farther hedge must be compared without changing the short. Each
record carries source, timestamp, `portfolio_verified=true`, and measurement kind.
Ambiguous, mismatched, future-dated or expired evidence is not used. Default freshness
is 300 seconds. Frozen records do not magically refresh; without a new current
policy they become unavailable. There is no unsupported margin polling adapter.

The comparison records absolute margin, percentage saving, hedge cost, net premium
credit, theoretical loss and credit/verified-estimate ratio. Credit/actual-blocked
capital and trade return on capital remain null unless the measurement is explicitly
supported as `ACTUAL_BLOCKED`. Estimates are always labeled separately. An optional
`max_broker_margin` is a hard veto when the estimate is unknown or over limit;
without that optional ceiling, margin-unverified **execution research** is permitted.
If future verified results show negligible relief, review an alternative structure
separately; this engine never falls back to naked selling.

## Execution and lifecycle

`NO_SETUP -> WATCH -> READY -> PENDING_PAPER_ENTRY -> OPEN -> PENDING_EXIT -> CLOSED`
also supports `ENTRY_REJECTED` and `UNRESOLVED_EXPOSURE`. Original ACTS `ENTER`
qualification corresponds to READY/queue creation; no separate made-up score gate
or extra reversal debounce is added.

- A bullish candidate sells an actual OTM put and buys a lower-strike put. A bearish
  candidate sells an actual OTM call and buys a higher-strike call of the same expiry.
  Every outward observed hedge is considered. No constant width or synthetic grid.
- Deterministic ranking first balances the original shared short-quality score
  (meaningful premium, distance scaled by observed strike step, observed wall/range
  evidence, OI/volume and execution spread), then hedge premium retention, farther
  protection at equal retention and exact identity. Audit evidence preserves components and ranking.
  Credit must be positive and below width; confirmed-multiplier defined loss must
  satisfy the explicit per-trade cap. A spread already at its configured stop using
  the current observed unwind book is mechanically nonviable and rejected.
- Signal N creates a durable pending paper order. Entry is only on a **later**
  observation with source quotes newer than both signal time and actual order
  creation time. Entry credit = short BID minus hedge ASK. Restarting does not
  backdate an order or fill. The fill is repriced and risk-checked again.
- Both exact leg identities (exchange, token, expiry, strike, type, symbol) and lot
  size must match. Positive, finite, noncrossed tick-aligned books, causal fresh
  timestamps and enough observed side depth for one confirmed lot are required.
  Reference-future timestamp must also be current for new entries.
  Entry OI and volume must be present; no NIFTY-sized minimum OI/volume count is
  imported into MCX. Hard liquidity checks use observed one-lot side depth and the
  explicitly configured entry spread ceiling; original ACTS scoring is unchanged.
- Partial/incomplete depth is **not** filled piecemeal. A failed atomic attempt is
  journaled; it may wait within the default 60-second TTL, then reject. No fictitious
  one-leg exposure or partially completed spread. This models conservative immediate
  visible-book fills, not queue priority, hidden liquidity or price impact.
- Mark/exit debit = short ASK minus hedge BID. Prices are never derived from LTP.
  Wider adverse exit spreads are allowed if otherwise valid; an observed exit debit
  above expiry payoff width is recorded honestly, never clamped to maximum loss.
- Exit priority: forced session/session rollover; expiry safeguard; kill switch;
  holding-time stop; instrument/global daily loss; hard per-trade loss; spread stop;
  trailing protection; profit target; full original opposite ACTS V2 ENTER signal.
  The opposite signal requires a genuine executable opposite candidate, not merely
  changed direction or an artificially zeroed execution score. This preserves the
  transferable ACTS `MIRROR_EXIT_V1` symmetry without NSE's 15:20 wrapper. Requested
  commodity profit/trailing/time exits are active, not merely diagnostics.
- Exit signals queue a durable exit request. Only later fresh source books after
  its actual creation can close it. Missing books/contracts retain unresolved
  exposure, null current P&L, its risk reservation, and the original exit reason.
  There is no fake session-end reset or expiry settlement. Later valid exact books
  can resolve it. Expired or no-longer-confirmed metadata cannot authorize a fill.
  If an entire capture fails before an observation exists, the existing V1
  CAPTURE_FAILED journal records it; V2 retains the last position rather than
  inventing an observation/exit, and the read API masks stale unrealized P&L.
- Held exact contracts are included in existing batched quote requests even when
  they leave the ATM neighborhood. A contract roll is not a substitution.

## Risk, provenance and recovery

- One lot, at most one active/pending position per instrument, configurable 1–2
  global positions and session trade count. Both instruments share a commodity-only
  DB mutex to serialize reservations and atomic lifecycle transactions.
- Per-trade maximum theoretical loss, instrument daily loss, global defined-risk
  and global daily-loss ceilings are explicit positive monetary inputs. Pending
  entries reserve risk too. Worst-case daily reservations include supported complete
  configured costs; realized losses and fresh marked losses feed daily exit checks.
  Unvalued/stale other exposure blocks new risk, but not exit management.
- No maximum-loss cap can guarantee a realized loss bound when markets gap, depth
  disappears or wide executable exit prices exceed theoretical expiry loss.
- Original policy/ACTS config, exact contract evidence, quantities, raw quote,
  normalized interpretation, timestamps, source snapshot, risk config and simulated
  fill evidence are frozen in each position and hash-chained event stream.
- Raw `CommodityOption.depth_unit` remains `UNKNOWN`. Execution requires **external,
  dated exact-contract evidence** of UNITS or LOTS, tick size, option/future linkage,
  lot size, and INR-per-point-per-lot. There is no numerical inference from depth or
  raw master tick values. A transient interpretation used by the shared validator
  is persisted separately alongside the original UNKNOWN/raw quantities.
- Gross P&L = (observed entry credit - observed exit debit) x confirmed INR/point/lot.
  Net remains null by default. Optional `COMPLETE_FLAT_PER_EXECUTED_LEG` requires
  `all_in_cost_per_leg_execution`, source and effective date range. It deducts **four**
  fully inclusive leg-execution costs per completed spread. It must cover all
  applicable MCX charges, not just brokerage. If a flat complete model is unsupported
  by actual fees, leave it unconfigured; no NIFTY STT schedule is copied. Displayed
  configured-cost net is a model estimate, never broker-settled net P&L.
- Six additive tables: `commodity_paper_cursors`, `commodity_paper_controls`,
  `commodity_paper_positions`, `commodity_paper_orders`, `commodity_paper_fills`,
  `commodity_paper_events`. They share the existing DB connection but never write
  NIFTY trading tables/journals. Instruments have separate cursor/hash chains.
- Fills and events have ORM and migration-installed database UPDATE/DELETE guards;
  PostgreSQL also blocks TRUNCATE. Orders/positions are projections verified against
  the immutable event ledger at restart. Snapshot hashes, cursor state and global
  policy are verified. Tampering fails closed rather than silently rebuilding data.
- Every paper observation retains its full frozen policy, even when rejected.
  Events record the DB-mutex transaction sequence as well as instrument-local
  hash-chain sequence, preserving actual cross-instrument risk ordering. Gaps from
  idempotent/no-event attempts are harmless; ordering is never inferred from clocks.
- IDs are deterministic, snapshot processing is chronological and idempotent, and
  order/fill/projection/cursor/event changes commit atomically. A crash rolls them
  all back. A committed V1 decision missed by V2 is drained before the next capture.
  On first adoption V2 starts at the latest available V1 observation, not by trading
  the entire old research history. Old quotes cannot become retrospective fills.
- Stop new entries with `COMMODITY_ACTS_TRADING_ENABLED=false`; **keep the capture
  flags and workers running** while managing existing exposure. Disabling/stopping
  those workers prevents automatic recovery and exits. Turning off new entries
  does not erase outstanding positions. Existing positions retain frozen policy.
  Risk-cap policy drift blocks pending/new entries rather than relaxing reservations.

## Read-only dashboard

`/commodity-acts` retains V1 overview/journal and adds a separately error-bounded V2
panel with instrument filters and bounded paginated position/order/fill/event views.
It shows observed future identity/price, original scores/components, data coverage,
timestamps, rejection counts, pending/open/closed/unresolved state, descriptive
wins/losses/sample-session counts, cumulative gross, nullable configured-cost net,
margin comparisons and risk blocks. Unavailable is not zero. Stale unrealized P&L
is hidden while original mark evidence remains stored. Process liveness is explicitly
unavailable; activity is not proof that a worker runs. There are no trading controls.

GET-only API additions: `/api/commodity-acts/paper/summary` and
`/api/commodity-acts/paper/{positions|orders|fills|events}?instrument=CRUDEOILM`
(or `NATGASMINI`, limit 1–100, offset 0–100000). Reads cannot initiate capture,
strategy execution, fills or broker requests. Missing migration yields a safe
MIGRATION_REQUIRED state. Original V1 APIs are unchanged.

## Configuration and controlled-deployment prerequisites

All actual environment files, production flags, workers and infrastructure were
left untouched. `.env.example` keeps all commodity activation defaults OFF.
Before any separately authorized deployment:

1. Reconcile the separately deployed Research Dashboard fix on
   `deploy/expired-paper-exposure`. Do not replace the running frontend with this
   branch wholesale or merge unrelated changes blindly. Existing NIFTY root/Phase15
   components are unchanged here.
2. Back up the DB and rehearse deployment against an appropriate staging copy.
   The disposable local PostgreSQL 16.15 integration check is now complete: migration,
   concurrent instruments, immutable evidence and recovery passed (details below).
   This does not replace production backup/approval or validate real MCX quotes.
   Do not downgrade a populated research ledger to discard evidence.
3. Apply, only with separate deployment approval and the normal production DB
   configuration: `python -m alembic upgrade 0021_commodity_paper_trades`.
   No migration was applied to production during development. V1 capture still
   works on 0020 while trading is OFF; V2 activation before 0021 fails closed.
4. Obtain current market-hours MCX quotes for both exact mini instruments. The
   existing 20-Oct sample had zero depth and invalid historical timestamps and
   does **not** validate any fills. Confirm timestamps, executable depth on both
   sides, quantity semantics, lot/cash multiplier, ticks, expiry/underlying linkage,
   and no contract ambiguity. Do not use the synthetic test policies as evidence.
5. Supply existing per-instrument `SESSION_START`, `SESSION_END`, `FORCED_EXIT`,
   `SESSION_SOURCE` and `COMMODITY_ACTS_HOLIDAYS` from the current MCX calendar.
   Forced exit must precede capture end by at least 30 seconds (prefer sufficient
   operational buffer). The existing 3/18/33/48 and 7/22/37/52 second staggering
   remains unchanged. No NSE session default is introduced. V1 policy drift guards
   remain in force; calendar/session-policy changes require an explicit research
   continuity plan, not bypassing those guards.
6. Supply `COMMODITY_ACTS_PAPER_POLICIES_JSON` as a JSON object keyed by each
   enabled instrument. Each `PaperPolicy` requires instrument ID, `ContractEvidence`
   above, `max_loss_per_trade`, `daily_loss_limit`, and `max_entry_spread_pct`.
   Dates/times must be real ISO values, with timezone offsets on datetimes. Evidence
   must be known before the observation and valid through the management window.
   Optional research defaults: max 3 trades/session, 30-second quote age, 60-second
   entry TTL, 30-minute holding time, 50% profit capture, 2x credit stop, 25% trailing
   activation / 15%-of-entry-credit giveback, at least 1 DTE. These are configurable
   research policies, **not** validated optimal commodity parameters. Do not tune
   original ACTS weights/entry thresholds to make data trade.
7. Set explicit `COMMODITY_ACTS_GLOBAL_MAX_DEFINED_RISK` and
   `COMMODITY_ACTS_GLOBAL_DAILY_LOSS_LIMIT`, and global max positions 1 or 2. Limits
   must be coherent across both processes. Verify optional broker margin externally
   or retain MARGIN_UNVERIFIED labels; do not make a capital-efficiency claim.
   Configure complete supported costs or keep net null.
8. Only after read-only validation and explicit activation approval, the minimum
   commodity flags are `COMMODITY_ACTS_PAPER_ENABLED=true`, per-instrument
   `COMMODITY_ACTS_CRUDEOILM_ENABLED=true` / `COMMODITY_ACTS_NATGASMINI_ENABLED=true`,
   and `COMMODITY_ACTS_TRADING_ENABLED=true`. These flags enable **simulated** orders
   only. No existing production/NIFTY strategy flag needs changing.

Future worker commands from the repository root, not executed for deployment here:

```text
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM --status
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI --status
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI
```

`--status` is DB-read-only. `--once` captures/processes a single eligible observation
when explicitly enabled. Full workers stay in independent processes. There is no
LIVE argument, executable order adapter, order endpoint, order authentication flow
or broker order call in this commodity path. No Docker/Caddy/Lightsail changes are
part of this implementation.

## Remaining evidence limitations

Current market-hours MCX execution metadata/depth remain deployment blockers.
The disposable PostgreSQL runtime check is complete. Both margin verdicts remain unverified;
capital efficiency and production readiness cannot be claimed. Normal option
expiry/devolvement cannot be paper-settled without observed supported evidence, so
missing end-of-session books remain unresolved exposure. Dated metadata expiring
while exposure is unresolved intentionally prevents further fills, requiring an
audited evidence/operational review rather than a synthetic reset.

The journal and immutable source evidence support deterministic audit/reconstruction,
including actual processing-time knowledge and configuration. This is not a new
commodity replay CLI and does not change Phase14 replay. Simulated top-of-book fills
are conservative atomic evidence checks, not a model of exchange queue priority.
Evaluate approximately 30–40 actual sessions descriptively before considering
strategy/economic changes. No minimum trade count or profitable result is invented.

## Change manifest

```text
.env.example
alembic/versions/0021_commodity_paper_trades.py
backend/app/api/commodity_acts.py
backend/app/commodity_acts/broker.py
backend/app/commodity_acts/config.py
backend/app/commodity_acts/paper_coordinator.py
backend/app/commodity_acts/paper_engine.py
backend/app/commodity_acts/paper_execution.py
backend/app/commodity_acts/paper_journal.py
backend/app/commodity_acts/paper_models.py
backend/app/commodity_acts/paper_policy.py
backend/app/commodity_acts/paper_read.py
backend/app/db/models.py
backend/tests/test_commodity_acts_migration.py
backend/tests/test_commodity_paper.py
backend/tests/test_commodity_paper_controls.py
backend/tests/test_commodity_paper_migration.py
backend/tests/test_commodity_paper_postgresql.py
backend/tests/test_commodity_paper_read_recovery.py
backend/tests/test_scalper_migration.py
docs/commodity-paper-trades.md
frontend/app/commodity-acts/page.tsx
frontend/components/commodity-paper.tsx
frontend/lib/api.ts
frontend/tests/commodity-acts.test.tsx
frontend/tests/commodity-paper.test.tsx
frontend/types/commodity-paper.ts
worker/commodity_acts_paper_worker.py
```

The two existing migration tests change only the expected migration head/parent.
The DB model registry and API client additions are registration-only for the new
commodity tables/read methods. No deployed NIFTY dashboard root is modified.

## Prior implementation validation (`55538e23`)

Broader backend regression: **1,713 passed, 10 skipped**, no failures. Final
commodity-focused checks (including subsequently added provenance/clock/depth
cases): **69 passed, 1 skipped**, no failures. These are overlapping suites, not
additive counts. PostgreSQL integration checks skip without an explicitly supplied
disposable PostgreSQL 16 URL; SQLite migration integration and PostgreSQL offline
DDL pass. The existing Starlette/httpx deprecation warning remains unrelated.

Frontend: **73 tests passed**; `npm run lint`, `npx tsc --noEmit`, `npm run build`
and `git diff --check` pass. The build prerenders `/commodity-acts`, `/`, and
`/phase15`. No actual market-hours execution evidence or profitability is implied.

Focused command from the repository root, using the existing local venv:

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
.venv\Scripts\python.exe -m pytest -o addopts='' backend/tests/test_commodity_paper.py backend/tests/test_commodity_paper_controls.py backend/tests/test_commodity_paper_read_recovery.py backend/tests/test_commodity_paper_migration.py -q
```

The environment switch prevents Alembic's dotenv loader from modifying test
environment settings. All migration execution in these tests targets temporary
databases (or an explicitly supplied disposable PostgreSQL schema), not production.

## PostgreSQL integration validation

**48 passed, 0 failed, 0 skipped** on a disposable local Docker PostgreSQL
**16.15 (Debian 16.15-1.pgdg13+2)** instance, using psycopg and the real Alembic
migrations. These comprise 46 cases in `test_commodity_paper_postgresql.py` and
the two existing PostgreSQL commodity migration tests. No SQLite results are
substituted here. The full previously passing backend/frontend suites were not
repeated. The existing Starlette/httpx deprecation warning is unrelated.

Validated:

- `0020 -> 0021` with both commodities' existing research: four snapshots,
  168 exact option rows, four original Alpha/ACTS decisions, four journal events,
  and both cursors. Every pre-existing table's rows, columns, PK/FK/unique/check
  constraints, indexes, triggers and existing sequence counters remain unchanged.
  Representative NIFTY snapshot/option data is included in this comparison.
- A separately created **fresh database**, not just `create_all()` or a new schema,
  migrates from empty to head `0021_commodity_paper_trades` with correct genesis seeds.
- Bull/bear entries for both instruments, next-observation exits/P&L, missing/stale
  and partial books, native mirror exits, profit/stop/trailing/forced exits, TTL,
  atomic transaction rollback and journal/global-control validation.
- Pending entries, open positions, pending exits and unresolved exits survive
  disposal of the original connection pool and recovery through a fresh engine,
  session factory and coordinator. Duplicate observations do not duplicate fills.
- Separate-connection concurrent instruments respect the global reservation limit;
  simultaneous duplicate processing produces exactly one atomic fill. PostgreSQL
  composite foreign keys reject cross-instrument order/fill ownership (`23503`).
- Actual database triggers reject UPDATE, DELETE and TRUNCATE of paper fills/events.
  Read-only views, coordinator crash-gap recovery and initial cursor recovery work.

No PostgreSQL compatibility fixes to the engine, ORM or migrations were required.
Only the opt-in integration tests and this evidence/checklist were added/updated.
All test markets/contract attestations remain synthetic, not actual MCX validation.
No production database, worker, deployment, credential file or strategy flag was used.

The server was bound only to `127.0.0.1` on an assigned ephemeral port. Its Docker
image was `postgres:16`, resolved to
`sha256:a902560563d3a5661228585f53474c5065e7f273b21ffd77e555a8b5682c5690`.
The disposable server contains only synthetic fixtures; local trust authentication
is for this isolated test instance only, never a production configuration.

To repeat against a new dedicated disposable instance:

```powershell
docker --context desktop-linux run --detach --rm --name ai-trade-commodity-pg-validation --publish 127.0.0.1::5432 --env POSTGRES_USER=commodity_validation --env POSTGRES_DB=commodity_paper_validation --env POSTGRES_HOST_AUTH_METHOD=trust postgres:16
docker --context desktop-linux port ai-trade-commodity-pg-validation 5432/tcp
# Replace <port> with the reported loopback port; wait for pg_isready first.
$env:COMMODITY_POSTGRES_TEST_URL='postgresql+psycopg://commodity_validation@127.0.0.1:<port>/commodity_paper_validation'
$env:PHASE15_POSTGRES_TEST_URL=$env:COMMODITY_POSTGRES_TEST_URL
$env:PYTHON_DOTENV_DISABLED='1'
.venv\Scripts\python.exe -m pytest -o addopts='' backend/tests/test_commodity_paper_postgresql.py backend/tests/test_commodity_paper_migration.py::test_postgresql_paper_migration_on_disposable_schema backend/tests/test_commodity_acts_migration.py::test_commodity_postgresql_additive_migration -q
docker --context desktop-linux stop ai-trade-commodity-pg-validation
```

The new suite rejects non-loopback URLs, other database/user names and connection
query overrides. It never reads `DATABASE_URL` as its test target. Each runtime case
uses a private schema with migrated triggers/FKs; only mutable genesis seeds are
reset so the original fixtures can insert their identical seed records. Test-created
schemas/fresh databases are dropped afterward. No engine behavior is patched to
work around PostgreSQL; the established synthetic directional context is reused.
