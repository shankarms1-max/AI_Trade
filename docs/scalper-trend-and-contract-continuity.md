# Trend-aware paper scalper and required-contract continuity

This change is paper only. It makes no trade quota or profitability claim. Phase 14
strategy, risk, cadence and normal chain membership are unchanged. The scalper's
short-leg-first spread construction, widths 100/200/300/400, one lot, one position,
and runtime ₹25,000 default maximum defined loss are unchanged.

## Phase 14 capture

The regular collector and one-shot recorder read identities from forward-paper
`PENDING_PAPER_ENTRY` and `OPEN` documents before each capture when forward paper
is enabled. They request only exact identities absent from the normal rolling
chain, through the existing Kotak read-only quotes batching helper (50 per call).
No broker order capability is introduced.

A nullable `market_snapshots.required_contracts_json` column, added by migration
`0017_contract_continuity`, holds the supplementary request list and matched fresh
quotes on the same snapshot. Missing responses remain absent. Original option
rows and journal evidence are not rewritten. The chain's single-expiry invariant
stays intact; supplementary quotes can retain the held expiry across a rollover.
`exact_contract` searches both lists and still rejects ambiguous identities.
Indicators and candidate generation continue to use only the normal chain.

Every supplementary market field comes from the current broker response. The
provider carries only identity, never old bid/ask, LTP, OI, volume or timestamps.
The SDK's documented all-quotes schema maps `open_int` and `last_volume` to current
OI and volume; missing fields remain unknown and pending-fill liquidity checks
still reject them. See [official SDK quote documentation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/quotes.md).
Source freshness, depth, bid/ask, non-0DTE, next-observation execution and journal
integrity checks remain in place. A genuine missing held quote still produces
`UNRESOLVED_EXPOSURE`; this change does not repair old unresolved trades.

Migration upgrade is additive and leaves old rows null. Explicit downgrade drops
only the supplementary column and thus discards its evidence; do not downgrade a
database whose new capture evidence must be retained.

## Captured intraday context

All context uses observations from the same IST session at or before the current
observation. The live service reads five scalar price/futures fields from the current
session through a timestamp-bounded query using the existing capture-time index.
Only the most recent `feature_lookback - 1` snapshots load option quotes (11 by
default); confirmation loads only the immediately preceding signal JSON. Earlier
option rows and feature/signal documents are not loaded for slow context. The first
observation does not roll out after 1,000 samples. Replay uses the same builders.
The first captured spot is a session observation reference, not an inferred
exchange opening print if capture began late.
PostgreSQL timestamps are normalized to IST on load so session entry and forced
exit gates use the same clock as collector timestamps and offline replay.

Stored context includes:

- First observed spot/time, session move in basis points, high, low, range and
  position in that observed range.
- Actual elapsed 1/3/5/15-minute spot returns and the exact reference timestamps.
  The reference is the most recent observation at or before the target. A
  reference more than 1.5 capture intervals before the target is unavailable.
- Signed multi-horizon agreement: returns of at least +1 bp vote +1, at most -1 bp
  vote -1, smaller returns vote 0; unavailable horizons do not vote.
- Distance from the observed 09:15–09:30 opening range, only after completion,
  at least 80% expected samples, a first sample by 09:16 and a last sample from
  09:29. No late-start fabricated opening range.
- Same-identity futures returns across each horizon and the 5-minute change in
  futures basis. Unknown or rolled futures identity contributes no confirmation.
- The existing fast momentum/local structure, plus OI/volume changes on matching
  option identities in both ATM windows. Chain membership changes are not OI flow.

## Scores and thresholds

Default configuration:

| Variable | Default / meaning |
| --- | --- |
| `SCALPER_TREND_ALIGNED_MIN_SCORE` | 68 |
| `SCALPER_MIXED_MIN_SCORE` | Unset: falls back to `SCALPER_SIGNAL_MIN_SCORE` (80) |
| `SCALPER_COUNTERTREND_MIN_SCORE` | 85 |
| `SCALPER_MIN_CONFIRMATIONS` | 2 |

Configuration enforces aligned < mixed <= countertrend, all within 0–100.
The legacy score setting remains the mixed default and a compatibility fallback
for historical signals without an adaptive threshold. It is no longer a universal
80-point gate. Replay supports explicit overrides for all three new thresholds.

Fast direction comes only from signed spot momentum plus local structure (net
magnitude at least 10). Fast conviction, bounded 0–100, uses:

