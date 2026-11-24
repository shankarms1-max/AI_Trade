# ACTS_V3_MCX_FUTURES — directional outright futures paper strategy (CRUDEOILM, NATGASMINI)

> **Status: PRE_REGISTERED_UNVALIDATED.** No historical MCX futures data was
> available when this package was written. Every threshold is a pre-registered
> design choice. **No performance claim exists**, and none can be made until
> `research.py` has been run on real, complete, out-of-sample MCX bars.
> Paper only. Nothing in `backend/app/mcx_futures/` talks to a broker or the
> network, and nothing here can place an order.

## 1. What it is and what it is not

ACTS_V3_MCX_FUTURES trades **outright MCX futures**, one lot at a time, LONG or
SHORT, intraday only. It keeps four ideas from NIFTY ACTS V3:

- a 100-point evidence score with hard vetoes kept separate from it;
- the V3 opportunity cycle (ARMED / RESERVED / ENTERED / CONSUMED / REARMED);
- a hash-chained, append-only journal;
- strict, explicit risk limits.

It deliberately **does not** reuse any NIFTY options assumption: there is no
option chain, no Greeks, no OI walls, no PCR and no credit spreads. Its
evidence comes only from the futures contract's own price, volume and quote.

| Module | Responsibility |
|---|---|
| `spec.py` | `ContractSpec`, `resolve_futures` (nearest FUTCOM outside the expiry window), `round_to_tick` |
| `session.py` | MCX session calendar (IST): seasonal close, holidays, per-date overrides, cutoffs |
| `bars.py` | causal 1-minute bars from 15-second quotes, gap detection, N-minute resampling |
| `features.py` | EMA/slope, Wilder ATR, session VWAP, ADX/DI, breakout distance, efficiency ratio, ATR percentile, k-bar return, spread, quote age |
| `config.py` | two independent, versioned, hashed `InstrumentConfig`s; US event blackouts |
| `score.py` | 100-point LONG/SHORT score, market state, hard vetoes |
| `cycle.py` | V3 opportunity-cycle semantics (pure functions over a JSON dict) |
| `risk.py` | stop sizing, per-trade, daily and streak limits, cooldowns, gap guard, expiry and time cutoffs |
| `costs.py` | dated, itemised MCX non-agricultural futures cost schedule, with a status on every rate |
| `paper.py` | conservative fills, stops, trailing stop, P&L, MAE/MFE |
| `engine.py` | `McxFuturesEngine` (observations in, journal events out, snapshot/restore) |
| `replay.py` | 1-minute-bar replay through the unchanged engine, plus metrics |
| `research.py` | splits, walk-forward, grid registry, baselines, fragility, credibility verdict |

## 2. Contract identity (`spec.py`)

- The spec is resolved only from Kotak scrip-master rows with `pSymbolName`
  equal to the instrument, `pInstType == "FUTCOM"`, `pOptionType == "XX"` and
  `pExchSeg == "mcx_fo"`.
- `tick = dTickSize / 10**lPrecision`. `lot_size = lLotSize` units per lot, so
  `rupees_per_point_per_lot = lot_size`. Lot size and tick are never hardcoded.
- **Verified facts** (real master of 2026-10-22): CRUDEOILM lot 10 with tick
  1.00 (dTickSize 100, lPrecision 2), and NATGASMINI lot 250 with tick 0.10
  (dTickSize 10, lPrecision 2). Quotation units are BBL and MMBTU.
- `pExpiryDate` (`19Nov2026`) must agree with `lExpiryDate`. `lExpiryDate` is
  the expiry day at 23:59:59, expressed as UTC epoch seconds; this was checked
  against the 2026-10-20 fixture.
- **Rollover rule:** choose the nearest expiry `>= today + expiry_window_days`
  (3 days). A contract inside its expiry window is never entered; the next
  contract is used instead.
- **Refusal:** a malformed candidate row (lot, tick, precision, expiry or
  identity), two FUTCOMs with the same expiry, or no eligible contract raises
  `SpecError`. The master is never partially trusted.

## 3. Session calendar (`session.py`)

- Open 09:00 IST. Close 23:55 IST from 2026-11-02 to 2027-03-12 (MCX circular
  MCX/TRD/550/2026, US winter time). Close 23:30 IST otherwise. Weekends are
  closed.
- `holidays` and `overrides` are configuration. An evening-only session is
  `DayOverride(open=17:00, close=None)`, and a full closure is
  `DayOverride(None, None)`. **The default holiday list is empty.** Operations
  must load it from the MCX holiday circular; this package does not invent
  holidays.
