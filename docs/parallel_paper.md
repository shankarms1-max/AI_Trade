# Parallel paper experiments

ACTS V2 continues in its existing worker and journal. The new worker runs ORB_15,
ORB_30, ORB_60, EMA_PULLBACK and SUPERTREND_RSI_VOLUME. It does not calculate Alpha
or evaluate ACTS. It defaults to disabled and supports PAPER mode only.

After installing this code and applying migration `0019_parallel_paper` in the
intended environment, these are the runtime commands (no deployment is performed
by them):

```bash
PARALLEL_PAPER_ENABLED=true python worker/parallel_paper_worker.py --mode PAPER
python worker/parallel_paper_worker.py --mode PAPER --status
```

In the runtime image the worker path is `/app/worker/parallel_paper_worker.py`.
The status command queries the database only, without initializing a broker,
evaluating strategies, advancing cursors or appending events. It reports each of
the five experiments separately and reads existing ACTS_V2 performance evidence.

## Data and causal bars

The five new strategies share one read-only Kotak capture every 15 seconds,
09:15–15:20 IST on configured trading days. This independent capture uses the
existing consumer-key market-data client; it does not log in to an order session.
ACTS keeps its existing capture and worker unchanged. The new mode is
`PARALLEL_PAPER` in the existing scalper snapshot/quote tables. Exact held legs and
the fixed option-volume basket are requested through the existing continuity
provider. Missing exact legs never receive an inferred or LTP fill.

There was no reusable candle aggregator in the capture architecture. One shared
builder now creates sampled futures OHLC bars, anchored at 09:15, with a default
3-minute interval. Prices are the actual 15-second futures observations, not
tick-complete exchange candles. Only bars ending at or before the observation are
usable. A tick exactly at a bar's end belongs to the next price bar. Missing
coverage, gaps over 45 seconds and futures identity/expiry changes invalidate
bars. Indicators use the latest contiguous valid completed bars, resetting across
gaps and trading sessions. Historical reads are bounded to the current IST session
and project scalar futures fields and only two option volume contracts; they do
not repeatedly load full session option chains.

The existing adapter supplies futures price, symbol and expiry, but **does not
capture reliable futures volume**. The technical experiment therefore uses the
explicitly labelled `FIXED_SESSION_ATM_CE_PE_CUMULATIVE_DELTA_V1` option participation
series. The basket is the first captured session's exact ATM CE and PE of its
option expiry. It never follows changing ATM strikes or substitutes contracts.
Each bar's volume is the sum of actual cumulative-volume differences for those
same two contracts, using causal observations at/before its boundaries (within
45 seconds). The pair is fetched through the existing REST all-quotes endpoint
on every cycle using its `last_volume` cumulative field, including while it remains
inside the ATM chain. Chain-volume fields are never mixed into this basket series.
Missing REST basket quotes explicitly clear volume rather than retain chain values.
Each individual counter must remain monotone, with fresh non-future
source timestamps. Missing contracts, quantities of volume, timestamps, boundary
observations or counter resets make volume unavailable. The first bar commonly
lacks an opening boundary baseline and is excluded from volume confirmation.
This is option participation volume, **not futures volume**. A valid option series
is required; the technical strategy stops qualifying entries when it is missing.
No unrelated series or synthetic volume is substituted. Authoritative futures
volume would require a separate, verified capture input and a new policy version.

## Entry rules

- ORB windows are exactly half-open `[09:15,09:30)`, `[09:15,09:45)` and
  `[09:15,10:15)`. Complete continuous coverage is required. A futures price
  strictly above the high or below the low must persist for 30 seconds, with no
  confirmation gap over 45 seconds. There are no Alpha, OI, VWAP or technical
  confirmation filters. By default each direction can trade once per session.
  Configured re-entry requires a new excursion after returning inside the range;
  an unchanged breakout cannot repeatedly enter. Range re-entry and reversal
  timestamps are retained on open trades for false-breakout analysis.
- EMA uses completed-bar closes with SMA-seeded EMA9 and EMA21. A trend-side
  counter-move must touch the EMA zone (5 bps tolerance), followed within five
  completed bars by a directional close beyond the fast EMA, a directional
  candle body and at least 2 bps of close-to-close resumption. Bull and bear rules
  mirror each other. There is no Alpha or positioning requirement.
