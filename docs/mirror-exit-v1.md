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
- ACTS uses the exact existing market-state and score calculation with a separate
  watch state, no candidate and no entry risk gates. A TRADEABLE/STRONG/VERY_STRONG
  opposite thesis must persist for 60 seconds. The existing ACTS reset on a gap
  greater than 30 seconds is shared. Configured alpha veto and missing futures
  evidence still prevent thesis qualification.

ACTS's original execution score requires a candidate. Its existing `candidate=None`
semantics contribute **zero execution points**, with no rescaling and the same
score bands/weights. Evidence labels this constraint explicitly. Default execution
weights total 14, so the maximum without execution is 86 out of the original 100.
An environment whose tradeable threshold exceeds this ceiling cannot produce an
ACTS mirror exit; this is recorded, never replaced with a new formula.

## Causal diagnostics

`mirror_exit_state`, `mirror_exit_latest`, `mirror_exit_request` and
`mirror_diagnostics` are stored with the trade and journaled on marks. They do not
share the entry cursor. Diagnostic first timestamps are observation times, with
completed-bar end times where applicable. They are never interpolated/backfilled.

Diagnostics include ORB own-boundary re-entry, midpoint and first/confirmed
opposite breakout; EMA adverse ordering, original pullback-anchor close break,
opposite WATCH and ENTER; first opposite ST, ST+RSI and full signal; and all four
existing sustained ACTS component invalidations. First-event evidence includes
gross P&L when the book is available, MFE, MAE, best gross points and relevant
indicator/thesis evidence. An unavailable book yields null current P&L evidence.

Old profit-capture, trailing and time-stop threshold crossings are recorded only
as diagnostics at the first usable observed book. They never close a new-policy
trade. Existing documents retain actual exit debit/reason, holding duration,
gross outcome, excursions and policy version. Expanded monitor details expose
the version and actual exit reason; accounting remains GROSS ONLY.

No outcome comparison, expectancy improvement or profitability claim follows
from this change. Evaluate the forward observations before changing parameters.