- Cutoffs are per instrument, measured in minutes before the actual close of
  the day: last entry 30 min before close and forced exit 10 min before close.
  That gives 23:25 / 23:45 in winter and 23:00 / 23:20 in summer, and the
  cutoffs follow any override.
- `state(at)` accepts any timezone-aware datetime and returns
  `OPEN | CLOSED | PRE_OPEN` with the next open.

## 4. Bars and features

- Observations arrive at roughly 15-second intervals. An observation whose time
  is at or before the last processed one is rejected (`DUPLICATE_OBSERVATION` /
  `OUT_OF_ORDER_OBSERVATION`). Malformed or crossed quotes are rejected with
  their reason.
- A 1-minute bar is complete only when an observation from a later minute
  arrives, or when the session ends. Missing minutes are reported as gaps, and
  bars are never fabricated.
- Bar volume is the delta of cumulative day volume. The first observation of a
  session is only a baseline, and a decrease in cumulative volume makes that
  bar's volume `None`.
- Feature bars are clock-aligned N-minute buckets (CRUDEOILM 3, NATGASMINI 5).
  A bucket that has missing minutes is still emitted, but it carries
  `missing_minutes`, and the hard veto `DATA_GAP_IN_BAR` blocks entries on it.
- Indicator history runs continuously across sessions and is bounded to
  `feature_window_bars` = 160 bars. VWAP covers the current session only.
  VWAP without volume is `UNAVAILABLE`; it is never replaced by a substitute.
- Warm-up is `max(ema_slow + slope_bars, 2*adx + 1, breakout + 1, er + 1,
  atr + atr_percentile_min_samples, momentum + atr + 1)`, which is 54 bars for
  both configs. Until then every feature is `None` with reason `WARMUP n/54`.

## 5. Score (`score.py`): 100 points, no rescaling

| Component (max) | Evidence |
|---|---|
| trend (25) | EMA(9) vs EMA(21) stack (40%), plus the slow-EMA slope over 3 bars / ATR, scaled to `slope_full_atr` (60%) |
| momentum (20) | 3-bar return / ATR, scaled to `momentum_full_atr` |
| structure (20) | side of session VWAP in ATR (50%), plus breakout distance vs the prior 20-bar high/low in ATR (50%; 0.5 at the level) |
| strength (15) | ADX between `adx_floor` and `adx_full`, only when the DI agrees with the direction |
| regime (10) | efficiency ratio above the chop threshold (60%), plus ATR percentile inside the volatility band (40%) |
| execution (10) | spread in ticks (60%), plus quote freshness (40%) |

- Direction is LONG when EMA fast > slow and the slope > 0, and SHORT in the
  mirror case. Market state is one of `NEUTRAL`, `{DIR}_TREND`,
  `{DIR}_PULLBACK` (a 3-bar move against the trend of at least `pullback_atr`)
  or `{DIR}_CHOP`.
- An unavailable input scores 0 and is listed under `unavailable`. The total is
  never rescaled.
- **Hard vetoes** are separate from the score: `WARMUP_INCOMPLETE`,
  `ATR_UNAVAILABLE`, `QUOTE_UNAVAILABLE`, `SPREAD_TOO_WIDE`,
  `QUOTE_AGE_UNAVAILABLE`, `STALE_FEED`, `EVENT_BLACKOUT`,
  `OUTSIDE_ENTRY_WINDOW`, `EXPIRY_WINDOW` and `DATA_GAP_IN_BAR`.
- **The entry floor of 60 is a design choice for this NEW score.** It is not
  inherited from NIFTY. `research.py` grids it over {55, 60, 65, 70}.

## 6. Opportunity cycle (`cycle.py`)

The cycle keeps the semantics of V3, applied per instrument and per IST
session:

- **FIRST_OPPORTUNITY** arms a direction that has not been consumed or
  quarantined. Entry needs `confirmation_bars` consecutive qualifying decisions
  (CRUDEOILM 1, NATGASMINI 2).
- **Consumption happens only on FILL.** A decision moves the cycle to
  `RESERVED`. An unfilled or rejected entry calls `released()`, which restores
  `ARMED`/`REARMED` without consuming the direction.
- **PULLBACK_RESUMPTION** (or OPPOSITE_PULLBACK_RESUMPTION): while flat, a
  `{DIR}_PULLBACK` state is recorded. A confirmed full signal in that direction
  within `setup_lifetime_minutes` re-arms it.
