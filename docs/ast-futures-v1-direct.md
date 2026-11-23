# AST_FUTURES_V1_DIRECT PAPER worker

The frozen AST_FUTURES_V1_DIRECT strategy trades one NIFTY futures lot in PAPER mode only. It has no options, no optimizer, no regime filter and no order capability. It runs isolated from ACTS, with its own database, journal and container.

## Frozen rules

| Rule | Value |
|---|---|
| Bars | Completed 5-minute NIFTY futures bars, session anchored at 09:15 IST |
| Opening range | The three bars from 09:15:00 (inclusive) to 09:30:00 (exclusive). It is frozen once the 09:25 bar completes. If any of the three bars is missing or invalid, there is no range and no trading that day |
| AST | Existing Wilder ATR / final-band SuperTrend, ATR 22, multiplier 3.9 |
| Regime / optimizer | NONE / DISABLED |
| BULL entry | A completed bar closes above the range high and the completed AST direction is BULL. Go LONG |
| BEAR entry | A completed bar closes below the range low and the completed AST direction is BEAR. Go SHORT |
| Fill | The first executable observation at or after the signal bar's end. The signal bar is never its own fill. The fill window is 60 s; after that the entry is rejected |
| Exit | A completed AST direction flip away from the position, filled at the next executable observation. Otherwise a forced exit at 15:20:00 IST |
| Size | One lot. Lot size comes from configuration and must equal the lot reported by the broker |
| Positions | At most one, including pending and unresolved ones |

The policy model fixes every strategy parameter as a literal. A configuration that tries to change 22, 3.9, the regime, the optimizer, 15:20, the lot count, the position limit or the fill basis fails validation. Only operational fields are configurable: `lot_size`, gap tolerance, fill window, forced-exit grace, history depth and optional research friction.

An opportunity's identity is: date, direction, frozen range, ATR 22, multiplier 3.9, AST transition and futures contract. It is consumed when filled and is never reused. A new opportunity needs a new AST transition. The bar that triggers an exit cannot also open the reversal; the next completed bar can. This matches the research `signal_trades` sequencing exactly.

Indicator history is causal. The worker uses prior sessions of the same contract, plus today's native captures up to the current observation. Earlier sessions come from, in order of preference, the worker's own captures, the shared AST capture stream, or the validated AST bootstrap. Streams are never mixed within one date. A gap, invalid bar or contract roll restarts the 22-bar warm-up. Missing history blocks signals.

## Paper execution basis

The capture schema has no trustworthy NIFTY futures bid/ask or depth. Fills therefore use `FUTURES_LTP_PAPER_V1`: the observed futures LTP, recorded explicitly as an LTP proxy. No bid/ask or quantity is fabricated. Each trade document keeps these values separate: signal price (bar close), fill prices, mark (in observation evidence), gross points, gross INR, the configured research friction, and net research points. Net research points are null when no friction is configured.

A fill requires valid capture timing, a positive price, the exact contract identity, an unexpired expiry and the configured lot size. If the held contract stops being observable, the trade becomes `UNRESOLVED_EXPOSURE`. The same happens if the forced exit cannot execute within the 120 s grace, or if a session ends with the position open. No exit is invented. An unresolved exposure blocks new entries until an operator acknowledges it. Acknowledgement records no P&L.

## Lifecycle and evidence

Trade states are `PENDING_PAPER_ENTRY`, `OPEN`, `PENDING_PAPER_EXIT`, `CLOSED`, `ENTRY_REJECTED`, `UNRESOLVED_EXPOSURE` and `UNRESOLVED_ACKNOWLEDGED`. Journal events are `PENDING_PAPER_ENTRY`, `ENTRY`, `ENTRY_REJECTED`, `PENDING_PAPER_EXIT`, `AST_FLIP_EXIT`, `FORCED_EXIT`, `SAFETY_EXIT`, `UNRESOLVED_EXPOSURE`, `UNRESOLVED_ACKNOWLEDGED` and `OBSERVATION`. Observation status is `NO_SETUP`, `WATCH`, `SIGNAL_BLOCKED` or the position state.

Migration `0024_ast_direct_paper` adds `ast_direct_paper_cursor`, `ast_direct_paper_trades` and `ast_direct_paper_events`. Events are hash-chained and immutable, protected by SQLite and PostgreSQL triggers. Earlier AST option-era and AST_FUTURES_V1 tables are untouched. The downgrade refuses a non-empty journal.

Each observation is one transaction under a compare-and-swap cursor lock. Observations are processed strictly in capture order. On restart the worker rebuilds state from the persisted cursor, active trade and verified journal head. A repeated observation returns `DUPLICATE`, so entries are never duplicated.

## Deployment (isolated research host only)

```sh
cp .env.ast-direct.example .env.ast-direct     # set lot_size, DB password, KOTAK_CONSUMER_KEY
mkdir -p ast-direct-control
docker compose -f docker-compose.ast-direct.yml --env-file .env.ast-direct up -d --build
```

