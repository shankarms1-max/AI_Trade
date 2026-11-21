# Adaptive SuperTrend V1: isolated paper/research engine

AST_V1 is an independent NIFTY futures signal and option credit-spread paper experiment.
It does not modify ACTS V2/V3, run the retired parallel strategies, or appear in the public
ACTS-only Phase 15 API/frontend. No profitability claim is made. No live mode or order
adapter exists. Nothing in this commit deploys or enables a worker.

## Architecture and frozen facts

`app.adaptive_supertrend` owns its indicators, policy, optimizer, decisions, projections,
journal, replay and reporting. `worker/adaptive_supertrend_worker.py` is a separate
PAPER-only entry point, disabled by default with `ADAPTIVE_SUPERTREND_ENABLED=false`.
Use a separate research database/instance; no production Compose/Caddy changes are provided.

Market-data-only capture reuses the existing collector factory and shared evidence tables
`scalper_market_snapshots` / `scalper_option_quotes` with explicit **AST_PAPER** provenance.
Only AST-mode rows enter AST history/catch-up. ACTS and parallel journals are never queried
or written by the service. Exact held option identities are supplied to the read-only
collector; if a required quote cannot be captured, there is no ATM/expiry substitution.

Five-minute sampled futures OHLC bars are anchored at 09:15 Asia/Kolkata. They contain
only observations in `[start,end)` and become available at `end`. An incomplete bar is
never an indicator input. Source sample gaps above the configured tolerance, missing
futures identity/expiry, expired futures, contract changes and missing bars are explicit
invalid boundaries; indicator warm-up restarts after a boundary. CSV research input must
provide aware five-minute OHLC timestamps and futures instrument/expiry identity.
These are sampled bars, not a claim of exchange tick-complete highs/lows.

Wilder ATR seeds with the first period's true-range mean and recursively smooths thereafter.
SuperTrend uses final upper/lower bands, previous completed close and prior direction.
An initial equality/doji selects BULL deterministically; equality at an established band
preserves direction. ADX uses Wilder directional movement (first ready at index `2p-1`);
ER is absolute displacement divided by absolute price path and returns zero on a flat path.
No RSI or option-volume confirmation is used.

The exact parameter grid is 46 ATR periods (5–50) × 41 integer multiplier tenths (10–50):
**1,886 configurations**. No floating-point stepping, worker pool or per-configuration
pandas copy is used. ATR arrays are shared per period. Grid evaluation is sequential.

## Causality and daily freeze

For session D, optimizer data must be from dates **strictly before D**, end before D 09:15,
and be available at `selected_at`, which itself must be before D 09:15. Parameters never
fallback to (10,3), change intraday or use D's eventual results.

`ast_optimizer_runs` stores the complete training-bar input, its hash, explicit policy,
full ranking/metrics and deterministic run identity. `ast_session_parameters` stores the
selected period/tenths, date, selection clock, actual last training-data cutoff, policy
hash, input identity, metrics, neighbour evidence and walk-forward status. These records
are immutable. A second freeze returns the existing identical policy or refuses a mismatch.
The worker attempts selection at startup before the session and at 09:00 IST; missing
history/eligible configurations blocks selection rather than inventing parameters.
An intraday restart loads that day's frozen object and never re-optimizes it.

The 15-minute opening range is exactly three valid completed bars covering
**09:15 inclusive through 09:30 exclusive**. Its high/low/date/time evidence is frozen
in the AST cursor and copied into every trade. Later prices cannot revise it. A futures
identity change during the session blocks entries rather than using a new contract against
an old range.

After range completion:

- Current futures above the range + completed AST BULL + explicit regime policy pass → BULL,
  construct a PE credit spread.
- Current futures below the range + completed AST BEAR + explicit regime policy pass → BEAR,
  construct a CE credit spread.
- Disagreement → `AST_ORB_DIRECTION_CONFLICT`; inside → `ORB_NOT_BROKEN`.
- Unavailable indicators, range, causal observation timing, policy or daily parameters →
  precise blockers; capture can continue without an entry.

An opportunity hashes trading date, direction, exact frozen range, frozen parameters,
AST transition identity and futures identity. Only a confirmed simulated fill consumes it.
Continued persistence after an exit cannot create another trade for the same opportunity.
A later causal AST transition has a different opportunity identity. No arbitrary cooldown
is used as the primary duplicate guard.

## Explicit unresolved policy (entry remains fail-closed)

`AST_CONFIG_JSON` is a validated extra-forbidden schema. Default `{}` reports
`AST_POLICY_UNCONFIGURED`. Required research choices are deliberately **not guessed**:

- optimizer lookback sessions, chronological subperiods, minimum acceptable trades;
- all metric weights: profitability, drawdown, profit factor, Sharpe-equivalent,
  frequency, consistency, neighbours;
