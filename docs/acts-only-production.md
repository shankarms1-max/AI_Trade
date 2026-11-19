# ACTS-only NIFTY paper product

The public product exposes ACTS V3 as the current paper strategy and ACTS V2 as
its historical predecessor. This is a read-only API/UI change. It does not
change strategy decisions, execution, workers, broker access or database schema.

## Public projections

The existing `/api/phase15` URLs remain compatible:

- `/summary`: exactly ACTS_V3, ACTS_V2 in that order. Every total is calculated
  from those two rows, including unresolved/expired exposure and daily counts.
- `/positions`: actual ACTS positions only, including historical V2 exposure.
- `/journal`: ACTS trades only when no filter is supplied; only ACTS_V3 and
  ACTS_V2 are accepted strategy filters. Retired strategy filters return 422.

`app.acts_paper.read_models` supplies the public wrappers. Their shared read
projections exclude parallel cursors, trades and events before querying or
paginating. They retain the existing 512-sequence-slot indexed historical ACTS
lookup, version isolation, malformed-evidence safeguards and unobserved V3
zero/N/A row. No paper engine is instantiated and no journal is replayed.

## Preserved research and history

ORB_15, ORB_30, ORB_60, EMA_PULLBACK and SUPERTREND_RSI_VOLUME modules, models,
migrations, journals and trade history remain intact. The default generic
`parallel_paper.monitor.summary`, `trade_monitor.trades`, full performance report
and research/worker code retain their full research behavior. Their new optional
ACTS-only read scope is used by the public wrappers, not by strategy execution.
This source change does not stop, restart or reconfigure any deployed worker.

## Frontend evidence

Production cards and pulse show V3 then V2 with ACTS score, band and Alpha.
Retired strategy cards, filters and opening-range workspaces are absent from the
production components. Standalone opening-range evidence code and the existing
NIFTY research page remain available for research.

The workspace reads NIFTY FUT from `dashboard.snapshot.nifty_future`, without
using spot, option premiums or retired strategy evidence as a substitute. ACTS
freshness prefers a V3 observation, falling back to V2 when V3 has no timestamp.
Snapshot failure leaves the future price unavailable without hiding independent
ACTS positions.

Client projections also isolate ACTS from stale mixed responses. Missing version
rows or unknown marks keep aggregate metrics unavailable. Exact contracts,
prices, quotes, policy hashes, MFE/MAE, mirror diagnostics and optional V3 cycle
audit fields remain available in position/journal details. No trading evidence is
recalculated or fabricated.

## Validation and operations

Tests inject large retired-strategy profits, open/unresolved exposures and
malformed dormant records. They verify unchanged public ACTS totals, ACTS-only
SQL/API reads, retained research access and unchanged stored journals. The
94,000-event fixture exercises the existing historical bound on SQLite and local
PostgreSQL 16. No production access or deployment is needed for validation.

No Docker container/image or existing application data is removed by this task.
Local database tests use only newly created synthetic fixtures.
