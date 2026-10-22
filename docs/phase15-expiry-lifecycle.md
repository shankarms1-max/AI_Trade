# Phase 15 PAPER expiry lifecycle

`PAPER_EXPIRY_TERMINALIZATION_V1` applies to ACTS V2 and the five parallel
paper strategies. It does not change signals, execution prices, entry expiry
permission, MIRROR_EXIT_V1, PHASE15_EXIT_BOOK_V1, risk limits, or spread widths.
No migration or production configuration change is required.

The exact stored candidate's two leg expiries must agree. Only an observation
whose **IST date is strictly later** than that expiry can terminalize it.
A new chain's expiry and wall-clock startup time never provide fill evidence.

- Expiry-day unavailable exit: retain existing OPEN/UNRESOLVED behavior.
- Expired PENDING_PAPER_ENTRY: append ENTRY_REJECTED with
  CONTRACT_EXPIRED_BEFORE_FILL.
- Expired OPEN/UNRESOLVED_EXPOSURE: append CONTRACT_EXPIRED_UNPRICED (ACTS uses
  ACTS_CONTRACT_EXPIRED) and project CLOSED. Store expiry_date,
  expiry_terminalization, lifecycle version, terminalized_at, the actual
  terminalization snapshot, and previous_unresolved_reason. Exit timestamp,
  exit snapshot, debit, quote evidence, final outcome and gross P&L are null.

This is a lifecycle observation, not a fill or exchange settlement. A priced
expiry-day close, if already recorded as CLOSED, is never reinterpreted.
Existing event rows and stored execution-policy documents/hashes remain intact.
The new event participates in the existing locked, transactional hash chain.
Duplicate processing produces no extra terminalization event.

## Startup and policy safety

Startup validates the journal and makes no writes. The upgrade guard uses the
current IST date **only to defer already-expired records** to expiry processing.
Non-expired incompatible entry/exit-book policies still refuse startup.

Processing uses the actual observation's IST date, checks ordering, terminalizes
expired records before the unresolved early return or policy comparison, then
runs the existing guards for all remaining active positions. If replay/catch-up
first encounters a date on which an old-policy contract had not expired, it
still refuses execution under the new policy. It does not skip that evidence,
silently migrate its policy, or retroactively manufacture a fill.

Expired held legs are not requested from the broker for new captures. They do
not need quotes to expire. If capture is unavailable, state stays unchanged
until a valid observation can be processed; status/monitor reads do not expire
records using wall time. No broker order capability is involved.

Thus old expired October 19 records can pass startup on October 21 and close
**unpriced** on the first eligible observation processed. Any incompatible
non-expired record still requires resolution under its original policy. No
rows should be manually deleted/updated to bypass a guard.

## Accounting and monitor

Unpriced expiry records are neither active nor priced exits. They do not enter
win/loss, realized P&L, loss-streak, daily-loss or exit/loss cooldown calculations.
They retain their historical entry counts on their original entry/decision day.
The monitor exposes open_position_count, unresolved_exposure_count and
expired_unpriced_count separately; totals expose open_positions,
unresolved_exposures and expired_unpriced_count. Individual P&L remains null,
with CONTRACT EXPIRED / EXIT PRICE UNAVAILABLE displayed in the journal.

ORB historical decision rejections are counted once per strategy, IST session,
direction and features.breakout_timestamp. Missing identities are not invented;
legacy observations lacking a breakout timestamp retain their previous count.
Real pending-trade fill rejections remain separate, counted once per trade.
For future ORB signals, trade management always runs first. If exposure remains
active, the signal is BLOCKED / ACTIVE_POSITION_BLOCKS_ENTRY; no entry candidate
build or new trade is attempted. One regular OBSERVATION per cadence remains
for research evidence; this patch does not delete or deduplicate stored events.