- validation and out-of-sample session counts;
- explicit regime `NONE`, or `ADX_ER` with ADX period/threshold and ER lookback/threshold;
- allowed spread widths, entry end, minimum short distance/credit/credit-width ratio;
- maximum spread percentage and quote age, minimum OI and volume;
- max defined loss, daily loss, trades/day, loss streak, both cooldowns and entry TTL;
- forced exit time and an explicit safety policy;
- supported, dated costs/slippage if net P&L is required.

Frozen facts (AST_V1, 5-minute bars, 15-minute range, exact grid, one lot) cannot be overridden.
One pending/open/unresolved position is the fixed maximum. Offset defaults to seconds
10,25,40,55 and is validated in `[0,15)`; this is scheduling, not strategy policy.
Sample coverage tolerance defaults to the existing 45-second infrastructure convention.
`kill_switch` and configured market-event blocks are enforced at decision and fill and
trigger available-book safety exits. All policy inputs and implementation-policy version
identifiers are hashed into new trade evidence. Active trades reject incompatible policy
changes on startup. Historical policies/events are never rewritten.

## Execution and exits

The existing short-leg-first candidate builder and exact quote validators are reused as
pure functions. Widths are explicit AST policy; AST does **not** invent a 400-point rule.
One-lot defined maximum loss is checked at decision and again at fill. Entry requires real
short BID minus hedge ASK on a **later** observation; the decision cannot fill itself.
Freshness, tick alignment, depth units/quantity, minimum credit, OI/volume, risk counters,
event blocks, current thesis/opportunity and exact futures/lot identity are rechecked.
LTP is never an executable fill. No partial depth or inferred quantities are accepted.

Marks/exits use exact held short ASK minus hedge BID and the original confirmed lot size.
The proven `PHASE15_EXIT_BOOK_V1` allows a wide spread for risk exits while retaining all
other executable-book protections. Missing marks are explicitly unavailable. Missing exact
contracts or a missing executable book at forced exit produce terminal
`UNRESOLVED_EXPOSURE`; expired positions without an exit price remain unresolved.
There is no invented settlement or fabricated close.

There is no invented AST strategy exit. The selected safety policy is explicit:

- `HARD_SAFETY_ONLY`: forced close, kill/event exit, hard cash loss and daily-loss exit.
- `LEGACY_SAFETY_BASELINE`: additionally requires explicit profit %, stop multiple,
  trailing activation/giveback and time stop. These remain labelled legacy safety in
  policy, trade and research outputs; they are **not** AST-optimized rules.

Costs reuse the existing dated `CostSchedule`. Gross is always separate. Net stays null
unless a complete supported schedule is configured; unknown costs are never zeroed into net.

## Persistence, locks and recovery

Migration `0022_adaptive_supertrend` adds only:

- `ast_optimizer_runs`
- `ast_session_parameters`
- `ast_paper_cursor` (one seeded authoritative row)
- `ast_paper_trades`
- `ast_paper_events`

PostgreSQL triggers prohibit UPDATE, DELETE and TRUNCATE on evidence; SQLite triggers
prohibit row UPDATE/DELETE. ORM mutation guards also apply. Events contain unique causal
keys, previous/event hashes and full transition evidence. Cursor CAS/row lock, trades and
events commit atomically; failures roll back the entire observation. Duplicate or earlier
observations cannot refill a trade. Catch-up pages AST captures in causal timestamp order
before making new network calls. Startup checks indexed journal head/observation and active
projections, without scanning the journal. Full verification is a separate read-only,
streaming command. No reset/repair/deletion command is supplied.

Indexed scalar reads load historical completed bars once per service/session/futures identity;
subsequent cycles query only today's scalar prices. Only the current observation loads an
option chain. The optimizer is pre-session, never on the 15-second decision path. There is
no history truncation to a magic 1000-row window.

## Research, metrics and walk-forward

Signal research reports **futures signal points**, never option rupees. Entry signal at a
completed bar fills only at the next bar open. The explicit signal-research baseline exits
on a directional flip at next bar open or a scheduled session-end signal mark. A separate
`SESSION_END_ONLY` signal baseline is supported. Missing session-end evidence leaves a
signal position unpriced/excluded, never backfilled. These are signal-quality statistics,
not the paper spread safety-policy results.

Metrics include trade count, wins/losses, expectancy, PF (null plus reason when undefined),
max drawdown, unannualized trade-return Sharpe-equivalent, win rate, mean win/loss, payoff,
trades/session and chronological subperiod consistency. Selection requires configured
minimum trades and positive gross signal points; configured percentile-based metric weights
and the four orthogonal parameter neighbours' median scores provide deterministic robust
ranking. Ties use ATR period then integer tenths. Highest raw P&L alone is not the selector.

Walk-forward uses contiguous training → validation → out-of-sample blocks, never shuffle.
Each period's parameters are chosen only from its training window. Validation/OOS metrics
use causal training prefixes for indicator warm-up, with only period entries counted.
Daily freeze reports `NOT_RUN` unless separate walk-forward research has been performed;
no out-of-sample success is fabricated.