- **RETURN_AFTER_FALSE_REVERSAL:** after an `OPPOSITE_SIGNAL` exit, the
  previous direction may re-arm when it returns.
- **Reversal quarantine:** an `OPPOSITE_SIGNAL` exit quarantines the opposite
  direction. It can only be entered through **EXCEPTIONAL_REVERSAL_CONFIRMATION**:
  a score of at least 85 (natgas 88) sustained for
  `reversal_confirmation_seconds` (360 s crude, 600 s natgas). The spacing
  between consecutive decisions may not exceed
  `bar_seconds + 1.5 × observation_interval_seconds`, which ties the gap
  tolerance to the 15-second observation cadence.
- A new session resets the cycle and the daily risk counters.

## 7. Risk (`risk.py`)

| Rule | CRUDEOILM | NATGASMINI |
|---|---|---|
| Stop | max(1.5×ATR, 8 ticks), rounded up to the tick | max(2.0×ATR, 10 ticks), rounded up to the tick |
| Lots | 1 | 1 |
| Max risk per trade (stop_pts × lot × lots) | Rs 2,500 | Rs 4,000 |
| Daily realised loss limit (entry also refused if the stop would breach it) | Rs 6,000 | Rs 8,000 |
| Max entries per session / consecutive losses | 4 / 3 | 3 / 2 |
| Cooldown after exit / after loss | 5 / 15 min | 10 / 20 min |
| Stale quote (entry veto) / stale-feed exit | 45 s / 120 s | 45 s / 120 s |
| Gap guard (open or after an outage, vs last price) | > 1.5 ATR → 15 min | > 2.0 ATR → 20 min |
| Expiry window (no entries) | < 3 days | < 3 days |
| Trailing stop: activation / trail | 1.0 / 1.5 ATR | 1.5 / 2.0 ATR |
| Max holding | 120 min | 180 min |
| Spread limit | 3 ticks (Rs 3.00) | 4 ticks (Rs 0.40) |

Only one position per instrument is allowed, and positions are intraday
only.

## 8. Paper execution (`paper.py`)

- **Entry:** a market order fills at the **next observation** after the
  completed-bar decision, at the ask for LONG and the bid for SHORT (plus
  `entry_slippage_ticks`, which defaults to 0). The fill is allowed only when
  the quote is two-sided, fresh (age 45 s or less), within the spread limit,
  within `max_entry_latency_seconds` = 30 s, before the last entry time and
  outside any blackout. Otherwise the result is `ENTRY_REJECTED` with the exact
  reason, and the reservation is released without consuming the direction.
- **Stop:** the stop is checked on every fresh observation against the bid
  (LONG) or ask (SHORT). It fills at the **worse of the stop and the observed
  executable price**, which models gap-through, minus or plus
  `stop_slippage_ticks` = 1.
- **Exits:** stop, ATR trailing stop (updated on completed bars), opposite
  full signal (score at or above the floor in the opposite direction, held for
  `confirmation_bars`, filled at the next observation), max holding time,
  session forced exit, and stale-feed exit. The stale-feed exit triggers only
  on the first fresh quote after more than 120 s without one. Stale quotes are
  never marked (`MARK_UNAVAILABLE`).
- **P&L:** `(exit - entry) × direction × lot_size × lots`, reported gross and
  net of itemised costs, with MAE and MFE in points and rupees.
- A position still open when the session closes (for example because no
  quote arrived before the forced-exit time) is exited at the first fresh
  quote of the next session with reason `SESSION_FORCED_EXIT_LATE`, and its
  loss counts against the new session. This handling is conservative.

## 9. Costs (`costs.py`, `MCX_NONAGRI_FUT_COSTS_2026_V1`)

| Item | Rate | Basis | Status |
|---|---|---|---|
| CTT | 0.01% | sell-side value | STATUTORY (Finance Act 2013, non-agri futures) |
| Stamp duty | 0.002% | buy-side value | STATUTORY (Indian Stamp Act amendment, effective 2020) |
| SEBI turnover fee | Rs 10 per crore | turnover | STATUTORY |
| GST | 18% | brokerage + exchange + SEBI | STATUTORY |
| MCX transaction charge | Rs 2.10 per lakh | turnover | **UNVERIFIED_ASSUMPTION** — verify against the current MCX circular |
| Brokerage | Rs 20 per executed order (scenarios 0 / 10 / 20) | order | **ASSUMPTION** |

