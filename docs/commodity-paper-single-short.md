# Commodity ACTS: single-leg short-option PAPER research

Version: `COMMODITY_ACTS_NAKED_PAPER_V1`. Supersedes new commodity spread entries,
not historical records. This is not authorization to deploy or trade live.
All activation switches remain OFF by default; NIFTY code and flags are unchanged.

## Direction, selection and ACTS scoring

The existing ACTS V2 market-state, watch, directional/Alpha scoring, thresholds,
weights and mirror signal logic are reused without changing the shared NIFTY
implementation. Bull evaluates an actual OTM PUT; bear evaluates an actual OTM
CALL, relative to the selected MCX future. No hedge or spread width is constructed.

Candidates require exact identities, fresh causal timestamps, positive uncrossed
tick-aligned bid/ask, observed OI/volume, and sufficient underlying-unit depth.
Both executable sides must have one lot available at selection/entry. Selection
balances the existing short-quality components: meaningful premium, distance in
observed strike steps, available support/resistance and range position, OI/volume
and bid/ask quality. Ties prefer lower modeled stress loss then exact identity.
This is deterministic, not a maximum-premium sort. All components survive in the
journal. Stress scenarios are explicit estimates, not additional market inputs.

The commodity-only scoring compatibility view supplies the **same option's actual
two-sided book** to the original ACTS execution-quality calculation. It does not
create a hedge. The legacy scorer's credit/width input is explicitly mapped to
premium / stress-close-premium estimate; there is no real spread denominator or
defined-loss claim. That mapping is versioned and recorded as
`single_option_execution` / `score_mapping`, not hidden in an invented contract.
Momentum, structure, Alpha, positioning and participation still use the original
functions and parameters. No shared ACTS file or NIFTY strategy was changed.

## Units, fills and accounting

The operator's final decision on 2026-10-22 confirms Kotak MCX depth as **underlying
units**: CRUDEOILM one lot = 10 barrels; NATGASMINI one lot = 250 MMBtu. New captures
store `UNITS` with explicit provenance and no conversion. Historical `UNKNOWN`
rows remain untouched and deserialize as before. Exact dated policy evidence must
agree with these lot sizes and cash multipliers; a LOTS interpretation is refused
for new naked policies. Tick and option/future-link evidence are still required;
the raw master tick is not silently divided or otherwise inferred.

- Signal observation queues one durable SELL request; no same-observation fill.
- A later observation, with source time newer than signal **and actual creation**,
  sells exactly one lot at the observed BID. No queue/hidden-depth inference.
- The initial mark is null. Subsequent eligible observations mark at observed ASK.
- Exit signals queue a BUY; a later eligible observation fills at observed ASK.
- Gross P&L = `(entry BID premium - exit ASK premium) * underlying quantity`.
- Zero bids, crossed/missing prices, stale or noncausal quotes, insufficient depth,
  partial depth and changed contract identities cannot manufacture a fill.
- Net is null unless a complete dated cost model is configured. A completed single
  option deducts **two** inclusive per-leg execution costs, not a spread's four.
- Estimated broker margin is never actual blocked funds or a guaranteed capital
  allocation. Unknown margin and return-on-capital fields stay null, never zero.

## Naked risk is not defined spread risk

`max_loss_per_trade` retains its legacy configuration name but is explicitly a
**cash premium-loss exit trigger**, NOT a maximum possible or guaranteed loss.
`stop_credit_multiple` is the premium-multiple stop. Neither protects against gaps,
missing liquidity or delayed observations. New records have `maximum_loss=null`,
`loss_is_bounded=false` and no `defined_risk` or spread-width field.

Required scenario inputs are `stress_underlying_move_pct`,
`stress_premium_multiple` and `max_stress_loss_per_trade`. No live risk values are
supplied by this change. The adverse future scenario is down for puts/up for calls.
Scenario closing premium is the greater of observed ASK times the stress multiple,
or stressed intrinsic plus observed ASK. It is a conservative explicit scenario,
**not a pricing model, observed price, probability, Greeks estimate or loss bound**.
Nonfinite scenarios fail closed. Entry and fill recheck the scenario cap. Risk
reservations are at least the cash-stop trigger and modeled stress loss; observed
adverse marks can increase them, never clamp P&L or pretend to bound losses.