| Component | Maximum absolute points | Scaling |
| --- | --- | --- |
| Momentum | 40 | existing weighted 1/3/5-observation returns / 6 bp |
| Local structure | 30 | 20 × clipped distance from recent mean / 8 points, plus 10 for local breakout |
| Futures | 20 | 17 × clipped one-observation return / 6 bp, plus 3 × clipped basis change / 5 points |
| Participation | 10 | mean normalized put-minus-call OI/volume change |

Signed evidence opposite the fast direction subtracts from conviction. Absolute
futures basis is no longer directional evidence. Book quality is stored as a
component for diagnosis but adds **zero** directional score points; executability
remains a separate hard candidate check.

Slow evidence weights sum to 100 when all inputs are available:

| Component | Maximum absolute points | Scaling |
| --- | --- | --- |
| Session move | 20 | / 40 bp |
| 1m / 3m / 5m / 15m returns | 5 / 10 / 15 / 15 | / 6 / 12 / 20 / 40 bp |
| Session range location | 10 | position mapped to -1…+1, range >= 10 bp |
| Opening-range break | 10 | signed outside-range distance / 10 bp |
| Same-contract 5m futures return | 12 | / 20 bp |
| 5m basis change | 3 | / 10 points |

Each scaled term is clipped to [-1,1]. Signed slow score is normalized by available
weights. A magnitude >=20 determines slow direction. A strong slow trend requires
magnitude >=60, aligned session move >=10 bp, aligned 3m return >=2 bp, aligned 5m
return >=4 bp and aligned horizon agreement >=0.75. Missing 3m/5m history therefore
cannot unlock the relaxed threshold.

When fast and slow agree and 5m context exists, combined score is 70% fast + 30%
slow conviction. Opposing slow context subtracts 10% of its conviction from fast
score. Otherwise fast score stands alone. This allows exceptional countertrend
conviction to qualify at the higher threshold rather than banning all such trades.
Only strong aligned context selects `TREND_ALIGNED`; opposing direction selects
`COUNTERTREND`; all remaining cases select `MIXED`.

Confirmation requires successive same-direction, same-regime observations, each
passing its own threshold. Direction/regime changes, failed thresholds or gaps
longer than 1.5 capture intervals reset it. Two qualifying observations suffice.
No execution or risk gate is relaxed by trend context.

## Decisions, exits and replay

Each new `signal_json` and immutable observation event stores fast/slow direction,
alignment, both scores, combined score, regime, applicable threshold, confirmation,
signal qualification, entry qualification, candidate counts/rejections and primary
blockers. `signal_qualified` means confirmed score evidence; `entry_qualified` also
requires an executable candidate and all risk gates. A captured row awaiting
processing explicitly has `ENTRY_NOT_EVALUATED` if its signal already qualifies.

Signal-failure exits now use the current adaptive threshold. A direction flip still
exits immediately at an available valid observed book, without waiting for entry
confirmation. Spread stop, structural invalidation, trailing exit, profit capture,
time stop and forced exit are unchanged. Missing executable quotes never become
synthetic exit prices.

Replay retains deterministic next-observation fills and `GROSS_ONLY` accounting
unless costs are complete. New summary/timeline fields include qualified signals,
entry-qualified observations, wins/losses, gross P&L, average winners/losers, gross
realized drawdown, direction/regime splits, missed aligned strong-trend observations
and their blockers, score and executed-entry score distributions. "Missed" includes
occupied and cooldown observations; these counts are not independent opportunities
or a quota. Existing stored scores from earlier versions may differ by design.

Use `python scripts/run_scalper_replay.py --help` for the existing offline CLI;
point it only at a local disposable copy of actual captured raw data. Do not infer
October 8 performance from synthetic test fixtures.

## Operational limits

No worker is enabled, deployment performed, production environment changed or
production database accessed by this change. Migration must precede a later rollout.
Policy hashes remain fail closed: code/policy changes while old paper exposure is
active can produce `PAPER_POLICY_CHANGED` / `SCALPER_POLICY_CHANGED`. Roll out while
flat under the existing policy; never rewrite old evidence or bypass that guard.
Old unresolved exposure remains unresolved. The separate Phase 15 rolling stream
does not receive Phase 14 continuity supplements.

Before judging opportunity frequency or profitability, replay multiple real sessions
and inspect book coverage, late-start context, regime transitions, turnover, costs
and end-of-session performance. In a local PostgreSQL 16 synthetic session with
1,500 observations and 42 options per snapshot, history plus feature calculation
took 2.232 seconds before the bounded quote-window change and 0.034 seconds after
it, with identical features. Prior option rows loaded fell from 62,958 to 462,
alongside 1,499 five-field price observations. These are single-run local timings,
not broker latency or production performance guarantees.
