# Phase 14 contract continuity backport

This patch starts from `bd3f68aca662149ba56bc9fb46139aa319ecae85`.
It adds only migration `0017_contract_continuity`, following `0016_scalper`.
The migration adds nullable `market_snapshots.required_contracts_json` (JSONB on
PostgreSQL, JSON on SQLite). Existing snapshots and paper journal rows are not
backfilled or rewritten. No later ACTS or parallel-paper migrations are included.

With forward paper enabled, the existing three-minute collector reads exact
identities from PENDING_PAPER_ENTRY and OPEN trades. Only legs missing from the
normal captured ATM chain receive supplementary read-only broker quote requests.
Tokens are never inferred, remapped to another strike, or replaced with cached
books. Missing, conflicting or duplicate broker identities fail closed. Only
actual broker market fields populate supplementary quotes; execution book,
freshness, liquidity and one-lot depth checks remain unchanged.

Supplementary evidence is stored separately from `MarketSnapshot.options` and
restored through the existing repositories. `execution_options` includes it for
exact execution lookup. Indicators, Alpha and normal candidate construction
continue to use the original rolling option chain. With forward paper disabled,
the collector does not request held-contract evidence.

## Expired unresolved exposure

PENDING_PAPER_ENTRY and OPEN always block new entries. UNRESOLVED_EXPOSURE blocks
through its validated candidate expiry, inclusive. Malformed or incompatible
candidate evidence continues to block. On the next IST calendar day, a valid
expired unresolved trade no longer contributes open strategy keys or an unknown
current exposure to the day's monetary risk state. Closed trades with incomplete
cost accounting for that day still make monetary state incomplete. No counters,
losses or other risk limits are reset or relaxed.

The first subsequent paper observation appends one
`UNRESOLVED_EXPOSURE_EXPIRED` event in the existing serialized journal transaction.
It records expiry, release snapshot, IST date/time, original policy hash, and
explicit `exit_price_inferred=false`, `pnl_inferred=false` and
`profitability_claim=false`. The trade projection receives only an
`exposure_release` annotation and keeps its UNRESOLVED_EXPOSURE state, unresolved
reason and original policy. No exit timestamp, final outcome or settlement value
is created. The summary still counts it as unresolved. Retry and restart do not
append another release, and journal replay reconstructs the same projection.

The production example expiring on 2026-10-13 blocks through that date; its
entry block can release from 2026-10-14. Historical evaluations on or before
expiry still block even after the release annotation exists. Pipeline guards
use the persisted observation's IST date; administrative exposure checks without
an observation default to today's IST date.

No deployment, production database access, broker orders, configuration changes
or historical trade reconciliation are performed by this patch.
