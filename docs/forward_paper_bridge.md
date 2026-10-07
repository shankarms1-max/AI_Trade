# Forward paper bridge — engineering report and review runbook

Branch: `research/agent-analysis-2026-10-06`. This report supersedes the forward-readiness
sections of the October 6 audit, not its sealed replay results. No commit, push,
merge, deployment, production migration, live broker request or production flag
change was performed. The existing replay ledger and strategy/risk implementations
are unchanged.

## A. Exact files changed by this task

Capture reconciliation (copied selectively from `fix/replay-market-data-capture`):

- `backend/app/broker/base.py`
- `backend/app/broker/kotak/depth.py` (new on this branch)
- `backend/app/broker/kotak/market_data.py`
- `backend/app/collector/service.py`
- `backend/app/data/snapshot.py`
- `backend/tests/test_kotak_option_quotes.py` (new on this branch)
- `backend/tests/test_kotak_quote_diagnostic.py` (new on this branch)
- `backend/tests/test_phase2_collector.py`
- `scripts/diagnose_kotak_option_quotes.py` (new on this branch)
- `worker/collector.py`

Forward-paper integration:

- `backend/app/paper/__init__.py`, `models.py`, `service.py` (new)
- `alembic/versions/0015_forward_paper.py` (new)
- `backend/app/core/config.py`
- `backend/app/db/models.py` (register new models only)
- `backend/app/pipeline/service.py`
- `backend/app/api/dashboard.py`
- `backend/tests/test_forward_paper.py` (new)
- `backend/tests/test_forward_paper_migration.py` (new)
- `frontend/components/session-overview.tsx` (minimal edits to existing uncommitted work)
- `frontend/types/index.ts`
- `frontend/tests/dashboard.test.tsx`
- `docs/forward_paper_bridge.md` (this report)

Pre-existing frontend CSS, dashboard layout and API-client edits remain intact;
they were not replaced. User replay datasets/configurations were not modified.

## B. Capture reconciliation

The selected files match the capture branch's implementation (apart from an
irrelevant trailing blank line). Tests confirm wall-clock three-minute alignment,
124 observations, inclusive 09:18/15:24/15:27, no 15:30 bucket, premarket/weekend/
holiday handling, batched exact-contract depth enrichment, timestamp parsing,
nullable tick size and quote/depth persistence. Existing option-chain LTP/OI/
volume are retained. UNKNOWN quantity units remain UNKNOWN.

The older branch's pipeline/config files were NOT copied over the decision-only
safeguards. This establishes code parity with the supplied production-capture
branch, not a new live production verification.

## C. Migration

`0015_forward_paper`, following `0014_phase14_2_1_replay_integrity`, adds only
`paper_cursor`, `paper_trades`, `paper_events`, their constraints/indexes, one
cursor seed row, and evidence-protection triggers. Existing rows are not updated.
The revision is shorter than the existing 32-character Alembic limit.

Capture/decision-only operation and the dashboard remain compatible with 0014;
unmigrated paper counts return null. PAPER requires 0015 before startup. SQLite
upgrade/downgrade/data preservation and actual SQL trigger rejection were tested.
PostgreSQL DDL was compiled offline, not executed on a PostgreSQL instance.
Downgrade destroys the paper evidence tables: export/backup first; never use it
as a way to clear exposure or an integrity failure.

## D–E. State machine and pipeline order

`PENDING_PAPER_ENTRY -> OPEN -> CLOSED`

Invalid entry: `PENDING_PAPER_ENTRY -> ENTRY_REJECTED`.
Missing/invalid exit or interrupted path: `OPEN -> UNRESOLVED_EXPOSURE`.
Unresolved exposure retains its original entry and blocks new entries; it has no
invented exit, zero P&L, or automatic recovery/reset operation.

Each new stored observation is processed in this order:

1. Mark/exit previously open exposure and resolve previously pending entries.
2. Build features, optional alpha, then regime.
3. Generate candidates and evaluate existing hard risk gates against paper state.
4. Queue one approved candidate as a new pending entry; stop without filling it.