One lot and one pending/open/unresolved position per instrument are enforced.
The existing commodity-only mutex serializes both instruments' position limits,
global exposure reservations, independent daily-loss limits and session trade
limits. `global_max_stress_risk` is a new required positive exposure budget for
naked entries; `global_max_defined_risk` remains only for historical spread recovery.
Missing budgets block new entries. Kill switches still permit exit management.
An unvalued/stale existing position blocks new risk. Immediately after entry its
fresh validated two-sided book can support reservation checks, but not a fake mark.

Automatic exits retain profit capture, cash/premium stops, trailing giveback, full
opposite ACTS entry signal, holding-time and forced session-end exits. Stress-cap
violations also request an exit. No loss is clamped to a modeled reserve or stop.
Missing/stale future references leave the scenario unavailable and request risk
reduction instead of presenting stale stress estimates as verified. An entry-only
margin ceiling does not block an executable opposite ACTS signal used to close
existing exposure; it is enforced again before any real subsequent paper SELL.

`pre_expiry_exit_days` is at least one calendar day; `pre_expiry_exit_time` is an
explicit IST wall-clock setting. Entries are refused within their TTL of that
cutoff. Open exposure requests exit by the cutoff. Session rollovers/forced exits
are additional protections. Missing quotes/contracts retain unresolved exposure,
its reservation and original reason; no expiry settlement or successful exit is
invented. The API reports overdue pre-expiry exposure even if capture has stopped.
Workers must remain running for exits: disabling entry flags does not flatten a
position. Complete capture outages cannot produce synthetic paper observations.

## Optional official single-instrument margin capability

The default worker's consumer-key-only client has no completed trade-auth session.
It therefore continues to label margin `MARGIN_UNVERIFIED`. An optional explicit
`authorized_margin_reader(existing_sdk, clock)` can be injected into the coordinator
by a separately authorized read-only host integration. It does not log in, obtain
credentials, expose order methods, or place anything. There is no new live adapter.

The capability calls only official `margin_required`, with `mcx_fo`, SELL, NRML,
one exact token, underlying lot quantity, and the observed proposed BID. It accepts
only successful validated positive `ordMrgn` (documented order margin), never
account cash, total used margin, missing/zero values or an invented formula.
Errors are sanitized to unavailable. Only allowlisted estimate fields are retained.
See [official field mapping](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/portfolio-and-funds-apis/margins.md).

Estimates are **account-contextual single-instrument estimates**, not independently
verified standalone portfolios. Evidence records use
`measurement_kind=SINGLE_INSTRUMENT_ESTIMATE`, `portfolio_verified=false`, exact
token/expiry/lot/NRML identity, source and actual receipt time. Frozen externally
obtained `SingleMarginEvidence` may also be configured. Records must be unique,
current and not future-dated. Margin is refreshed/revalidated at fill; an expired
quote during an optional margin lookup cannot fill. No margin lookup is required
for exits. If `max_broker_margin` is configured, unavailable/over-limit evidence
blocks entry/fill. Without it, explicitly configured stress-limited PAPER research
can proceed with `MARGIN_UNVERIFIED`; this is not a claim about capital feasibility.

## Persistence and historical compatibility

No schema or migration change is required: existing commodity-only JSON documents
and immutable event/fill tables hold the versioned single-leg evidence. Decisions,
pending/rejected SELL/BUY requests, fills, risk scenarios, policies, timestamps,
rejections and exits remain auditable in the hash-chained journal. No historical
row is rewritten on upgrade. Policy loading preserves legacy hashes and four-leg
spread costs. Existing open/pending-exit/unresolved spreads keep their original
two-leg recovery/exit logic. Historical pending spread entries are rejected rather
than filled under the new strategy. No new spread entry is allowed.
A legacy LOTS attestation conflicting with a new confirmed-UNITS quote is refused
for execution and requires operator review; history is never silently reinterpreted.