Execution research optionally reads **actual** `ScalperMarketSnapshot` JSONL and runs the
same paper service against an ephemeral, isolated SQLite database. It stores exact quote
fills and hash-verifies the replay. It cannot write a source DB or production journal.
Repeat with a different explicit safety policy to compare exits. If no option evidence is
supplied, executable option P&L stays unavailable. No source CSV reconstructs missing quotes.

## Commands (local/research only)

```sh
PYTHONPATH=backend python -m pytest backend/tests/test_adaptive_supertrend*.py -ra

# CSV columns: start,end,open,high,low,close,future_instrument_id,future_expiry
PYTHONPATH=backend python -m app.adaptive_supertrend.research \
  --csv /research/futures-5m.csv --policy /research/ast-policy.json \
  --date 2026-11-20 --grid --walk-forward --top 20 --json /research/results.json

# Same grid, measured single-process runtime and peak RSS (benchmark timing is nondeterministic).
PYTHONPATH=backend python -m app.adaptive_supertrend.research \
  --csv /research/futures-5m.csv --policy /research/ast-policy.json \
  --date 2026-11-20 --grid --benchmark --json /research/benchmark.json

# Optional actual option quote replay, in an automatically removed ephemeral DB:
PYTHONPATH=backend python -m app.adaptive_supertrend.research \
  --csv /research/futures-5m.csv --policy /research/ast-policy.json \
  --date 2026-11-20 --grid --observations /research/snapshots.jsonl \
  --json /research/execution-results.json

# DATABASE_URL must point at an isolated migrated AST research database.
python worker/adaptive_supertrend_worker.py --mode PAPER --status
python worker/adaptive_supertrend_worker.py --mode PAPER --verify-journal
# Requires explicit enabled flag and complete approved AST policy; not run by this commit:
python worker/adaptive_supertrend_worker.py --mode PAPER
```

No optimizer weights, thresholds, risk settings or exits in the synthetic tests constitute
approved production policy. Real option-data availability, real worker latency, strategy
research validation and a configured policy remain prerequisites for a future paper run.

### Validation notes

The optimizer requires at least one nonzero quality/risk/robustness weight in addition
to the positive-profit/minimum-trades eligibility checks; a profit-only ranking policy
is rejected. Known cooldown windows include previous-session exits rather than losing
that evidence at midnight. The only active-policy safety exception is a **false→true
kill switch with every other policy field identical**. It closes using the real book,
retains the original frozen trade policy/hash, and appends explicit emergency-override
hash/reason evidence. Any other active-policy change still refuses startup.

A synthetic 20-session / 1,500-bar full-grid run completed all 1,886 configurations in
76.939 seconds, with peak RSS 81.734 MiB, one process, two-CPU affinity and a 3 GiB
address-space cap. This measures the local research grid, not production broker latency
or strategy profitability. Results/evidence are in `/workspaces/ast-v1-validation`.

The existing shared capture schema derives `source_market_timestamp` from the maximum
option source timestamp; it does **not** expose an authoritative futures source timestamp.
That option aggregate is never a futures signal-availability/freshness gate: missing, stale
or future aggregate values alone do not invalidate a captured futures price. AST uses the
recorded `captured_at` observation time, with request/response ordering checked against it,
valid futures prices, exact instrument/expiry identity, existing coverage/gap checks and
completed five-minute bars. It never substitutes receipt time for an exchange/source time.

Decision, journal, trade-feature and status evidence explicitly expose:

- `futures_source_timestamp = null`
- `futures_source_timestamp_status = UNAVAILABLE_IN_SHARED_CAPTURE_SCHEMA`
- `futures_observation_time = captured_at`
- `futures_observation_time_basis = BROKER_RESPONSE_CAPTURE_TIME`
- `snapshot_request_started_at` and `snapshot_response_received_at` from the recorded snapshot.

Executable option fills/marks independently require **each exact option quote's own**
source timestamp and the unchanged executable-book validation. Missing/stale individual
option timestamps still reject a paper fill, even when futures signal evidence is valid.
Authoritative futures exchange/source-time freshness remains unavailable; capture ordering
and coverage are observation-time evidence, not a claim of exchange-quote freshness.
The shared capture adapter/field semantics and all ACTS paths remain unchanged.

The candidate builder's legacy `holding_horizon_minutes` is construction metadata only;
AST never consumes it as an exit rule. The explicit AST safety policy alone determines
whether a time stop exists. Subperiod consistency includes chronological sessions with
zero trades rather than deleting inactive days from the ranking evidence.

## Historical CSV session boundaries

ACTS_PAPER historical captures can end around 15:20 IST. The CSV loader preserves all
supplied five-minute OHLC rows; it does not fill the missing 15:20–15:30 interval or treat
15:20 as the research strategy's session-end exit.

