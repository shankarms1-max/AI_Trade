# Phase 15 paper positions and journal

`/phase15` retains the six-strategy overview and adds independent **Open
Positions** and **Trade Journal** sections. There are no order controls. Each
section has independent loading/error handling and refreshes every 20 seconds.

## Read-only API

- `GET /api/phase15/positions?limit=100&offset=0` includes pending, open and
  unresolved projections across all entry dates, without depending on cursors.
- `GET /api/phase15/journal?date=2026-10-15&strategy=ACTS_V2&state=CLOSED&limit=100&offset=0`
  reads the existing ACTS V2 and five parallel trade projections. Omit `date`,
  `strategy` or `state` for All. The UI defaults to Today / All strategies / All
  states. Dates are decision-observation dates in IST, not exit dates.

Both return bounded pages with `items`, `limit`, `offset`, `has_more`, `as_of`
and explicit `GROSS_ONLY` accounting. Maximum page size is 100. Order is newest
decision observation first, with strategy/ID tie-breakers. No new journal,
schema, persistence, broker calls, engine invocation or writes are introduced.
PostgreSQL transactions explicitly use `SET TRANSACTION READ ONLY`.

MTM comes only from stored gross marks. A mark must be the latest successful
attempt for the current projection's snapshot, dated today and not in the
future. Missing/failed/prior-day marks, pending entries and unresolved exposure
return null current debit and MTM. The UI displays `MARK UNAVAILABLE`, not an
inferred price or P&L. Same-day marks are labelled by their captured timestamp;
the UI does not promise a live tick when capture has stopped. MFE/MAE remain
stored points. Open holding duration is elapsed wall time, not a simulated mark.

Expanded research details allowlist stored score/band, rejection counts, mirror
timestamps, quote fields, false-breakout timing and policy hash. Arbitrary stored
objects and secrets are not dumped. Historical exit reasons are displayed as
stored, including `TIME_STOP` and earlier mirror reasons.

## Fixed-400 correction (new Phase 15 construction only)

ACTS V2 and all five parallel strategies construct only 400-point candidates and
store `allowed_widths: [400]` in their new policies. Missing executable 400-point
hedges reject construction; no narrower fallback is used. Existing quote,
liquidity, credit and loss checks remain unchanged. The original ACTS V1
compatibility policy/hash and non-Phase-15 construction are unchanged.

The UI derives width from the stored candidate or its stored strikes. It never
assumes 400 for a historical trade. Historical policies, widths, exit reasons
and event hashes are not rewritten.

**Later deployment requires zero active Phase 15 positions**, including pending
and unresolved exposure. This construction correction changes the current policy
hashes. Existing hash guards remain intact: processing an old active position
under a changed policy marks it unresolved rather than silently reinterpreting
its rules. This change does not bypass that guard or provide a migration of open
positions. Follow the read-only preflight in `mirror-exit-v1.md` before any later
deployment. No deployment or production flag change is part of this work.
