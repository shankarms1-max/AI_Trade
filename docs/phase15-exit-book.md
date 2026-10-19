# Phase 15 executable exit books

`PHASE15_EXIT_BOOK_V1` applies only to ACTS V2 PAPER and the five parallel PAPER
strategies. It is selected explicitly by their services. Other scalper/replay
callers and ACTS V1 retain their existing validation contract by default.

The spread-percentage ceiling remains an entry-quality requirement. Entry
validation, entry evidence payloads/candidate hashes, OI/volume checks, 400-point
construction, all risk limits and BID-minus-ASK entry pricing are unchanged.

For an existing position the ceiling becomes a quality warning. Exact identity,
positive/non-crossed bid and ask, ticks, timestamp freshness, known depth units,
required exit-side quantity and executable spread-payoff checks still apply.
Only the observed short ASK minus hedge BID supplies the debit. No midpoint,
LTP, carried-forward book or theoretical value supplies a mark or fill.

Each new-policy exit validation retains `spread_pct`, `wide_spread` and
`quality_warning`. A wide valid book has `valid=true`, `reason=null` and warning
`BID_ASK_SPREAD_TOO_WIDE`. Marks/exits persist this evidence. A warning does not
block mirror, emergency, stop or forced exits. The MIRROR_EXIT_V1 signal policy,
SPREAD_STOP, HARD_MAX_LOSS and 15:20 IST forced-close precedence are unchanged.

Mark events/projections record `mark_quality_warning`. Parallel valid marks also
record their `last_snapshot_id`, allowing the positions API to verify that the
mark belongs to the latest successful observation. The API retains its checks
for a current, same-day, non-future mark; latest failed attempts return no MTM
or quality warning. Valid wide marks remain available and expose the warning
and per-leg spread diagnostics. Historical stored marks are not backfilled.

## Policy and deployment safety

Both new ACTS V2 and parallel policy payloads include `exit_book_policy`, whose
version is `PHASE15_EXIT_BOOK_V1`. This changes their deterministic policy hashes.
Entry-policy versions and MIRROR_EXIT_V1 itself do not change. No migration or
production configuration change is needed for the new JSON evidence fields.

**Do not deploy this revision over old-policy active positions.** Startup and
serialized processing refuse old-policy PENDING_PAPER_ENTRY, OPEN and
UNRESOLVED_EXPOSURE trades. New-policy active trades can restart normally.
Existing documents, stored policy hashes, entry/exit prices, journal events,
cursor history and terminal classifications are never migrated or rewritten.

Read-only preflight: both queries must return no rows before an upgrade:

```sql
SELECT state, count(*) FROM acts_paper_trades
WHERE document::jsonb->>'strategy_version' = 'ACTS_V2'
  AND state IN ('PENDING_PAPER_ENTRY','OPEN','UNRESOLVED_EXPOSURE')
GROUP BY state;
SELECT strategy_id, state, count(*) FROM parallel_paper_trades
WHERE strategy_id IN ('ORB_15','ORB_30','ORB_60','EMA_PULLBACK','SUPERTREND_RSI_VOLUME')
  AND state IN ('PENDING_PAPER_ENTRY','OPEN','UNRESOLVED_EXPOSURE')
GROUP BY strategy_id, state;
```

This means today's OPEN positions cannot silently receive new executable-book
semantics. Keep the installed worker managing them under its frozen policy;
they may close naturally only with evidence valid under that installed policy.
If it instead leaves terminal UNRESOLVED_EXPOSURE, this upgrade remains blocked.
This patch neither provides a recovery command nor fabricates a closure to
achieve zero exposure. Reinterpreting an existing active trade requires a
separate explicitly authorized journal-safe plan, not a policy-document edit.
Historical CLOSED and ENTRY_REJECTED rows do not block startup and remain intact.

No production database access, orders, deployment or push is performed here.