The stack runs `ast-direct-paper-postgres`, `ast-direct-paper-migrate` and `ast-direct-paper-worker`. The worker uses the existing read-only Kotak market-data reader, with the consumer key only and no trading login. It captures every 15 s from 09:15 to 15:30 IST on weekdays that are not configured holidays, and idles otherwise.

This strategy supersedes the optimizer-based AST_FUTURES_V1 worker. Keep `ADAPTIVE_SUPERTREND_ENABLED=false`; the direct worker does not depend on it.

Seed warm-up history before the first session with the existing bootstrap, run against the isolated database (see `docs/adaptive-supertrend-v1.md`). Without it, the first session can trade only after 22 completed bars.

## Operations

```sh
W="docker compose -f docker-compose.ast-direct.yml --env-file .env.ast-direct exec ast-direct-worker python /app/worker/ast_direct_worker.py --mode PAPER"
$W --status                      # read-only status JSON
$W --status --verify-journal     # full hash-chain audit
touch ast-direct-control/KILL    # kill switch: no entries, SAFETY_EXIT any open paper position
rm ast-direct-control/KILL       # clear it
$W --acknowledge-unresolved TRADE_ID --note "reason"
```

Status shows: worker running/stopped (from the heartbeat), strategy version, trading date, futures contract, last observation, last completed bar, opening range, AST direction and value, current blocker or signal, position and mark, today's trades, today's and cumulative gross points, forced-exit status, and the journal head. Logs record state changes only, not repeated identical blockers.

Rollback: `docker compose -p ast-direct-paper -f docker-compose.ast-direct.yml stop ast-direct-worker`. The journal and database volume are preserved.

## Historical regression gate

The gate reproduces the frozen AST_FUTURES_V1_22_39 decade audit with the production engine. Run it on the research host:

```sh
F=/home/ubuntu/strategy-research/AST_FUTURES_V1_22_39_FREEZE
PYTHONPATH=backend python -m app.adaptive_supertrend.direct.regression \
  --angel-1m-csv /home/ubuntu/ast-research/angel-nifty-1m/nifty50-spot-1m-full-sessions.csv \
  --frozen-ledger $F/ast-decade-fast-trades.csv \
  --frozen-summary $F/ast-decade-fast-summary.csv \
  --frozen-friction-summary $F/ast-futures-friction-summary.csv \
  --json /home/ubuntu/strategy-research/ast-direct-regression.json
```

How it builds the dataset and replays it:

- **Timestamps:** naive timestamps are read as IST wall-clock time; aware ones are converted to IST. The file must be strictly chronological; a disorder or duplicate fails the run.
- **Sessions:** a session counts only if it has exactly 375 one-minute rows from 09:15 to 15:29. Each run of five rows becomes one 5-minute bar, giving exactly 75 bars. A session that isn't exact is excluded whole and listed with its reason. Nothing is synthesized or forward-filled, and sessions are never mixed.
- **Warm-up:** for each session, SuperTrend 22/3.9 runs freshly over the previous 15 exact sessions followed by the current one. The first 15 exact sessions are warm-up only.
- **Replay:** the current session goes through the unchanged production engine, one observation per bar at that bar's open. That gives completed-close breakouts, next-bar entries, next-bar AST-flip exits and the 15:20 forced exit. The legacy `optimizer.signal_trades` 15:30 session-end mark is never a reference.
- **Aggregate gates:** 3,461 trades, +18,031.89 gross points, 1,549.20 drawdown, and +11,109.89 / 1,701.20 at 2 points of friction. Drawdown uses daily equity from zero: closed-trade points summed per trading date. The per-trade-sequence drawdown is reported alongside for information.
- **Ledger parity:** only rows with `atr_period` = 22 and `multiplier` = 3.9 are compared. Trade count, date, direction, entry and exit timestamps, and exit reason must match exactly; entry price, exit price and points allow ±0.005. BULL/LONG and BEAR/SHORT are equivalent.
- **Exit-reason labels:** if the ledger names exit reasons differently, pass the vocabulary explicitly, for example `--exit-reason-map AST_FLIP=FLIP,FORCED_EXIT=SESSION_END`. The report lists the labels found on both sides.
- **On mismatch:** the command exits non-zero and reports the first mismatching trade, the differing fields, and the expected and actual values. Never change the expected values to force a match.

The report also records the SHA-256 of each input and includes the frozen summary CSVs verbatim. `--futures-csv` accepts an already-completed 5-minute research CSV and applies the same exact-session and warm-up rules.

The test `test_frozen_decade_historical_regression` runs the same gate when `AST_DIRECT_ANGEL_1M_CSV` is set. `AST_DIRECT_FROZEN_LEDGER` and `AST_DIRECT_EXIT_REASON_MAP` are optional.

Live warm-up note: the frozen audit warms up on 15 prior sessions, while the worker's operational `history_sessions` defaults to 25. To make live SuperTrend state follow the audit's construction, set `"history_sessions": 15` in `AST_DIRECT_CONFIG_JSON`. That is configuration only and changes no strategy rule.