For an incomplete source session, it appends an explicit `AST_INCOMPLETE_SESSION_GAP`
marker from the final supplied bar's end to the scheduled 15:30 boundary. This is **not a
market bar**: it is immutable, `valid=false`, with every OHLC/volume value `null`.
Its timestamps describe missing coverage, not a quote or a simulated fill. The final CSV
session receives the same marker without needing any following day's prices. Causal
prefixes select by `end <= observation_time`, so the marker never changes evidence at
the last actual capture. Any optimizer cutoff that includes this marker includes boundary
metadata; it does not assert that a 15:30 market price was captured.

Existing research segmentation removes the marker before indicator and trade calculations,
resets continuity, and starts again on the next genuine session's valid 09:15–09:20 bar.
That session's three real opening bars remain available for the 09:15–09:30 ORB. A new
contract after this explicit reset cannot leak the prior contract's indicator state.
Complete 15:30 sessions retain existing overnight handling; missing intraday bars or a
missing 09:15 opening bar are not repaired.

Signal-research positions lacking an observed, valid session-end exit remain unpriced
and excluded, as before. Neither the gap marker nor the next session supplies an exit
price for the incomplete session. No strategy, optimizer scoring, ADX/ER policy, paper
worker or ACTS behavior changes are made by this CSV input correction.

## ADX/ER regime walk-forward research harness

`app.adaptive_supertrend.regime_research` is a standalone **futures signal research** CLI.
It uses the same `load_bars`, `_segments`, `signal_trades`, `metrics`, ATR, SuperTrend,
ADX and ER implementations. It does not require `ExecutionPolicy`, broker credentials,
a database, or an enabled worker. It cannot configure or deploy any runtime policy.

The default **research candidates only** are FAST: 5/1.1, 5/1.2, 6/1.1, 6/1.2,
7/1.1, 7/1.2; SLOW: 20/1.2, 22/1.2, 24/1.2, 25/1.2, 26/1.1, 26/1.2, 26/1.3.
`--ast-configs` accepts an optional comma list such as `FAST:5/1.1,SLOW:26/1.2`.
Unlabelled configurations use CUSTOM. Multipliers remain exact integer tenths.
All period ranges, quantiles, block sizes and minimum trades must be supplied explicitly;
there are no approved threshold defaults. The example below is an illustrative research
experiment specification, not a recommendation for production strategy settings:

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.regime_research \
  --csv /research/futures-5m.csv \
  --json /research/ast-regime-walk-forward.json \
  --csv-output /research/ast-regime-walk-forward.csv \
  --adx-period-min 10 --adx-period-max 14 \
  --er-lookback-min 5 --er-lookback-max 10 \
  --threshold-quantiles 0.25,0.5,0.75 \
  --minimum-trades-per-block 3 \
  --train-sessions 15 --validation-sessions 5 --oos-sessions 3
