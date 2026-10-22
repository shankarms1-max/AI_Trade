# Commodity ACTS release integration

Integration branch: `release/commodity-acts-nifty-integration`.
Commodity development branch remains at `e3f84f2d6288509ce486df761fac007d9d2fcc30`.
This is a source-integration artifact, not deployment or activation approval.

## Integrated history and preservation checks

A non-fast-forward merge brings in exactly these two missing NIFTY commits:

- `98fabc2686319e8120ac5d6abb80178c02e61036`: paper expiry terminalization,
  journal/monitor accounting, parallel-paper lifecycle and corresponding tests/UI.
- `169db24b16eb7ed71b2a48c0d69c4e2805110aeb`: deployed ACTS V2 daily cap,
  `min(base.max_trades_per_day, 6)`; lower explicit limits remain respected.

The merge has no conflicts. The integrated `acts_paper`, `parallel_paper`, paper
expiry helper, scalper-lab engine, Phase 15 monitor/trade components and shared
frontend types match `169db24` exactly. ACTS V1 and parallel-paper caps, expiry-day
permissions, mirror exits and exit-book behavior are not independently changed.
NIFTY expired-unpriced lifecycle records remain explicitly unpriced, not fills.

The commodity engine, approved risk proposal, settings, accounting correction and
worker match `e3f84f2` unchanged. Both commodity dashboard components and route are
preserved. All three Research Dashboard null guards remain present, including
both reasons accesses. No strategy is redesigned or threshold retuned.

All four commodity activation settings default to false; effective local values
were also confirmed false. No environment files, Compose files, broker order
capabilities, deployment machinery or running services are changed.

## Migration compatibility

The single Alembic head and unchanged parent chain are:

`0019_parallel_paper -> 0020_commodity_acts_research -> 0021_commodity_paper_trades`

`0020` adds five isolated commodity research tables and commodity-only indexes,
seed cursors and immutability triggers. `0021` adds six isolated paper tables,
commodity-only indexes/foreign keys/triggers, two new paper cursor rows and one
new control row. Their upgrade code does not alter, delete or rewrite existing
NIFTY or commodity research rows/tables. Alembic's own revision bookkeeping advances
normally. Neither migration is modified by this merge; no production migration is
run. An eventual approved rollout must account for production still being at
`0019`; new commodity endpoints/workers require both later revisions.

## Validation and reproducibility

Frontend: all 82 tests passed; ESLint, `tsc --noEmit`, production `next build`
passed. Built routes include `/`, `/phase15` and `/commodity-acts`.

Main combined ACTS V1/V2, warmup/local Alpha, trade-cap, expiry, parallel-paper,
Phase 15 monitor, commodity research/paper/accounting/recovery and migration group:
450 passed, 5 optional PostgreSQL tests skipped, no failures after test-process
isolation described below. This includes the additive migration/unchanged-chain,
preserved-data and offline PostgreSQL SQL checks.

Additional Alpha/mirror-exit/exit-book/scalper-lab/Alpha-bridge regression group:
302 passed, 2 optional PostgreSQL tests skipped, no failures. Counts are per run;
do not add overlapping diagnostic reruns. The explicitly reproduced migration-then-
expiry case also passed after isolation: 68 passed, 1 PostgreSQL skip.

The initial mixed backend run exposed test-process environment contamination:
Alembic's existing `load_dotenv()` loads the developer `.env`, after which later
offline scalper fixtures reject notification networking with
`SCALPER_REQUIRES_NOTIFICATION_NETWORK_OFF`. This was reproduced by running an
ACTS migration test before the expiry tests; the expiry suite passes alone.
The contaminated initial group reported 128 failures and 93 fixture errors; these
are not omitted from the validation history. The complete 455-case group was then
rerun successfully, rather than only rerunning a passing subset.
For the repeatable combined offline run, set `PYTHON_DOTENV_DISABLED=1` **only in
the test process**, before invoking pytest. The installed python-dotenv supports
this switch. No application configuration, notification setting or safety guard
is changed to make tests pass. Do not export this test switch into production.
For example, in a disposable PowerShell test process:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
.venv/Scripts/python.exe -m pytest backend/tests/test_acts_paper_migration.py backend/tests/test_paper_expiry_terminalization.py --tb=short
```

Actual PostgreSQL revalidation is blocked on this machine: the Docker CLI cannot
reach the `dockerDesktopLinuxEngine` named pipe, `postgres`/`initdb`/`pg_ctl` are
not available on PATH, and neither standard PostgreSQL installation directory is
present. No Docker service or container was started or changed. The dedicated
PostgreSQL commodity suite collected 58 cases and skipped all 58 without a test
URL. Prior successful disposable PostgreSQL results do not establish that this
combined revision has been PostgreSQL-validated. Offline PostgreSQL SQL checks
and SQLite regression results must not be represented as PostgreSQL execution.

## Remaining release/activation gates

- Re-run actual PostgreSQL 16 integration for this exact combined revision on an
  explicitly disposable local test database before deployment approval.
- Preserve the independently modified production Compose configuration. This
  branch does not incorporate or replace Lightsail's source tree at `6dd2b8a` or
  its local modifications; they were not accessed. Any later deployment plan needs
  an operator-reviewed configuration diff, database backup and migration sequence.
- The initial commodity paper budgets are approved, but remain unactivated.
  Obtain current exact contract/expiry/linkage and dated execution evidence,
  confirm session/calendar and source-book quality, and check frozen global-risk
  policy compatibility against the approved research database before activation.
- Gross research does not require invented margin/cost evidence. Keep missing
  net-P&L, actual margin and capital-efficiency metrics unavailable.
- No workers, paper flags or live execution are enabled by this integration. No
  production database, container, Lightsail host or running worker was modified.