Contract value is `price × lot_size × lots`. Hand-checked reference examples
are covered by tests:

- **CRUDEOILM long 5600 → 5615:** gross Rs 150. Charges: CTT 5.615, stamp
  1.12, SEBI 0.11215, exchange 2.35515, brokerage 40, GST 7.644114, for a total
  of Rs 56.85 and a net of Rs 93.15.
- **NATGASMINI short 310.0 → 308.5:** gross Rs 375. Charges: CTT 7.75, stamp
  1.5425, SEBI 0.154625, exchange 3.247125, brokerage 40, GST 7.812315, for a
  total of Rs 60.51 and a net of Rs 314.49.

At Rs 20 per order, brokerage is about 70% of the round-trip cost. A crude
trade needs roughly 6 points (6 ticks) just to break even, so cost sensitivity
must be part of any evaluation.

## 10. Engine and journal (`engine.py`)

```python
engine = McxFuturesEngine(config, spec, costs, state=None, calendar=None)
events = engine.on_observation(obs)        # -> list[dict], JSON-safe
state  = engine.snapshot()                 # JSON-safe full state
engine = McxFuturesEngine.restore(config, spec, costs, state, calendar=None)
```

Each event has the shape
`{seq, type, at, strategy_id, instrument, trading_symbol, config_id, payload, prev_hash, hash}`,
where `hash = sha256(canonical JSON of everything else)` and the events form a
chain. The event types are `OBSERVATION_REJECTED`, `BAR`, `DECISION`,
`ENTRY_PENDING`, `ENTRY_FILLED`, `ENTRY_REJECTED`, `MARK`, `MARK_UNAVAILABLE`,
`EXIT` (its payload carries the full `trade` and the risk state), `CYCLE`,
`SESSION` and `RISK_BLOCK`.

Engine guarantees:

- Decisions are made only on completed feature bars, and fills happen only on
  later observations.
- The snapshot carries the identity (config hash, spec, cost version and
  calendar source). Restoring it with a different identity is refused.
- Restoring a snapshot and replaying the same observations reproduces
  identical events. Re-sending an already processed observation is rejected
  without any state change.
- Instruments are independent, with no module-level mutable state.

## 11. Replay (`replay.py`)

`replay(bars_df, config, spec, costs, half_spread_ticks=1.0, calendar=None, collect_events=False)`
takes a DataFrame with the columns `ts` (timezone-aware IST minute start),
`open`, `high`, `low`, `close` and `volume`, plus optional `bid`/`ask`.

**Quote synthesis:** each bar becomes four observations, at +0 s (open),
+15 s and +30 s (the extremes) and +45 s (close). Without bid/ask in the data,
the quote is `ltp ± half_spread_ticks × tick`, and the half spread can be
tested with `spread_sensitivity` at 0.5, 1 and 2 ticks.

**Conservative path:** a pending LONG entry sees the high first, so it fills
at the worst price, and an open LONG position sees the low first. SHORT
positions are mirrored.

**Intrabar stop:** when a path segment crosses the stop, an observation is
inserted at the point where the executable side touches the stop, so the fill
is the stop minus or plus slippage. If the bar opens beyond the stop, the fill
is at the open's executable price (gap-through).

**No look-ahead:** the events produced from a prefix of the bars are exactly
the prefix of the events from the full data. A test checks this.

Replay assumes all bars belong to one contract. Replay each contract segment
with its own spec.

Replay reports:

- the trade ledger;
- trade statistics: trade count, win rate, expectancy, t-stat, average win
  and loss, and profit factor;
- maximum drawdown and maximum consecutive losses;
- average holding time, MAE and MFE;
- cost totals by item, the worst day, and exit-reason counts.

## 12. Research harness (`research.py`)

`run_research(bars, base_config, spec, costs, grid=DEFAULT_GRID, plan=ResearchPlan(), calendar=None)`
works in the following steps:

1. **Data sufficiency first.** Too few sessions (fewer than 20 by default) or
   any session with bar coverage below 95% returns `status: INSUFFICIENT_DATA`
   with reasons, an empty registry and **no numbers**.
2. **Pre-declared grid.** `entry_score_floor` {55, 60, 65, 70} ×
   `stop_atr_multiple` {1.25, 1.5, 2.0} × `feature_timeframe_minutes` {3, 5}
   gives 24 configs. Every evaluated config is registered with its hash and
   its train, validation and OOS summaries, and the result includes
   `evaluated_config_count`.
