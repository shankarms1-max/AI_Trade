# Adaptive SuperTrend futures paper strategy

`AST_FUTURES_V1` is the sole active AST paper strategy. It takes one long or short NIFTY futures lot. The independent worker accepts only `--mode PAPER` and is disabled by default (`ADAPTIVE_SUPERTREND_ENABLED=false`).

## Signal and daily freeze

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

A breakout above the frozen range with completed SuperTrend BULL can open a long. A breakout below with completed SuperTrend BEAR can open a short. Regime configuration, warm-up, opportunity identity and consumption on fill use the existing decision logic.

## Paper execution

Kotak's NIFTY reference response supplies the futures symbol, expiry and LTP. Each native capture persists the original request/response times and the scalar futures observation in `scalper_market_snapshots`, with `mode=AST_PAPER` and `producer=AST_FUTURES_V1`. No call/put quote rows are required. The shared schema also requires spot, ATM, reference option expiry and header lot metadata. The futures worker uses one independently verified configured lot size and requires the captured header lot to agree. Each pending entry records the actual futures symbol and expiry. The later fill rechecks both and records the confirmed identity and observed lot. A new session can select a new contract under the same policy. A held position retains its original identity and quantity.

A decision creates a pending entry. The next chronological native observation must still support the same opportunity and have the same futures identity and expiry, a finite positive price, valid capture timing, and fall within the entry window and configured sequence gap. Failure rejects the pending entry. Simulated entry, mark and exit prices are labelled **observed futures LTP**. No executable exchange bid/ask, tick-complete path, or futures exchange timestamp is available. `futures_observation_time` is response capture time and `futures_source_timestamp` stays null.

Every valid held-contract observation records a mark. Long points are mark minus entry, short points are entry minus mark. Gross INR equals points times the frozen one-lot size. MAE is the lowest observed signed points including zero, MFE the highest. Net INR stays null unless an explicit round-trip cost for one lot is supplied in the frozen policy.

A reversal requires an actual SuperTrend direction change on a completed 5-minute bar after entry. A new post-gap seed alone cannot trigger it. The exit becomes pending and uses a subsequent exact-contract observation. The configured session-end exit uses the first valid observed price at or after its predetermined cutoff in the session. The final scheduled capture bucket starts at 15:30 IST. Lost contract identity, missing exit pricing at cutoff, or reaching a later date with the position still open records unpriced `UNRESOLVED_EXPOSURE`. Restart does not substitute a later day's price. Pending, open, exiting and unresolved positions block a second position. No stop, target or trailing rule is configured.

## Configuration and recovery

`AST_CONFIG_JSON` accepts the signal optimizer and regime settings, plus the following futures-only `execution` fields. Use the approved 15-session optimizer configuration and verified contract metadata when preparing a paper run.

| Field | Meaning |
| --- | --- |
| `lot_size` | Required positive integer, independently verified for NIFTY futures. A missing or conflicting observed lot blocks execution. |
| `entry_end_time` | Last permitted entry time in IST |
| `session_exit_time` | Scheduled session-end exit in IST, after the entry cutoff and no later than 15:30 |
| `round_trip_cost_per_lot_inr` | Optional flat one-lot round-trip cost, effective for trades under this policy |

Missing configuration blocks entries. Historical spread policies fail validation. The worker verifies the cursor, chain head and active projection on restart. Cursor updates, trade projections and hash-linked events commit in one transaction. A changed policy cannot manage an active trade. Catch-up processes saved observations in capture order and ignores duplicates.

Migration `0023_ast_futures_paper` seeds a new cursor and creates `ast_futures_paper_cursor`, `ast_futures_paper_trades` and `ast_futures_paper_events`. The events are append-only. **LEGACY/RETIRED** tables `ast_paper_cursor`, `ast_paper_trades` and `ast_paper_events` retain their original evidence. No futures projection is seeded from those tables. Shared immutable optimizer/session records remain available. A date already frozen under another policy cannot be overwritten or reused for futures entries.

Read status and verify the futures journal using the isolated AST database:

```sh
python worker/adaptive_supertrend_worker.py --mode PAPER --status
python worker/adaptive_supertrend_worker.py --mode PAPER --verify-journal
```

## Historical 15-second futures paper replay

