# MIRROR_EXIT_V1 forward paper experiment

New ACTS_V2, ORB_15/30/60, EMA_PULLBACK and SUPERTREND_RSI_VOLUME trades
store `exit_policy_version=MIRROR_EXIT_V1` in their JSON documents and hashed
policy payloads. Trade events include the version and the complete trade document.
There is no schema change, historical rewrite, threshold optimization or order path.
ACTS_V1 and the legacy scalper/lab policies retain their previous exits.

## Deployment precondition

**Deployment MUST occur with zero active Phase 15 positions**, including pending
entries, OPEN trades and unresolved exposure, in both ACTS and parallel journals.
Do not force this condition by deleting or repairing rows. Let existing paper
positions finish under the installed historical application and resolve any
unresolved evidence through the existing operational process before deployment.

Old CLOSED trades and events remain byte-for-byte unchanged. A legacy ACTS_V2 or
parallel OPEN trade has a different policy hash after this upgrade. If the
precondition is ignored, existing policy-mismatch handling marks it unresolved;
the guard is deliberately not bypassed. ACTS_V1 retains its existing compatibility
path. Future configuration changes are still subject to the existing hash guard.

The ACTS symmetry correction also changes the ACTS policy hash relative to the
initial MIRROR_EXIT_V1 draft in commit `402188b`. Deploy the correction with zero
active ACTS positions as well. Existing CLOSED draft trades retain their stored
policy and `SUSTAINED_OPPOSITE_ACTS_TRADEABLE_THESIS` reason. The parallel
experiments' policy payloads and hashes are unchanged by this correction.

Read-only preflight (both queries must return no rows):

```sql
SELECT strategy_id, state, count(*) FROM parallel_paper_trades
WHERE state IN ('PENDING_PAPER_ENTRY', 'OPEN', 'UNRESOLVED_EXPOSURE')
GROUP BY strategy_id, state;
SELECT state, count(*) FROM acts_paper_trades
WHERE state IN ('PENDING_PAPER_ENTRY', 'OPEN', 'UNRESOLVED_EXPOSURE')
GROUP BY state;
```

## Exit and fill rules

At each observation, exact held-leg book validation remains required. With an
executable book, precedence is forced close at/after **15:20 IST**, configured
kill switch/market event, existing SPREAD_STOP, existing HARD_MAX_LOSS, mirror,
then HOLD. Stop parameters and their comparison operators are unchanged.
The new policy requires the existing forced-close setting to remain 15:20.

Entry remains short BID minus hedge ASK on the next observation. Mark/exit remains
short ASK minus hedge BID. Missing exact contracts remain UNRESOLVED_EXPOSURE.
Other unusable books produce unavailable marks; at forced close they remain
unresolved without a debit or fabricated P&L. A confirmed mirror exit request is
persisted if the exact book is temporarily unusable and closes on the next valid
observed book, subject to the higher-priority exits. No retrospective fill is used.

- ORB uses each trade's stored original range and the existing decision engine,
  confirmation duration, strict inequalities and sequence-gap resets. The exit is
  a confirmed breakout beyond the opposite edge, never simple range re-entry.
- EMA uses the existing completed-bar EMA/pullback/resumption engine with isolated
  trade state. Opposite ordering alone is insufficient.
- Supertrend uses the existing completed-bar Supertrend, RSI and volume engine;
  all three must produce an opposite ENTER, including the volume rule.
- ACTS runs the existing candidate builder on the current captured snapshot for
  the opposite market-state direction, with an isolated per-trade watch state.
  It passes that candidate to the unchanged ACTS V2 `decide` engine, including
  the original spread/depth/credit execution scoring. It exits on the first
  opposite **ENTER**, reason `OPPOSITE_ACTS_V2_ENTRY_SIGNAL`. Native ACTS
  pullback/resumption behavior, configured alpha veto and futures requirements
  remain part of that signal. No additional 60-second confirmation is applied.

ACTS retains the complete 100-point model and existing default bands 40/55/70/85,
with no score rescaling, weight changes or adjusted mirror threshold. An absent
or invalid opposite candidate means no strict mirror signal; SL, emergencies and
forced close still apply. The old candidate-free 86-point path is removed.

The opposite candidate is signal evidence only. Portfolio permissions (open
positions, daily trade/loss limits, consecutive losses and cooldowns) are not
passed to the mirror decision. Normal entry processing retains all its risk
checks; additionally, a mirror close suppresses new entry on that same
observation, even if all other permissions would permit entry. No opposite
trade or order is created by the mirror evaluation. No extra broker request is
made, and the exit itself still uses the exact held spread's executable book.

## Causal diagnostics

`mirror_exit_state`, `mirror_exit_latest`, `mirror_exit_request` and
`mirror_diagnostics` are stored with the trade and journaled on marks. They do not
share the entry cursor. Diagnostic first timestamps are observation times, with
completed-bar end times where applicable. They are never interpolated/backfilled.

Diagnostics include ORB own-boundary re-entry, midpoint and first/confirmed
opposite breakout; EMA adverse ordering, original pullback-anchor close break,
opposite WATCH and ENTER; first opposite ST, ST+RSI and full signal; and all four
existing sustained ACTS component invalidations. ACTS also records the first
opposite candidate, first full opposite ENTER, its original score/band and the
candidate/quote evidence. Component invalidations remain diagnostic-only.
First-event evidence includes
gross P&L when the book is available, MFE, MAE, best gross points and relevant
indicator/thesis evidence. An unavailable book yields null current P&L evidence.

Old profit-capture, trailing and time-stop threshold crossings are recorded only
as diagnostics at the first usable observed book. They never close a new-policy
trade. Existing documents retain actual exit debit/reason, holding duration,
gross outcome, excursions and policy version. Expanded monitor details expose
the version and actual exit reason; accounting remains GROSS ONLY.

No outcome comparison, expectancy improvement or profitability claim follows
from this change. Evaluate the forward observations before changing parameters.