- Supertrend uses Wilder ATR10 and multiplier 3, with its initial direction set
  by close versus bar midpoint. RSI14 uses Wilder smoothing (flat gains/losses
  give RSI50). Bull requires bullish Supertrend and RSI >=55; bear requires
  bearish Supertrend and RSI <=45. The latest completed bar's valid option-basket
  volume must be >=1.20 times the average of the **preceding** 20 completed bars.
  The current bar is excluded from that baseline. Missing volume blocks this
  strategy, and neutral RSI rejects it. At most one entry is committed per bar.

All signals select the existing balanced short-leg-first executable spread:
bull sells PE with a farther OTM PE hedge; bear sells CE with a farther OTM CE
hedge. OI/volume thresholds here are existing execution liquidity checks, not
strategy positioning confirmation. Entry is on a later observation using short
bid minus hedge ask. Marks and exits use short ask minus hedge bid. Depth units,
quote freshness, confirmed quantity, credit/width, maximum loss, non-expiry-day
entry and exact contract identity remain enforced.

## Independent state, policy and risk

Each strategy has its own cursor, watch/setup JSON, logical event journal,
sequence/hash chain, trade projection and policy hash. Shared physical tables are
partitioned by `strategy_id`, with composite ownership FKs and independent event
uniqueness. Events are append-only in SQL and ORM. Restart verifies hash chains,
trade ownership and cursor/projection agreement. Per-strategy transactions roll
back on failure; healthy strategies continue and capture continues while a failed
strategy waits for retry. Duplicate observations do not create another trade.

Defaults per new strategy are one position, six entries/day, three consecutive
losses, ₹50,000 daily gross loss limit, 09:15–14:45 entry window, 60-second exit
cooldown and 180-second loss cooldown. Other execution/risk/exit settings reuse
the existing `SCALPER_*` controls. Exits are standardized spread stop, maximum
loss, event/kill switch, profit target, trailing, time stop and forced square-off.
There is no bespoke ORB profit-taking rule. Missing exact exit evidence produces
terminal unresolved exposure for that strategy. Other strategies remain free to
trade. An execution-policy change cannot silently manage an old trade under new
rules: it becomes unresolved and its journal retains its original policy.

Set parameters with `PARALLEL_PAPER_CONFIG_JSON`. Unknown keys and invalid settings
are rejected. For example:

```json
{
  "bar_minutes": 3,
  "orb_confirmation_seconds": 30,
  "orb_allow_reentry": false,
  "ema_fast": 9,
  "ema_slow": 21,
  "atr_period": 10,
  "supertrend_multiplier": 3,
  "rsi_period": 14,
  "rsi_bull": 55,
  "rsi_bear": 45,
  "volume_baseline_bars": 20,
  "volume_min_ratio": 1.2,
  "risk": {
    "ORB_15": {"max_trades_per_day": 6, "max_consecutive_losses": 3},
    "EMA_PULLBACK": {"daily_loss_limit": 50000, "entry_end": "14:45:00"}
  }
}
```

The complete effective policy and its deterministic hash are stored in journal
evidence and trades. Changing one strategy's risk or technical parameters leaves
unrelated strategies' policies unchanged. Preserve parameters for the intended
30–40-session experiment; these initial defaults have not been optimized.

## Evidence and reporting

Trades retain strategy/version/id, decision/entry/exit timestamps, futures identity,
direction, underlying entry price, exact option legs, lot size, fill books,
credit/debit, strategy features, entry/exit reasons, holding time, MFE/MAE and
accounting. Rejected setups retain blockers and candidate rejection counts.
The report shows per-strategy daily trades, realized/unrealized P&L, open or
unresolved positions, cumulative wins/losses, expectancy, average win/loss,
profit factor, closed-trade maximum drawdown, MFE/MAE, holding time, false
breakout rate and completed trade evidence. Net P&L remains unavailable when
the inherited cost schedule is incomplete; partial cost estimates are labelled
`GROSS_ONLY`, not treated as audited net P&L.
