# ACTS V3 opportunity-cycle paper engine

ACTS V3 is opt-in through `ACTS_PAPER_STRATEGY_VERSION=ACTS_V3` and the existing
`worker/acts_paper_worker.py --mode PAPER` command. The default remains V2.
This change does not enable a worker, change an environment file, or deploy anything.

V3 wraps the unchanged V2 market-state and 100-point scoring functions. Its
candidate construction explicitly uses V2's short-leg-first ordering and exactly
400-point spreads. Entries still fill on a later observation at short BID minus
hedge ASK; exits use the exact held short ASK minus hedge BID. Mirror exits,
exit-book validation, expiry terminalization, costs and 15:20 forced close are
inherited. In particular, the original full opposite **V2** ENTER signal (55-point
floor under default configuration) still exits immediately. V3's entry gate is
never used to filter that exit signal.

## Entry lifecycle

| State / observation | Result |
| --- | --- |
| New IST session | `ARMED`, no consumed directions |
| First executable V2 opportunity, score >=60, hard gates clear | Reserve cycle, `ENTERED`, create pending entry |
| Next-observation executable fill | Mark that direction consumed; remain `ENTERED` |
| Actual trade exit or inherited unpriced expiry terminalization | `CONSUMED`; store actual reason/time |
| Plain continuation in a consumed direction | `CONSUMED_CYCLE` |
| New flat-state V2 pullback | `RESET_OBSERVED` |
| V2 pullback resumption at >=60 | `REARMED`, then `ENTERED` when risk permits |
| Opposite continuation after mirror exit | `REVERSAL_QUARANTINE` |
| Quarantined opposite full signal >=85 for continuous >=60 seconds | `EXCEPTIONAL_REVERSAL_CONFIRMATION`, rearm |
| Return to the prior direction at >=60 after mirror exit | `RETURN_AFTER_FALSE_REVERSAL`, rearm |
| Pending entry fails without a fill | Release its reservation without consuming a trade/opportunity |

Rearming same-direction pullbacks records `PULLBACK_RESUMPTION`; opposite
pullbacks record `OPPOSITE_PULLBACK_RESUMPTION`. A pullback observed while an
existing position is held cannot reset the entry cycle: a new pullback must be
observed while flat after consumption. The unchanged V2 setup lifetime still
governs whether a pullback/resumption is genuine. A low-score resumption does not
erase an otherwise live setup before it can satisfy V3's score floor.

The exceptional timer uses causal observation timestamps, not wall-clock worker
uptime. Five valid 15-second observations span the required 60 seconds. A gap
**greater than 22.5 seconds** resets confirmation, using the existing scalper
15-second cadence ×1.5 gap convention. Missing full signal/book, score below 85,
direction loss, or futures/option identity change also resets it. Already-confirmed
permission waiting for risk clearance is revoked if continuity is subsequently
lost. Confirmation can accumulate during a cooldown; it never bypasses that
cooldown or any portfolio gate.

Every entry still obeys one-open-position, kill/event gates, exact quote/depth
requirements, configured minimum credit/max loss, daily loss, loss streak and
exit/loss cooldowns. The emergency daily ceiling is `min(configured_cap, 6)`.
Previous-version same-day trades still count toward the shared ACTS risk ledger;
switching versions does not reset a daily budget. A stop does not release its
consumed direction merely because the loss cooldown expires.

## Persistence, versioning and historical integrity

`ACTS_V3_OPPORTUNITY_CYCLE_V1` parameters are included in a deterministic V3
execution-policy hash. The existing cursor's `watch_json` contains the V3 cycle
and nested V2 signal watch. Each observation journals the complete watch and
decision; fill/exit/rejection changes append `ACTS_OPPORTUNITY_CYCLE` events in
the same transaction as their normal trade events and projections. Entry audit
evidence is retained separately as `opportunity_cycle_entry`. Restart checks the
existing hash chain and projection before resuming the persisted timer/cycle.
Duplicates cannot append another observation or fill. A failure rolls back the
entire fill/cycle/journal transaction.

Audit fields include `cycle_id`, `cycle_state`, `rearm_reason`,
`previous_direction`, `previous_exit_reason`, `previous_exit_timestamp`,
`reversal_confirmation_seconds`, and `opportunity_cycle_policy_version`.
Combined performance, monitor and paginated trade reports keep V2/V3 separate;
V3 trade pages include the cycle audit. V2 history is read without invoking its
engine or rewriting any historical policy, row, event or hash.
V3 worker-status P&L is also V3-only. Its remaining-entry risk budget deliberately
includes prior-version same-day fills and is labelled `ALL_ACTS_VERSIONS`.

V3 startup refuses **all** prior-version pending/open/unresolved exposure,
including expired exposure. It also refuses an incompatible policy for active
V3 trades. There is no automatic migration or repair of those positions. No
schema migration is required. Use only one ACTS worker for this shared journal;
this is a version replacement, not permission to run V2 and V3 concurrently.
Version switches and rollbacks must occur only when active exposure is zero.

## Explicit interpretations and limits

- The 60-point floor applies to the entry decision. Pending fills retain V2's
  existing next-observation TTL, direction, quote and fill-economics checks;
  no new score-based fill or exit rule is introduced.
- Only mirror exits quarantine the opposite direction. A never-consumed opposite
  direction after a different exit can take its first opportunity, subject to
  every unchanged risk gate.
- A reserved pending entry uses the conceptual `ENTERED` state; its direction
  becomes consumed only on a real simulated fill. No fill means no consumed trade.
- Expired, unpriced exposure retains the existing explicit unavailable-price/P&L
  semantics. Cycle terminalization never invents an exit price or realized P&L.
- Tests use labelled synthetic causal market observations. No claim is made about
  V3 profitability or production forward performance.