```

For each chronological fold, the training, validation and OOS blocks use consecutive
observed session dates. The window advances by `oos_sessions`, so OOS dates never
repeat. Later training windows may legitimately include earlier folds' OOS observations
once those dates are in the past. No fold uses its own validation/OOS values to create
thresholds. ADX and ER distributions include only non-missing **training** indicator
observations. Each period's sorted training distribution is interpolated using linear
R7 quantiles (`h=(n-1)q`). Quantiles are crossed independently for ADX and ER. Every
concrete threshold and its training sample count is recorded per fold. Missing training
distributions produce null thresholds/unavailable results, not a neutral or zero filter.

Indicator context starts at each fold's training start and advances causally through
validation/OOS. Existing session gaps and contract-roll boundaries reset that context;
non-price markers never enter indicator calculations. Validation can choose among the
threshold candidates already created from training; it cannot recompute their values.
Changing OOS prices cannot change that fold's or earlier folds' thresholds or
training/validation selection. A previously OOS date can affect a later fold after it
becomes legitimate historical training data.

Two rankings are deliberately separate:

1. **Training/validation selection**, used for the honest selected-policy OOS evaluation,
   sees training/validation statistics only. It prefers more minimum-evidence AST
   configurations positive in both blocks, then better worst validation points, better
   median validation points, lower summed validation max drawdowns, more combined
   training/validation trades, and the deterministic parameter tie break.
2. **Post-OOS descriptive research ranking**, per fold and across folds, first places
   candidates with at least one minimum-evidence AST configuration ahead of candidates
   without one. It then sorts lexicographically by: more qualified AST configurations
   positive in BOTH validation and OOS; higher worst OOS points; higher median OOS points;
   lower sum of per-configuration OOS max drawdowns; more combined validation/OOS trades;
   then ADX period, ADX quantile, ER lookback, ER quantile ascending. Total P&L is not
   a ranking key. This retrospective ranking uses OOS and must **not** be represented
   as the policy selected before that OOS block.

Minimum trades is evaluated per AST configuration and per block. Ranking positivity
counts require the configured minimum in its two relevant blocks (training/validation
for selection; validation/OOS for retrospective analysis). Raw positive counts and all
individual evidence flags remain visible. No single outcome approves a runtime policy.
A fold without sufficient selection evidence records no selected policy; the selected
OOS series reports its actual covered dates rather than filling unselected dates with
zero trades/P&L.

Each candidate reports the existing full metrics separately for every AST configuration
and training/validation/OOS block, alongside the identical **UNFILTERED** AST baseline.
Baseline quality masks impose no ADX/ER gate or ADX/ER warm-up; AST warm-up, ORB, causal
signal timing, existing signal exits and unpriced-position exclusions remain unchanged.
Family robustness is shown for ALL configurations and separately for FAST/SLOW/CUSTOM.
Global OOS metrics concatenate only disjoint OOS trades chronologically. Aggregate
validation figures are explicitly labelled sums of fold evaluations: validation windows
may overlap, so those totals are research evidence, not a once-traded portfolio curve.
Summed drawdowns/counts across alternative AST configurations also are not a portfolio.

`regime_filter_value` always means **filtered OOS minus unfiltered OOS** for points,
max drawdown and trade count. Negative drawdown delta is improvement. The report also
shows changes in worst/median OOS and qualified positive configurations, and flags trade
reduction without points/drawdown or family-robustness gains. All the component deltas
remain available to inspect trade-offs. No option fills, option P&L, costs or net cash
returns are inferred from futures bars.

Input bars are grouped by session once, so rolling folds do not repeatedly scan the
entire dataset. Precomputation reuses ATR by period, AST directions by configuration,
ADX by period and bounded ER windows. Training distributions are sorted once per indicator period, and a
4096-entry per-fold LRU reuses identical quality-mask signal evaluations. JSON is written
incrementally and optional CSV emits per-fold/per-policy/per-AST rows. A 100,000
fold-policy-AST evaluation guard rejects excessive experiments explicitly; it never
silently truncates the grid and is not a trading threshold. Input CSVs cannot be used
as output destinations. Repeated runs with the same inputs/arguments are deterministic;
no wall-clock or random values enter the report.

In the global retrospective table, positivity/worst/median validation figures use the
per-configuration sums of validation fold evaluations; OOS figures use each configuration's
chronologically accumulated, nonoverlapping OOS series. Global evidence flags require the
minimum in every relevant fold block, and `thresholds_by_fold` preserves the actual
training-derived values instead of presenting a single fictitious global threshold.
The CLI records the input SHA-256 and rejects an input changed during calculation before
writing reports. None of the example research arguments becomes a runtime default.

## Explicit unfiltered regime

AST now distinguishes an explicit **`{"mode":"NONE"}`** from a missing/null regime.
NONE is fully configured and produces an all-True quality mask. It imposes no ADX/ER
threshold or ADX/ER warm-up requirement; diagnostic ADX/ER decision values remain null.
It does **not** bypass AST warm-up, causal completed bars, session/contract boundaries,
ORB, daily parameter selection, execution/risk checks or exact executable option books.
A null/absent regime still blocks entry as `AST_POLICY_UNCONFIGURED`. NONE accepts no
ADX/ER parameters; it never guesses them.

`{"mode":"ADX_ER", ...}` retains the existing indicator calculations and gates.
Legacy ADX/ER policy JSON without `mode` is still accepted as ADX_ER with the same
mathematics and decisions. Normalized policy JSON now includes the mode, so its hash
explicitly identifies the choice. Stored historical policies, hashes, optimizer results
and journals are not rewritten. Existing active-policy mismatch guards are unchanged;
this is not an instruction to update a running worker with active exposure.

No optimizer/execution choices or production activation values are supplied by this
change. Static 7/1.1 is a research benchmark, never a runtime fallback or fixed policy.

## Daily optimizer-policy research (regime NONE)

`app.adaptive_supertrend.optimizer_policy_research` is a standalone, CSV-only signal
experiment. It imports no paper service, broker, worker or database connection. It uses
`load_bars()` including its explicit non-price incomplete-session boundaries, the exact
1,886 grid, existing `optimize()`/`rank()`, completed-bar SuperTrend and `signal_trades()`.
Only an explicit `signal_research_only=True` call exempts missing **execution** policy
from `optimize()`; normal runtime calls still require the full policy. Missing optimizer
or regime is never exempted. No fake option execution policy or costs are constructed.

Every candidate is an explicit tuple of lookback, chronological subperiod count, minimum
closed training trades, named seven-field weight profile, validation sessions and OOS
sessions. All six candidate axes are required. Weight profiles are discrete caller-owned
JSON objects, not a continuous search and not runtime defaults. For example, a research
weights file can contain this **illustrative, unapproved experiment input**:

```json
{
  "balanced_experiment": {
    "profitability": 1, "drawdown": 1, "profit_factor": 1,
    "sharpe": 1, "frequency": 1, "consistency": 1, "neighbours": 1
  },
  "drawdown_experiment": {
    "profitability": 1, "drawdown": 2, "profit_factor": 1,
    "sharpe": 1, "frequency": 1, "consistency": 1, "neighbours": 1
  }
}
```

Example command, also **not** an approved runtime configuration:

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.optimizer_policy_research \
  --csv /research/futures-5m.csv \
  --weight-profiles /research/optimizer-weights.json \
  --lookback-sessions 5,10,15 --subperiods 2,3 --minimum-trades 3,5 \
  --validation-sessions 3 --oos-sessions 1,3 \
  --json /research/optimizer-policies.json \
  --csv-output /research/optimizer-policies-daily.csv
```

