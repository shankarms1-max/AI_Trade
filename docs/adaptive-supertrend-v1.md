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

- Current futures above the range + completed AST BULL + configured ADX/ER pass → BULL,
  construct a PE credit spread.
- Current futures below the range + completed AST BEAR + configured ADX/ER pass → BEAR,
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
- ADX period/threshold, ER lookback/threshold;
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