The bridge is deliberately bounded to one active exposure and one lot. Existing
daily risk/entry limits still apply. Pending/unresolved exposure consumes that
slot. Opposite-regime exits use the latest preceding persisted regime, never the
same observation's not-yet-computed regime. Other exits reuse the existing
profit-target, stop-loss, structural and time-exit function without tuning.

## F–H. Prices and validation

| Execution | Short leg | Long leg |
| --- | --- | --- |
| Entry credit | observed bid | observed ask |
| Exit debit | observed ask | observed bid |

Both legs must exactly match exchange, token, expiry, strike and option type.
The shared replay validator rejects missing/nonfinite/nonpositive prices,
crossed/wide books, invalid ticks, stale/future observations and source times,
leg timestamp skew, ambiguous identities, absent or insufficient executable size.
No LTP fallback, interpolated quote or inferred contract is used.

Unchanged integrity defaults: 180-second cadence, 30-second cadence tolerance,
240-second decision-to-fill maximum delay, 30-second observation/source age,
10-second maximum leg skew, zero future-time tolerance. Spread limits use the
stricter existing configured strategy/risk spread limit. Execution requires a
strictly later observation and decision time, continuous processed observation
path, same session and lot size, an allowed entry time and non-expiry day.
Decision-time books are also validated. Fill-time credit/liquidity, configured
market events and the exact approved maximum-loss envelope are rechecked.

## I. Depth

UNKNOWN is not a capacity estimate. Raw quantities and their UNKNOWN status are
preserved in evidence, but `UNKNOWN_REQUIRED_DEPTH` prevents entry execution.
Loss of usable depth on an open trade produces unresolved exposure. The bridge
does not expose an unknown-depth simulation override. Consequently the currently
captured Kotak books still cannot produce primary executable paper fills until
quantity semantics are independently confirmed and captured correctly.

## J–K. Durability and evidence

SQL transactions atomically update the mutable trade projection, cursor and
append-only event chain. A seeded singleton cursor serializes workers. Stable
trade IDs, a unique decision snapshot and unique event keys protect retries.
The durable observation-completion event prevents a retry of t from filling an
entry just queued at t. Restart verifies journal hashes and projections before
operation. Out-of-order new observations fail closed.

Events include DECISION_CREATED, ENTRY_PENDING, ENTRY_EXECUTION, ENTRY_REJECTED,
MARK, EXIT_EXECUTION, UNRESOLVED_EXPOSURE and FINAL_OUTCOME, plus OBSERVATION.
Evidence freezes raw observations, decision candidate/risk/feature/alpha/regime
context, source timestamps, exact books/quantity units, chosen price sides,
decision/execution/exit snapshot IDs, policy/config/source hashes and timestamps.
Absent alpha remains absent. No features or prices are repaired.

ORM event updates/deletes are refused. PostgreSQL triggers refuse UPDATE, DELETE
and TRUNCATE; SQLite triggers refuse UPDATE/DELETE. Hash-chain/projection checks
detect accidental inconsistency. This is not protection against a privileged DBA
deliberately dropping triggers/tables; backups and restricted DB privileges remain
necessary. The existing file-based integrity ledger is untouched.

## L–M. Flags and read-only UI

New `FORWARD_PAPER_ENABLED=false` and existing `PIPELINE_DECISION_ONLY=false`
are independently OFF by default. Forward paper requires the after-snapshot
pipeline and three-minute cadence. It rejects decision-only/replay-integrity
mode, AI/network notifications, unknown-depth/0DTE overrides and multiple-open
legacy configuration. It refuses any open legacy shadow exposure across versions.
When running in decision-only/legacy mode, the pipeline refuses active paper
exposure. Disabling the entire pipeline stops management; it does not discharge
or close exposure and must be handled as an explicit operational decision.