For **each validation and OOS day**, parameters are selected using precisely the preceding
candidate lookback sessions. The simulated selection clock is 09:00 IST before that day;
it is explicitly labelled historical simulation, not a fabricated broker/source timestamp.
No current-day bar can enter selection. Parameters are frozen for that entire day, even
inside a multi-day OOS block. Indicator context begins at that same rolling training
prefix and advances causally into the evaluated day. A missing grid selection remains
unavailable; static benchmarks cannot fill the hole. Later OOS sessions may legitimately
enter later historical training once those dates are in the past.

Each validation/OOS layout starts after the largest supplied lookback and its validation
window, so different lookbacks have comparable validation/OOS dates within that layout.
Windows advance by the OOS block size and use only complete blocks; unused tail dates
are reported. Validation windows may overlap, but OOS dates within a layout never repeat.
Different layouts can cover different dates; their comparisons are descriptive only.
The tool does not select a layout by OOS performance or install a winning runtime policy.

Before an OOS block, a separate, fixed **research-only policy selection rule** ranks the
candidate optimizers by validation results only. Eligibility requires a valid daily AST
selection on every validation session and at least one closed validation signal trade.
The order is higher validation daily consistency, higher expectancy, lower maximum
closed-trade drawdown, more closed trades, then candidate ID. Training affects each
validation day's full-grid ranking; OOS prices/metrics never enter this policy selector.
The chosen optimizer policy is held for the block, but its ATR/multiplier are still
reselected before every day. If no candidate qualifies, that selected-policy OOS path is
explicitly unavailable. Every individual candidate is also evaluated independently, so
this meta rule does not hide the alternatives. It is not a change to runtime grid ranking.

JSON contains per-candidate daily selected ATR/tenths, causal training dates/hash/cutoff,
policy/hash, closed OOS signal trades and full metrics: points, expectancy, profit factor
(with its undefined status), drawdown, Sharpe-equivalent, win rate, and positive-session
consistency. Coverage counts distinguish a valid selected configuration from an actual
closed trade. Partial coverage metrics describe the observed closed trades, not an
assumed flat return for missing selections. Turnover/stability use only adjacent planned
OOS days with valid selections on both; missing days are not bridged. FAST occupancy is
ATR 5–7 / multiplier 1.0–1.3; SLOW is ATR 20–26 / multiplier 1.1–1.3, with selected-day
denominators. Static 7/1.1, 7/1.2 and 26/1.2 are separately labelled benchmarks, evaluated
with the same causal lookback prefix and OOS dates. Both whole-OOS and matched-selected-day
benchmark metrics are shown; the point delta uses matched dates. No option P&L or net
cash returns are inferred, and unpriced incomplete-session positions remain excluded by
the existing signal engine. CSV provides deterministic per-candidate/day selections and
metrics; JSON also contains validation rankings, folds and selected-policy OOS evidence.

Grid metrics are computed once per (day, lookback, subperiods); minimum-trade and weight
variants rerank fresh rows with the unchanged rank function. Grid rows are discarded after
that day's group, and only daily selection/trade evidence is retained. Benchmarks share
causal signal calculations where possible. Limits of 256 explicit candidates and 512
full-grid fits bound an invocation; oversized experiments fail before fitting rather than
truncating candidates or changing the grid. Reduce/split explicit axes if a guard fires.
Candidate/grid/fold progress goes to **STDERR**, never deterministic JSON/CSV. Both source
CSVs and weight files have SHA-256 recorded and are checked unchanged before output;
outputs cannot alias either input or each other. No wall-clock duration/randomness enters
research output. This change neither changes nor launches a worker, ACTS or deployment.

## Execution-policy research on actual option books

`app.adaptive_supertrend.execution_policy_research` is an **offline research command**.
It does not create a broker, collector, worker, database connection or production journal.
It reads completed futures CSV bars and actual `ScalperMarketSnapshot` JSONL records.
A `.jsonl.gz` is streamed directly. No manual decompression, quote regeneration, LTP
fallback, interpolated prices or invented forced-close observation is permitted.

