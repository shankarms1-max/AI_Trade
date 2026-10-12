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
trading-minute lookbacks require local
history to warm up. Historical strategy-lab replay retains its separate
`--alpha-file` option; the live paper worker uses no CSV.

Start the worker with:

```sh
python /app/worker/acts_paper_worker.py --mode PAPER
```

Read its status and gross paper P&L with:

```sh
python /app/worker/acts_paper_worker.py --mode PAPER --status
```

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