The read-only API and `/commodity-acts` dashboard show single shorts and quantities,
estimated stress risk, gross versus supported-cost net, unknown margin, overdue
expiry exits, and clearly labeled historical spread legs. They never infer live
worker state or trading activation from journal activity. Profitability claims
remain false. Restart/duplicate/rollback/global-lock protection is retained.

## Prerequisites for a separately approved controlled PAPER deployment

1. Back up/review the existing commodity store and ensure migration 0021 is already
   present. No new migration is necessary. Do not alter historical journals.
2. Provide exact current, dated per-instrument contract/tick/linkage evidence covering
   the intended entry and exit window. Do not reuse test fixtures as live evidence.
3. Set each `COMMODITY_ACTS_PAPER_POLICIES_JSON` entry's
   `strategy=NAKED_SHORT_OPTION`; supply the required stress scenario/cap, explicit
   cash loss trigger, daily limits, quote spread ceiling and pre-expiry cutoff time.
   Reuse existing profit/stop/trailing/holding-time and trade-count controls unless
   deliberately reviewed separately. Old spread policy JSON is recovery-only.
4. Configure positive `COMMODITY_ACTS_GLOBAL_MAX_STRESS_RISK` and
   `COMMODITY_ACTS_GLOBAL_DAILY_LOSS_LIMIT`, plus verified per-instrument sessions,
   holidays and forced-exit time with the existing observation buffer. Existing
   global-policy drift guards are not bypassed when historical exposure exists.
5. Decide explicitly whether paper research may proceed with unverified margin.
   For a hard margin ceiling, provide a current verified estimate or the optional
   already-authorized read capability. Default consumer-key-only access cannot do
   margin queries. Do not enable trade authentication just to manufacture evidence.
6. Only after approval, enable `COMMODITY_ACTS_PAPER_ENABLED`, the selected instrument
   enable flags, and `COMMODITY_ACTS_TRADING_ENABLED` (paper entries only). The CLI
   accepts **only PAPER**. No flags were enabled by this implementation.

Existing worker commands (not executed as part of this change):

```sh
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI
```

The existing 15-second observations, batch quote acquisition and per-instrument
offsets remain unchanged. Establish live-session operational evidence for the
new strategy before unattended use. No paper fill proves an actual broker fill;
latency, queueing, canceled liquidity and price impact remain limitations. There
is no deployment, production access, broker order capability or profitability claim.

## Validation of this change

- Affected backend regression (commodity capture/lifecycle/migrations, original
  ACTS V2 and selected Phase 15 compatibility): **378 passed, 2 optional PostgreSQL
  tests skipped** in that offline run. Those migration checks were separately
  exercised against real PostgreSQL below, not replaced with SQLite results.
- Final lifecycle regression after safety refinements: **110 passed**. Final
  dedicated naked-option suite, including the legacy-units conflict guard:
  **44 passed**. These suites overlap; their counts are not additive.
- Disposable loopback-only Docker PostgreSQL 16: **60 passed, zero failed/skipped**.
  Actual migrations, existing research/NIFTY preservation, fresh database, single
  entries/exits/margins/P&L, expiry/unresolved recovery, concurrent instruments,
  duplicate observations, atomic rollback, immutable journals and legacy spread
  recovery were tested. The disposable server was removed afterward.
- Frontend: **74 passed**, lint, TypeScript and production build passed. Build
  includes `/commodity-acts`, `/`, `/phase15`. `git diff --check` passed.
- The existing Starlette/httpx deprecation warning is unrelated to this change.

No live MCX discovery/quote pass, authenticated margin request, worker startup,
production migration or deployment was performed. The optional single-instrument
margin reader is mock-tested, not live-verified. Controlled paper deployment still
requires the configuration/evidence checklist above, monitoring of unresolved
exposure, and market-session validation of the new scoring/execution behavior.