The forward path replaces both legacy callbacks; it never calls their entry/
marking methods. Risk uses paper-table exposure and remains SHADOW-authoritative,
never live-authoritative. AI providers are not constructed; notification hooks
are disabled. No external client is used by the paper engine.

`GET /api/dashboard/latest` adds explicit `execution_mode` and `forward_paper`
pending/open/closed/unresolved/rejected counts with `profitability_claim=false`.
Counts are all-session counts from the new tables, not legacy shadow rows.
Unmigrated counts are null, not fabricated zeros. The current dashboard consumes
these fields without redesign; daily filled/closed funnel stages remain unavailable
because an all-time count is not today's count. Legacy timeline entries are
explicitly labeled legacy, not forward paper. Existing legacy P&L cards/daily
summaries are not the new paper engine's performance evidence.

## N–P. Tests

Focused tests cover capture cadence/persistence, exact identities, both entry/exit
sides, invalid/stale/missing books, UNKNOWN/insufficient depth, gaps/TTL, same-t
and duplicate callbacks, concurrent callbacks, restart, atomic rollback, immutable
SQL evidence, policy exits, unresolved exposure, incomplete costs, 0DTE exclusion,
legacy/decision-only incompatibility, API counts, default-OFF/invalid flags,
SQL-only execution and absence of raw-data mutation.

- Focused capture/paper/safeguard suite: 129 passed.
- Full backend suite: 910 passed in 74.52 seconds (one existing Starlette/httpx deprecation warning).
- Frontend lint and TypeScript: passed.
- Frontend tests: 34 passed.
- Frontend production build: passed.
- `git diff --check`: passed using the repository's normal Windows line-ending configuration.

An initial full run had two notification-health failures, reproduced on the
unchanged `46f4f23` baseline as well:
`test_one_failure_health_is_degraded` and
`test_health_states_after_success_and_failures`. Their SQLite UTC-default
creation timestamp and IST-midnight date filtering are time-sensitive. Both
subsequent full-suite runs passed without changing notification code or tests;
retain this intermittent baseline issue for separate investigation. No test was
weakened. Baseline
reproduction used a separate temporary worktree, subsequently removed.

## Q. Timing

A local real-pipeline SQLite fixture measured 131.47 ms total, including 23.98 ms
paper pre-update and 8.72 ms enqueue. Every run retains existing stage/total
timings; OBSERVATION also records pre-stage runtime. This is comfortably below
180 seconds for the tested fixture, not a Lightsail or full-history performance
guarantee. Collector remains synchronous, `max_instances=1`, coalescing enabled.
Measure real capture + feature/alpha + paper time before unattended operation.

## R. Remaining blockers/limitations

- Kotak derivative quantity semantics remain UNKNOWN: no executable fills.
- Existing scalar cost settings lack the replay cost schedule's dated version
  and SEBI input. Shared `CostSchedule` therefore records GROSS_ONLY with null
  total/net amounts, even if the old completeness boolean is true. No rates are
  supplied or guessed. Incomplete realized monetary state blocks more same-day
  entries. A separately reviewed complete cost configuration is needed for net
  evidence, not a threshold optimization.
- Preserve approved monetary risk settings. Missing limits continue rejecting
  approvals; this change does not supply permissive example amounts.
- Actual PostgreSQL upgrade/locking/trigger smoke testing in staging remains
  required; the offline DDL test is not that verification.
- Unresolved exposure has no automatic repair endpoint. A process outage with
  no further observations leaves existing exposure active until a later
  observation can record the gap; it never invents a session-end fill.
- One lot/one exposure, no partial fills, no depth-unit inference, no assumed
  intrabar target/stop path. Three-minute observations cannot model queue priority
  or prove intermediate prices. More non-expiry sessions are still necessary.
- No general-purpose journal-to-trade replay CLI was added; frozen inputs and
  deterministic transitions support audit/reconstruction, but this SQL journal
  does not replace the registered Phase 14.2.1 offline replay workflow.