3. **Chronological split by session.** The split is 60% train, 20% validation
   and 20% OOS. Trades are attributed by entry session, and each config is
   replayed once over the full history (the strategy is causal and has no
   in-sample fitting).
4. **Selection on train only.** The objective is
   `train net P&L − λ × fragility`, where fragility is the dispersion of train
   net P&L across the config and its one-step grid neighbours. A config needs
   at least 10 train trades to be eligible.
5. **Walk-forward.** The history is split into 5 blocks. Each fold trains on
   an expanding window and tests on the next block, and the test results of
   each fold are reported.
6. **Baselines** use the same cost model: no-trade, and an EMA(9/21)-cross
   that is always in the market during the entry window and flat at the forced
   exit.
7. **Regime segmentation** of the selected config's OOS trades into
   TREND / CHOP / HIGH_VOL, using the efficiency ratio and ATR percentile at
   entry.
8. **Verdict.** `CREDIBLE_EDGE_CANDIDATE` is returned only if all of the
   following hold. Otherwise the verdict is `NO_CREDIBLE_EDGE`, and the failed
   checks are listed.
   - at least 30 OOS trades;
   - OOS net P&L above zero;
   - OOS t-stat of at least 2;
   - OOS profit factor of at least 1.2;
   - at least 75% of walk-forward folds positive;
   - the config beats the EMA baseline and no-trade on OOS.

   A seeded random walk is tested to come out as `NO_CREDIBLE_EDGE`.

## 13. CRUDEOILM vs NATGASMINI

The two configs are separate objects with separate hashes. NATGASMINI is
treated as the noisier, gappier and more event-driven contract, so compared
with CRUDEOILM it uses:

- **Bars and confirmation:** 5-minute bars instead of 3, and 2 confirmation
  bars instead of 1. The exceptional-reversal floor is 88 instead of 85, with
  600 s of confirmation instead of 360 s.
- **Noise envelope:** a 2.0 ATR stop instead of 1.5, and a 10-tick minimum
  stop instead of 8. Pullback 0.75 ATR (crude 0.5), trailing stop 1.5 / 2.0 ATR
  (crude 1.0 / 1.5), gap guard 2.0 ATR for 20 min (crude 1.5 ATR for 15 min).
- **Regime filters:** chop ER below 0.35 instead of 0.30, ADX between 20 and
  38 instead of 18 and 35, and a volatility band from the 15th to the 85th
  percentile instead of the 10th to the 90th.
- **Risk budget:** Rs 4,000 per trade and Rs 8,000 per day (crude Rs 2,500 and
  Rs 6,000). 3 entries and 2 consecutive losses per session (crude 4 and 3),
  cooldowns of 10 / 20 min (crude 5 / 15), and a maximum hold of 180 min
  (crude 120).
- **Spread limit:** 4 ticks (Rs 0.40) instead of 3 ticks (Rs 3.00).
- **Event blackout:** the EIA Weekly Natural Gas Storage Report, Thursdays
  10:30 ET, from 10 min before to 20 min after. CRUDEOILM uses the EIA Weekly
  Petroleum Status Report, Wednesdays 10:30 ET, from 10 min before to 15 min
  after.

Event times are computed in America/New_York and converted to IST, so DST is
handled. In winter 10:30 ET is 21:00 IST, and before 2026-11-01 it is 20:00
IST. Holiday-shifted releases are configured through `ScheduledEvent.shifted`
and `extra`; none are preloaded, because none were verified. Thanksgiving week
(2026-11-26) is a known candidate for a shift and must be checked against the
EIA schedule.

## 14. Verified vs assumption

**Verified:**

- lot and tick facts for both contracts (2026-10-22 master);
- the format of the Kotak master rows and the `lExpiryDate` epoch convention;
- the MCX winter close of 23:55 from 2026-11-02 to 2027-03-12 (circular
  MCX/TRD/550/2026), as given in the task brief.

**Statutory rates as encoded:** CTT, stamp duty, the SEBI fee and GST.

**Assumptions and unverified items:**

- the MCX transaction charge of Rs 2.10 per lakh;
- brokerage of Rs 20 per order;
- the default 23:30 summer close outside the configured window;
- the holiday list, which is empty;
- EIA holiday shifts;
- the half-spread and path assumptions in replay;
- **every strategy threshold**, all of which are PRE_REGISTERED_UNVALIDATED.

**Not available:** historical MCX futures data. Consequently there is **no
backtest, no calibration and no performance claim** of any kind. The tests use
only synthetic data, labelled as SYNTHETIC.