Supply a frozen **signal-only** `AstPolicy` JSON with no execution section. For the
completed signal research, the explicit input is:

```json
{
  "optimizer": {
    "lookback_sessions": 15,
    "subperiods": 3,
    "minimum_trades": 3,
    "weights": {
      "profitability": 0.75, "drawdown": 1.0, "profit_factor": 2.0,
      "sharpe": 2.0, "frequency": 0.0, "consistency": 1.25, "neighbours": 1.25
    },
    "validation_sessions": 3,
    "out_of_sample_sessions": 1
  },
  "regime": {"mode": "NONE"}
}
```

This is the specified frozen **research** signal input, not an installed runtime default.
No optimizer, signal, AST service, worker, ACTS or shared construction/book code changes
are needed. `SignalOnlyPolicy` is a local pure-decision view that excludes missing
execution from the configuration check; it serializes/hashes identically to that input.
A normal runtime `AstPolicy` still blocks without execution policy. AST warm-up, completed
bars, futures identity, causal timestamps, contract/session boundaries and ORB are intact.
Current signal price is `nifty_future`. The shared option-derived timestamp is never
represented as a futures exchange timestamp. Spread short-distance calculation remains
exactly the existing builder's spot-based calculation; it is not replaced with a new rule.

### Curated pass-one candidate file

[`examples/ast-execution-research-candidates.json`](examples/ast-execution-research-candidates.json)
contains exactly LOW_RISK, BALANCED, CREDIT_QUALITY, DISTANCE_200, WIDTH_100_ONLY and
WIDTH_150_ONLY from the construction experiment. It is not loaded automatically and
must never be treated as production configuration. Every candidate explicitly specifies
widths, short distance, credit/width, leg spread percentage, quote age, minimum credit,
liquidity profile and maximum defined risk. P10_LIKE means raw OI 180,000 / volume 100,000;
PERMISSIVE means zero thresholds, **not** permission to invent missing OI/volume.
Existing book validators still reject missing liquidity values and insufficient depth.

All seven requested width sets are supported: [50], [100], [150], [200], [50,100],
[100,150], [100,150,200]. Pass-one distance choices are 100/150/200; credit/width choices
0.10/0.15/0.20; leg bid/ask percentage limits 0.35/0.70/2.0; maximum quote age 4 seconds;
minimum credit 5; and maximum defined-risk choices INR7500/10000/12500. Widths of 250+
are rejected in this first-pass harness. Named liquidity profiles are explicit; there is
no automatic OI/volume grid or Cartesian expansion.

The file prominently labels **RESEARCH CONTROL VALUES — NOT OPTIMIZED**. Its identical
controls are entry end 15:19, forced exit 15:20, daily gross-loss ceiling INR1,250,000,
100 trades/day, 100 consecutive losses, zero cooldowns, 30-second pending TTL, kill switch
false and an empty configured event calendar. These deliberately permissive, bounded
values isolate construction differences; they are **not approved trading risk settings**.
One lot and the existing one-position maximum remain fixed. There are fewer completed
five-minute signal bars per session than these trade/streak caps. Actual control blockers,
including entry window/TTL/daily loss, are nevertheless reported separately. Supply other
explicit bounded controls if required, held identical across candidates; do not confuse
them with construction-policy optimization. No field may silently use a runtime default.

Only `HARD_SAFETY_ONLY` is accepted. Every legacy target/credit-stop/trail/time-stop field
must remain null; the existing safety schema rejects any non-null legacy parameter.
Exact-book hard loss, daily loss, configured event/kill safeguards, forced exit and expiry
behavior are delegated to the unchanged service. The shared builder's legacy
`holding_horizon_minutes` diagnostic may still appear in candidate evidence; HARD_SAFETY_ONLY
never consults it as an exit. No profit capture, trailing or time-stop is introduced.

### Invocation and computation boundaries

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.execution_policy_research \
  --futures-csv /research/futures-5m.csv \
  --snapshots /research/scalper-market-snapshots.jsonl.gz \
  --signal-policy /research/ast-frozen-signal-policy.json \
  --candidates docs/examples/ast-execution-research-candidates.json \
  --validation-sessions 3 --oos-sessions 1 \
  --json /research/ast-execution-policies.json