Run the file-only CLI with the archived ACTS_PAPER scalar observations, the research five-minute CSV and one frozen futures policy. Copy the approved optimizer and regime settings unchanged into the futures policy JSON. The execution section requires the verified lot size and explicit entry and session-exit times. Retire the old contract list before a future restart with zero active exposure. Existing journal documents retain their historical policy hashes.

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.futures_replay \
  --futures-csv /research/futures-5m.csv \
  --snapshots /research/acts-paper-snapshots.jsonl.gz \
  --policy /research/ast-futures-policy.json \
  --json /research/ast-futures-replay.json
```

To test one intended IST cutoff, add `--session-exit-time HH:MM:SS`. Otherwise the policy supplies it. The CLI records the effective policy and the SHA-256 of each input. Daily progress goes to stderr. No worker, broker credential, SQL connection or native capture row is needed.

The latest 15 prior sessions supply each pre-session grid fit. Prior replayed sessions use completed bars built from raw observations and the runtime gap policy. The CSV supplies historical seed sessions lacking raw observations. The current date's CSV never supplies an intraday signal or fill. New parameters are selected only from past sessions, once per day; historical 09:00 IST is the simulated selection clock. The policy remains fixed regardless of P&L. A missing freeze, incomplete ORB or non-executable observation retains its blocker. Replay line numbers are evidence references local to the archive.

`summary` counts distinct eligible signal opportunities before execution checks, pending entries created, fills, rejections and realized results. It reports open/pending positions at the end of the file separately from observed unresolved states. The last observation never forces a synthetic close. Drawdown uses closed trades in exit order, starting at zero. Win rate includes flat closures in the denominator. Expectancy and average wins/losses are points; PF is null without a gross loss.

`comparison` gives deltas against the supplied walk-forward aggregate (14 trades, +395.6 points, PF 2.56, drawdown 119.4 points). It separately runs the existing five-minute signal accounting on the replay dates with the already-frozen parameters. Trades are matched by session, side, exact contract and SuperTrend transition. The report separates entry and exit price differences for matched trades and lists the points from unmatched closures. The supplied aggregate alone cannot identify which individual trades differed. Daily evidence includes raw-versus-CSV bar agreement, observed contract identities, gaps, lot blockers, breakouts visible only on the current observation, and availability at the configured exit cutoff. A missing session-end price leaves exposure unpriced and can block subsequent sessions.

## One-time historical bootstrap

Use the original ACTS_PAPER raw snapshot JSONL or JSONL.GZ and its audited SHA-256. The four-column futures CSV lacks original request/response timing and required raw header scalars. Set `DATABASE_URL` to the isolated, migrated AST database. Choose a cutoff strictly later than the last archived trading date and no later than the session being prepared.

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.bootstrap \
  --snapshots "$ARCHIVE" --source-file-sha256 "$AUDITED_SHA256" \
  --source-mode ACTS_PAPER --before-date 2026-11-23 --confirm-isolated-ast-database
```

The transaction streams bounded batches, preserves original clocks, exact futures identity and physical source SHA, and inserts zero option quotes. The JSON receipt gives dates, first/last timestamps and inserted/already-present counts. For the described complete archive on an empty target, expect 42,350 rows across 29 dates. A repeat should insert zero. Incompatible provenance or a changed source for the same bootstrap date fails the import.

Prior dates prefer native AST_PAPER for the whole date, even with partial coverage. Otherwise historical AST_BOOTSTRAP may supply bars. Current-session history is native only. Earlier native captures without the new producer marker also remain history-only. Bootstrap and pre-cutover native rows cannot generate journal events. The freeze records input counts, dates and bootstrap source hashes. The latest 15 sessions naturally displace imported history as native sessions accumulate.

## Signal research and walk-forward

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

```sh
PYTHONPATH=backend python -m app.adaptive_supertrend.research \
  --csv /research/futures-5m.csv --policy /research/ast-policy.json \
  --date 2026-11-20 --grid --walk-forward --top 20 --json /research/results.json
```

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
remain available to inspect trade-offs. No paper fills, costs or net cash
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
ORB, daily parameter selection or futures execution checks.
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
or regime is never exempted. Signal research requires only the signal policy.

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
benchmark metrics are shown; the point delta uses matched dates. No cash P&L or net
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
