# ACTS_V1 paper worker

The ACTS paper worker is independent of the Phase 15 scalper worker and the
Phase 14 paper journal. It captures Kotak market data into the existing Phase
15 snapshot and quote tables, computes the existing frozen ACTS_V1 decision,
and writes `acts_paper_cursor`, `acts_paper_trades`, and
`acts_paper_events` for paper decisions and fills. It imports no broker order
adapter. The worker refuses a snapshot builder with an order method.
ACTS capture buckets carry their own `ACTS_PAPER` mode, so a concurrent
research or score-based scalper capture cannot replace its held-contract
quotes or enter its historical context.

Run migration `0018_acts_paper` before starting the worker. Set
`ACTS_PAPER_ENABLED=true` only in the dedicated paper worker environment.
`SCALPER_ENABLED` may remain either true or false. The same Kotak capture
supplies a local three-minute raw observation to the existing Phase 14.1
Alpha service. It stores `phase14_1_v1` / `LIVE_ORIGINAL` / `FUTURE` evidence
in this Phase 15 database. This dedicated capture does not require enabling
the separate Phase 14 `ALPHA_ENGINE_ENABLED` pipeline. ACTS uses the existing
causal matcher against those local rows; stale, future, invalid, or
identity-mismatched Alpha cannot open a paper trade. The 800/300
trading-minute lookbacks use eligible Phase 15 research captures as causal
prior history. No historical rows are copied into `market_snapshots`.
Historical strategy-lab replay retains its separate `--alpha-file` option;
the live paper worker uses no CSV.

Warm-up selects one capture from each completed three-minute bucket. Existing
local Alpha raw captures take precedence, followed by `ACTS_PAPER` and
`RESEARCH_ONLY` captures with verifiable read-only capture keys. A legacy row
must carry an explicit `LEGACY_RESEARCH` / `KOTAK_NEO_READ_ONLY` provenance
marker and complete option price, volume, OI, timestamp, and identity fields.
Older strategy-evaluated rows labelled `OTHER` do not qualify automatically.
The earliest capture in each mode and bucket is considered. If it fails field
checks, the next mode may supply that bucket. The current bucket, all later
captures, and rows persisted after the Alpha observation are excluded. The
history query uses the configured lookbacks and a fixed row limit; option
quotes are loaded only for chosen buckets every three minutes, never on the
15-second decision path.

Start the worker with:

```sh
python /app/worker/acts_paper_worker.py --mode PAPER
```

Read its status and gross paper P&L with:

```sh
python /app/worker/acts_paper_worker.py --mode PAPER --status
```

`latest_observation.alpha_diagnostics` distinguishes warming, invalid,
stale, identity-mismatched, and available Alpha. `alpha_history` reports the
reconstructed history span, trading minutes, row count, and capture-mode
split. `latest_observation.regime_diagnostics` shows the observed spot,
future, 3/5/15-minute returns, structure, opening range, VWAP, each ACTS
directional gate, and the main reason a regime remained RANGE. The 3-minute
threshold is context-only; ACTS_V1 still gates on its frozen 5- and
15-minute thresholds. Alpha readiness booleans describe whether the full
configured lookback is represented within one sampling bucket; they do not
add an entry gate or change the Alpha engine's validity rules.

The scheduler captures every 15 seconds from 09:15 through 15:20 IST on
market weekdays, excluding configured holidays. A decision marked `ENTER`
creates `PENDING_PAPER_ENTRY`. The next observation must still pass exact
contract, quote, risk, and ACTS entry checks. A paper fill sells the short
leg at bid and buys the hedge at ask. An open trade is marked using short ask
minus hedge bid. Only the existing ACTS thesis, hard-risk, profit, trailing,
time-stop, and 15:20 forced-exit rules can close it. Missing exact contracts
cannot create a mark or fill; an open exposure becomes terminal
`UNRESOLVED_EXPOSURE`. Journal events are append-only and hash-chained.

The worker starts with journal verification and uses a locked cursor for one
ordered evaluation per captured snapshot. Restart restores the watch state,
held contracts, trade state, and latest status. Repeated evaluations of the
same snapshot are idempotent. Realized and unrealized status fields are gross
paper P&L; unresolved exposure has no fabricated unrealized P&L.