## S. Conditional PAPER-only runbook — NOT executed

Only after review, a PostgreSQL staging smoke test, a backup, and explicit
deployment approval: use the reviewed research build in the existing checkout.
Do not merge to main or use the general deploy script (it auto-upgrades head).
Do not run a second collector or a legacy shadow writer alongside this worker.

Keep all approved thresholds, widths, weights, monetary limits, cost inputs and
calendar/holiday settings unchanged. The minimum explicit paper overlay is:

```dotenv
PIPELINE_AFTER_SNAPSHOT=true
FORWARD_PAPER_ENABLED=true
PIPELINE_DECISION_ONLY=false
PIPELINE_RUN_AI_RESEARCH=false
TELEGRAM_ENABLED=false
PHASE14_2_STRATEGY_LOGIC_ENABLED=false
PHASE14_2_1_REPLAY_INTEGRITY_ENABLED=false
REPLAY_ALLOW_UNKNOWN_DEPTH=false
REPLAY_ALLOW_0DTE=false
SHADOW_ALLOW_MULTIPLE_OPEN_TRADES=false
COLLECTOR_INTERVAL_MINUTES=3
COLLECTOR_START_TIME=09:18
COLLECTOR_END_TIME=15:27
ALPHA_ENGINE_ENABLED=false
REGIME_USE_STATISTICAL_ALPHA=false
```

For decision-only rehearsal instead, set `FORWARD_PAPER_ENABLED=false` and
`PIPELINE_DECISION_ONLY=true` (only with no active exposure). Alpha capture can
be separately authorized; do not silently enable alpha selection or Phase 14.2.
These are instructions for a later approved environment edit, not edits made here.

From the project root on Lightsail, outside market hours and after approval:

```bash
bash scripts/backup_postgres.sh
docker compose --env-file .env.production -f docker-compose.prod.yml stop collector
docker compose --env-file .env.production -f docker-compose.prod.yml build backend collector frontend
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm --no-deps migrate alembic -c /app/alembic.ini upgrade 0015_forward_paper
```

Read-only post-migration preflight (prints no credentials):

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm --no-deps backend python -c "from sqlalchemy import select; from app.db.session import get_session_factory; from app.db.models import ShadowTradeRecord; from app.paper.service import verify_journal,paper_summary; f=get_session_factory(); s=f(); assert s.scalar(select(ShadowTradeRecord.id).where(ShadowTradeRecord.status=='OPEN').limit(1)) is None, 'LEGACY_EXPOSURE_PRESENT'; s.close(); print('journal_events',verify_journal(f)); print(paper_summary(f))"
```

Stop and review any pending/open/unresolved state; never clear those tables to
make preflight pass. With the explicitly approved overlay above installed:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --no-deps --force-recreate backend collector frontend
docker compose --env-file .env.production -f docker-compose.prod.yml ps
```

Confirm the dashboard reports PAPER from the explicit API field, inspect stage
timings and rejection evidence, and reverify the journal after the session.
The current UNKNOWN-depth dataset should generate blocked entries, not fills.
Changing mode or stopping collection while exposure exists requires an explicit
operational decision; it must not be presented as having closed that exposure.

## T–W. Safety and readiness verdict

No broker-order path exists in this bridge or its pipeline integration; none was
added. App broker interfaces remain market-data-only. No strategy/risk thresholds,
alpha/carry/survival/directional weights, spread-width policy, or exit parameters
were changed. The 0DTE guard is enforced at decision and execution; it was not
bypassed. Existing historical rows and replay evidence were not altered.

The repository now has a tested forward paper bridge, not a flag-only alias for
legacy shadow trading. It is suitable for reviewed, staged **fail-closed paper
observation/rejection research** after the prerequisites above. It is NOT ready
to promise executable fills or primary/net profitability evidence next session:
UNKNOWN depth, incomplete costs and staging validation remain blockers. No
additional strategy-development/tuning phase is justified by this change.