```

Validation/OOS counts are mandatory CLI inputs; they do not retune the frozen signal
optimizer. Eligible evaluation dates require the supplied lookback's prior CSV sessions.
With the stated 29-session dataset and a 15-session lookback, 14 dates can receive daily
selection, and a 3/1 construction evaluation provides 11 nonoverlapping OOS sessions.
A day with no eligible optimizer configuration stays unavailable; no 7/1.1 fallback is
allowed. Incomplete source sessions retain their non-price boundaries; unpriced futures
research positions and option exposures receive no fabricated session-end price.

There are two bounded streaming passes over raw snapshots: metadata/order/content-hash
inventory, then execution for **all candidates together**, retaining one raw book at a
time. Each daily 1,886-grid optimization runs once per supplied signal policy/day. All
candidates share its selected ATR/multiplier, metrics and training-data identity. The
harness then rebinds only the full combined policy metadata to obtain the exact
hypothetical runtime optimizer-run hash, frozen parameter document and opportunity ID.
It never reranks or repeats the grid per execution candidate. Outputs separately identify
signal policy, named construction candidate and full hypothetical combined policy.

`decide()` runs once per observation against the causal rolling lookback prefix and only
CSV bars whose end is already captured. Futures contract mismatch invalidates that signal
boundary. Independent in-memory candidate projections share those genuine futures
opportunities while retaining their own consumed-opportunity sets, trades and risk state.
They delegate `_risk()` and `_transition()` unchanged to `AstPaperService`; candidate
construction and exact-book validation also use the existing functions. Entries remain
short BID minus hedge ASK on a later observation. Marks/exits remain exact held short ASK
minus hedge BID. Freshness, source timestamps, depth, identity, tick and defined-risk
checks remain in force. No database-backed service initializer or broker adapter is run.

The research execution seed is a plain namespace containing only declared, non-required
`scalper_*` defaults from `Settings.model_fields`. It never constructs `Settings` or any
`BaseSettings`, reads dotenv files or environment variables, or stores a broker key.
`ExecutionPolicy.scalper()` then applies the explicit execution JSON exactly as before.
Unrelated broker/scalper environment variables cannot alter research results. Runtime
settings construction and execution-policy conversion remain unchanged.

Limits are 32 named candidates, 8 named liquidity profiles, 100,000 raw observations,
1,500,000 candidate-observations and 128 full-grid fits per invocation. Exceeding a guard
fails explicitly before replay/optimization rather than truncating history, candidates or
the grid. Grid/day/fold/candidate and periodic observation progress goes to STDERR;
JSON contains no wall-clock duration or random values.

### Chronology, selection and interpretation

Each candidate is a **continuous independent shadow policy** throughout validation and
OOS. Unresolved exposures are never reset to make the next fold trade. Validation windows
may overlap; OOS blocks never overlap and only complete blocks count. Before reading the
first OOS observation, selection snapshots the preceding contiguous validation sessions.
A candidate needs at least one closed validation entry-cohort trade and zero active
pending/open/unresolved exposure. Eligible candidates rank by higher positive-session
consistency, higher expectancy, lower closed-trade drawdown, more closed trades and a
stable candidate-ID tie break. This fixed research-only selection rule never uses OOS
prices/results. A later OOS session may become later validation/training after it is past.

Per-fold selected OOS is the chosen candidate's independent shadow result, **not** an
executable portfolio that switches between historical shadow books. No hypothetical
switch rewrites positions or circumvents runtime active-policy guards. Missing validation
evidence selects nobody. Final OOS comparisons are descriptive; they do not approve a
production execution policy. Earlier validation results/selections remain frozen after
later books arrive. Statistics accept completed sessions only; future exits cannot enter
an earlier cutoff. Costs can optionally be supplied in the signal JSON's existing `costs`
field. Net P&L is null unless the supported schedule is explicitly complete, dated and
valid for every included closed trade. No costs or margin values are inferred.

Each candidate reports all-session and OOS opportunities, first-observation/ever static
execution availability, opportunities removed by construction filters, actual pending/fill
results, reason counts, controls, exits, width usage and filled-trade distributions
(short distance, credit, ratio, defined risk, leg spreads, quote age, raw OI and volume).
Static availability ignores portfolio/common-control blockers but keeps the candidate's
construction, per-trade risk and executable-book requirements. Once an opportunity is
shown statically available, repeated static checks may stop; this is an **opportunity**
measure, not a quote-by-quote availability rate. Rejection counts are per attempted
observation or examined leg/pair, not unique lost trades. Compare them with unique AST
opportunities and opened trades, rather than treating every reason count as a trade.

Closed gross option P&L and statistics are INR from actual exit books, ordered by exit.
Cashflow statistics include all closes in the evaluated block; separate entry-cohort
statistics include only entries from that block closed by cutoff. Carried positions,
open/pending/unresolved counts and unclosed entry cohorts remain explicit. Missing
session-end evidence leaves positions open or unresolved according to the unchanged
service; it never assigns settlement, a later LTP or an assumed zero return. Final trade
state and exact entry/exit/last mark evidence accompany each trade. A compact research
trace records non-mark transitions and a digest/count over all transitions, including
marks; it is reproducible from source files and is explicitly **not a runtime journal**.

Plain and gzip files with identical uncompressed contents produce identical research
results. The CLI additionally records physical input SHA-256s/path provenance (which
naturally differs for compressed versus plain files), checks every source unchanged,
and refuses input/output aliases. The uncompressed content hash also must match between
inventory and replay. No input, historical journal or production row is modified.
