# ACTS_V1 frozen research strategy

`ACTS_V1` is the fifth, independent Phase 15 lab ledger. Its decision path is
directional futures regime → original Phase 14.1 Alpha validation → 3m/5m
option positioning → sustained countertrend pullback → 15-second resumption →
short-leg-first executable spread. A/B/C/D and the live scalper keep their own
logic and ledgers. Do not change ACTS_V1 parameters during prospective evaluation.
This is research, not broker order functionality.

## Original Alpha definitions and input policy

The lab reads existing `alpha_feature_snapshots.result_json` for version
`phase14_1_v1` and mode `LIVE_ORIGINAL`; it does not recompute or reinterpret
Alpha from 15-second prices. The existing Phase 14.1 formulas are:

- Alpha 1: the percentile midrank of the current **signed** 5-minute futures
  log return, `ln(F_t/F_reference)`, against prior same-futures-contract,
  comparable-horizon returns in 800 trading minutes. Midrank is
  `(count(prior < current) + 0.5 × count(prior = current)) / count(prior)`.
  At least 50 prior finite observations are required.
- Alpha 2: the percentile midrank of the current standardized return
  `ln(F_t/F_reference) / population_stddev(prior comparable 5-minute futures
  log returns)`, against prior same-futures-contract standardized returns in
  300 trading minutes. The underlying volatility uses a 300-minute lookback,
  at least five returns and a minimum valid level of `0.00001`. Alpha 2 also
  requires the original ATM CE/PE exact-contract continuity and selection
  gates; at least 50 eligible prior observations are required. The older
  `price_return × ATM_activity / option_volatility` impulse is diagnostic only,
  never the Alpha 2 used here.

The Phase 14.1 `direction` function applies signed-return continuation logic:
rank ≥0.80 with positive return is `STRONG_BULLISH`, 0.70–<0.80 is
`BULLISH`; rank ≤0.20 with negative return is `STRONG_BEARISH`, >0.20–0.30
is `BEARISH`; all other available combinations are `NEUTRAL`. The original
`joint_direction` combines Alpha 1 and 2. Its strong bullish/bearish
confirmations map to `STRONG_BULL`/`STRONG_BEAR`; ordinary confirmations map
to `BULL`/`BEAR`; `NEUTRAL` and `CONFLICT` map to `NEUTRAL`, with the original
joint direction retained as evidence. `INSUFFICIENT_DATA`, invalid Alpha,
wrong version/mode/identity, or causally unavailable Alpha map to
`ALPHA_UNAVAILABLE` and block entry. Any original Alpha 1 or Alpha 2
`STRONG_BULLISH`/`STRONG_BEARISH` signal opposite the trade vetoes, including
when the joint label is `CONFLICT`; an ordinary opposite component is recorded
as a contradiction but is not by itself a veto. Neutral and supportive
evidence may proceed.

The Alpha row must be from the same futures instrument/expiry and option
expiry, be at most 180 seconds old, and have `feature_calculated_at` (and, if
present, `response_received_at`) no later than the 15-second capture. The
latest matching already-calculated row is used; invalid evidence fails closed.
Missing Alpha is never
filled from futures price, 15-second score or broker `oi_change`.

## Frozen parameters and decisions

| Parameter | ACTS_V1 |
| --- | ---: |
| Futures 5m / 15m directional returns | ±4 / ±8 bps |
| Session range position | BULL ≥0.55; BEAR ≤0.45 |
| Opening range | Complete; spot beyond midpoint in trend direction |
| Available authoritative futures VWAP | Futures on trend side; slope aligned or zero |
| Unavailable VWAP | Recorded unavailable; no synthetic substitute |
| Alpha age and opposite veto | ≤180s; strong opposite joint confirmation veto |
| Option positioning | At least 2 of 4 slow checks; ≥3 opposite checks veto |
| Pullback / resumption | Countertrend activity persists ≥60s; then ≥3-point aligned fast move |
| Spread | One lot; short-leg-first; 100/200/300/400-point widths |
| Structure break | Spot and futures >10 points beyond held wall for 60s |
| Trend / strong Alpha reversal | Continuous adverse evidence for 60s |
| OI-positioning reversal | Continuous adverse 3m/5m evidence for 180s |
| Maximum confirmation gap | 30s; longer gaps reset the adverse timer |
| Profit / time | 50% entry-credit capture; 30-minute time stop |
| Daily ACTS entries | At most 3, or fewer if existing risk config is tighter |

BEAR positioning checks are: **A** CE resistance cluster ΔOI positive on 3m
or 5m; **B** the CE wall center is `WRITING` (same-contract OI rises while
mid-premium is flat/falling) on 3m or 5m; **C** PE support cluster ΔOI is
negative on 3m or 5m; **D** local ATM ±3 or ±5 OI PCR slope declines on 3m
or 5m. BULL mirrors these with PE support, CE resistance and rising PCR.
The opposite forms of at least three checks veto. One-minute changes are
diagnostic only. Exact-contract historical reads and same-basket PCR come
from the existing causal lab context.

The pullback starts on a countertrend 15-second move. Its extreme becomes
the anchor; countertrend activity must span at least 60 elapsed seconds.
Entry then requires an aligned 15-second move at least 3 points away from
that extreme while regime, Alpha and positioning still qualify. The existing
builder enforces exact quote identity, bid/ask, depth, credit, credit/width,
liquidity and defined-risk gates. ACTS ranks executable shorts by relevant
wall proximity, 3m/5m writing and premium/OI evidence, premium, distance,
and the existing short-quality and hedge economics. It does not select max OI
alone. Next-observation fill rechecks regime, Alpha, positioning and risk.

Open trades keep immediate forced-close, kill/event, spread-stop and hard-loss
exits. Thesis exits need elapsed confirmation of opposing 5m/15m futures,
strong opposite Alpha, material held-wall unwind with adverse slow positioning,
or a spot-and-futures break beyond the held wall. One noisy 15-second flip or
one 1-minute OI flip does not exit. Existing trailing settings apply only after
their positive-excursion activation; no holding minimum delays hard exits.

The manifest records `ACTS_V1`, all frozen parameters, the Git HEAD commit,
dirty-tree status and input hash. The ACTS decisions/trades persist evidence,
and `acts_ledger.jsonl` gives a concise trade ledger. `summary.json` includes
ACTS status, Alpha, positioning, regime, P&L, exit and blocker breakdowns.
Run prospective evaluation for at least 20 trading sessions and preferably
50+ executed ACTS trades before promotion decisions. October 8, if available,
is diagnostic historical context only, never a parameter-tuning target.
