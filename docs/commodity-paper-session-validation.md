# Controlled Commodity ACTS paper-session proposal

Prepared against `cf30b200e9c2c2fdc5fe653dff7235275ca1496c` on
`feat/commodity-acts-paper-trades`, 2026-10-22 IST. **Proposal, not activation.**
No strategy code, environment file, database, worker or deployment is changed.
The user-specified NIFTY dashboard null-safety guards are now reconciled locally;
this checklist does not authorize an image deployment or worker activation.

## Initial limits: finite operational research budgets, not calibrated risk claims

| Current policy field | CRUDEOILM | NATGASMINI |
| --- | ---: | ---: |
| `strategy` | `NAKED_SHORT_OPTION` | `NAKED_SHORT_OPTION` |
| `lots` / `max_positions` | 1 / 1 | 1 / 1 |
| Underlying quantity per lot | 10 barrels | 250 MMBtu |
| `max_loss_per_trade` (cash-loss exit trigger) | INR 2,500 | INR 1,500 |
| `max_stress_loss_per_trade` | INR 6,000 | INR 8,000 |
| `stress_underlying_move_pct` | 5% | 10% |
| `stress_premium_multiple` | 2 | 2 |
| `daily_loss_limit` | INR 6,000 | INR 8,000 |
| `max_trades_per_session` | 2 | 2 |
| `pre_expiry_exit_days` / `pre_expiry_exit_time` | 1 / 20:00 IST | 1 / 20:00 IST |

Global proposal: `COMMODITY_ACTS_GLOBAL_MAX_POSITIONS=2`,
`COMMODITY_ACTS_GLOBAL_MAX_STRESS_RISK=14000`,
`COMMODITY_ACTS_GLOBAL_DAILY_LOSS_LIMIT=14000`. These are **not margin or capital**.
Do not use the legacy `GLOBAL_MAX_DEFINED_RISK` as the naked strategy's budget.

Why these numbers:

- Cash triggers correspond to adverse premium moves of INR 250/barrel and
  INR 6/MMBtu for one lot. The unchanged 2x-premium stop can trigger earlier.
  Neither cash trigger, stress scenario nor daily threshold guarantees a loss cap.
- The 5%/10% adverse-future scenarios plus 2x ASK premium shock are explicit trial
  stress assumptions, not forecasts, volatility estimates, exchange limits or
  claimed worst cases. The gas scenario deliberately tests a larger relative move;
  it is not evidence of statistically measured relative volatility.
- For scale only, the previously observed **2026-10-21 23:02** 9200 CRUDEOILM PUT
  (future 9210, BID 488.60, ASK 489.50) produces INR 4,904 modeled stress loss under
  this proposal. The observed 265 NATGASMINI PUT (future 266.40, BID 6.20, ASK 6.30)
  produces INR 6,335. This uses the existing stress formula, not new quotes or fills.
  Both fit the proposed per-trade caps; those caps are not deliberately zero-trade
  budgets for the known sample. This is **not** validation of today's contracts,
  all candidate strikes, tail risk, profitability or the adequacy of these limits.
- Global budgets equal the sum of both instrument ceilings. Daily entry checks
  reserve modeled risk and consume headroom after losses. Two trades is a ceiling,
  not a promise: the second may correctly be refused before the daily exit threshold.
  At most four entries across both instruments are intended for the first session.

No entry criteria, ACTS/Alpha thresholds or weights are changed or optimized. Keep
the current automated exits: 50% profit capture, 2x premium stop, trailing activation
25% / giveback 15%, 30-minute holding limit, genuine opposite ACTS signal and forced
session exit. These existing defaults are retained, not claimed to be calibrated.

Exit conditions are alternatives, **not cumulative requirements**. The first
eligible observation that satisfies any applicable exit condition queues an exit;
the engine does not wait for both cash-loss and premium-multiple stops. For a
single lot, the two loss stops correspond to an adverse premium move of
`min(max_loss_per_trade / lot_size, entry_premium * (stop_credit_multiple - 1))`.
Stress, daily/global risk, trailing, profit, reversal, holding, kill-switch,
session and expiry conditions can request an exit earlier. When conditions
coincide, the existing engine's deterministic priority selects the recorded reason;
it does not establish which threshold was crossed first between observations.
For example, the old CRUDE sample premium 488.60 gives a cash-stop move of 250
versus a premium-stop move of 488.60; the old gas premium 6.20 gives 6 versus 6.20.
Thus these cash stops can trigger before the 2x-premium stops without conflict.

These are **request thresholds, not guaranteed exit prices, fills or maximum
losses**. The subsequent BUY needs a fresh executable ASK with sufficient observed
depth and a source timestamp after the request. Gaps, adverse moves or missing
books can exceed every proposed trigger; retain pending/unresolved exposure until
an actual simulated fill is journaled. No stop, daily limit or stress scenario
makes naked short-option loss bounded.

## Actual time controls supported by this code

For a **normal trading day**, propose both instruments' `SESSION_START=09:00:00`,
`SESSION_END=23:30:00`, `FORCED_EXIT=22:30:00`, with the official MCX source recorded
in each `SESSION_SOURCE`. 22:30 is an operator-selected research exit time, not an
exchange close. Keep capture/exit management running to 23:30, giving an hour of
ordinary-session recovery buffer. Review current exchange notices before each run.

Keep `entry_ttl_seconds=60`. The existing engine stops **new entry decisions** at
22:29 (forced exit minus TTL); pending entries cannot fill on an observation at or
after 22:30. The actual forced BUY request is generated on the first eligible
observation at/after 22:30 and needs a subsequent executable quote to fill. These
are observation-time controls, not guarantees of exact wall-clock liquidation.

On the calendar day before the **dynamically resolved option expiry**, the expiry
cutoff takes precedence: stop new decisions at 19:59, reject pending entries at
20:00, and queue exits at/after 20:00, filled only on later valid ASK books. Keep
`min_entry_dte=1`; no intentional expiry-day/devolvement exposure. If discovery still
resolves a 2026-10-23 NATGASMINI option, this cutoff applies on 2026-10-22. Do not
assume that contract remains selected without a fresh master lookup.

There is **no independent entry-cutoff setting** in this revision. Do not invent
an `ENTRY_CUTOFF` environment key: unknown settings can be ignored. A separate
22:00 last-entry / 22:30 forced-exit schedule cannot be advertised as automatically
implemented here. The proposal above uses the controls that actually exist.

The fixed-session configuration cannot describe split/partial holidays or every
special session. For the first trial, conservatively exclude **all** dates listed
in the MCX holiday calendar, including partly open dates, and exclude special/weekend
sessions. This is a research restriction, not a claim that MCX is closed all day.
Use a reviewed normal weekday; do not derive sessions from weekdays alone.

## Contract evidence: documented constants, dynamic identities

| Evidence | CRUDEOILM | NATGASMINI |
| --- | --- | --- |
| `lot_size` / `rupees_per_point_per_lot` | 10 / 10 | 250 / 250 |
| **Option** `tick_size` | INR 0.05 | INR 0.05 |
| Futures tick, for reference only | INR 1 | INR 0.10 |
| Execution `depth_unit` | `UNITS` | `UNITS` |

The policy's tick field is the **option** tick; do not substitute the futures tick
or guess a divisor for raw master `dTickSize`.

Before starting an approved paper session:

1. Reuse the existing exact-family `mcx_fo` scrip discovery and batch quote adapter.
   Obtain actual future token/symbol/expiry and the actual nearest option expiry.
   Never copy yesterday's tokens/expiry into today's policy or choose another series
   merely to evade an expiry restriction.
2. Check readable and Unix master expiries agree, lot sizes match the constants,
   and quoted tokens/symbols match the requested exact master identities. Reject
   broker inconsistencies rather than overwriting its metadata with constants.
3. Verify the selected option maps to that actual mini future using the applicable
   MCX contract calendar/specification. Both option specifications state expiry two
   business days before the underlying future. Do not set
   `option_future_link_verified=true` merely because the nearest two expiries were
   independently chosen; the raw adapter deliberately records that limitation.
4. Populate exact `future_token`, `future_expiry`, `option_expiry` and dated,
   timezone-aware `verified_at` / `valid_until` evidence from the real preflight.
   Do not backdate an attestation or make up an expiry/timestamp to complete JSON.
   Evidence must cover the planned entry and exit/recovery window.
5. Preserve a visible provenance note: **Kotak Neo MCX underlying-unit semantics
   confirmed by the account user on 2026-10-22; not inferred from sample values.**
   New captures already persist this provenance and `UNITS`; do not rewrite older
   `UNKNOWN` snapshots or historical trade evidence.

Discovery is dynamic, but execution evidence is **pinned** to the discovered exact
contract identities. A rollover invalidates that policy; it does not auto-renew the
attestation. There is no complete runnable session policy until real evidence is
available. This preparation intentionally supplies no fabricated runtime IDs/dates.

Primary sources checked:

- [MCX Crude Oil Mini options, March 2026 onward](https://www.mcxindia.com/docs/default-source/products/contract-specification/crudeoil-mini-options/crude-oil-mini-(10-barrels)-options-march-2026-contract-onwards.pdf)
- [MCX Natural Gas Mini options, March 2026 onward](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas-mini-option/natural-gas-mini-(250-mmbtu)-options-march-2026-contract-onwards.pdf)
- [Crude Oil Mini futures, 2026](https://www.mcxindia.com/docs/default-source/products/contract-specification/crude-oil-mini/crude-oil-mini-january-2026-contract-onwardsdbdf4c09-a735-4396-a2f5-091dad12f14f.pdf)
- [Natural Gas Mini futures, 2026](https://www.mcxindia.com/docs/default-source/products/contract-specification/natural-gas-mini-option/natural-gas-mini-january-2026-contract-onwards.pdf)
- [MCX normal trading hours](https://www.mcxindia.com/market-operations) and [2026 holiday calendar](https://www.mcxindia.com/market-operations/trading-surveillance/trading-holidays)

## Gross research is not blocked by margin/cost uncertainty

For this controlled paper proposal use `max_broker_margin=null`, `margin_evidence=[]`,
and no optional authenticated margin reader. Keep `all_in_cost_per_leg_execution=null`,
`cost_model=UNCONFIGURED`, and cost source/effective dates unset. Gross paper research
can then proceed through the existing risk controls with `MARGIN_UNVERIFIED`.
Net P&L, actual-capital usage and capital-efficiency/ROC metrics remain unavailable,
not zero. Do not substitute stress reservations for margin or invent a cost schedule.
No new authentication, broker orders or live margin feasibility claim is needed.

Keep the existing 15-second cadence, 10 observed strikes each side, 30-second maximum
quote age, 60-second pending-entry TTL and 20% entry bid/ask spread ceiling. Require
positive uncrossed tick-aligned prices, exact identities and one actual lot's depth:
10/250 underlying units. Selection/entry checks both sides; BUY exits require actual
ASK depth. OI/volume must be observed as required by the existing validator. Do not
rescale scores, manufacture missing values or force Alpha out of warmup.

## Pre-session and in-session acceptance checklist

- [ ] Existing commodity tables/migration 0021 available in the approved research
  environment; journal verification and restart recovery intact. No new migration.
- [ ] Read-only check of the existing commodity global control and frozen historical
  policies: proposed 14000/14000 budgets must not conflict with them. The guard can
  reject a changed global policy even if old trades are closed. Do not reset/delete
  a journal, edit its control row or bypass the guard to apply these proposals.
- [ ] Approve the trial budgets and obtain current exact-contract evidence above.
- [ ] Check normal-day calendar/session, clocks, data provenance, 15-second observations
  and actual BID/ASK source times before the first possible paper entry.
- [ ] Confirm all commodity activation flags remain OFF during preparation. Any later
  approval must be explicit: `COMMODITY_ACTS_PAPER_ENABLED`, each instrument enable,
  and `COMMODITY_ACTS_TRADING_ENABLED` are commodity **paper** switches only. Do not
  change any NIFTY/live flags. The existing CLI accepts only `--mode PAPER`.
- [ ] A new entry must have the native ACTS direction/qualification, a real OTM strike,
  sufficient fresh depth, a later eligible SELL BID, one lot and no overlap.
- [ ] Marks and exit BUYs use later observed ASK, with actual quantities/timestamps.
  Gross equals premium difference times 10/250. Unknown net/capital stays unavailable.
- [ ] Verify profit/stop/trailing/reversal/time/session/expiry requests against real
  events; no same-observation exit fill, synthetic price, expiry settlement or reset.
- [ ] Alert on stale/absent marks, unresolved exposure and overdue expiry exits. Missing
  books may prevent closing; never report flat/zero P&L without an actual paper fill.
- [ ] If aborting entries, retain capture for exits. Settings are startup-loaded:
  approved `TRADING_ENABLED=false` and policy kill switches require a controlled
  worker restart; disabling the capture/instrument master switches stops the worker.
  A killed worker is not a liquidated position.
- [ ] Review at most two entries per instrument, rejections, latency, recovery and
  exit evidence. Zero qualifying entries is valid; do not relax thresholds to create
  trades. No profitability, calibrated-tail-risk or deployment-readiness claim.

## Frontend compatibility — specified correction reconciled locally

The user supplied the exact deployed safety protections, so a missing deployment
branch/SHA is no longer a blocker to this correction. `dashboard.tsx` now uses
`Object.entries(signal.components ?? {})`, `(signal.reasons ?? [])` for both reason
accesses, and `latest?.features?.executable_book_coverage`. The surrounding JSX and
the existing Commodity ACTS components remain intact. Regression coverage includes
both null and omitted scalper components, reasons and features, plus the existing
NIFTY/commodity frontend tests. No unrelated NIFTY expiry or trade-cap change is
imported, and no backend, environment, strategy or risk setting is changed.

Proposal field constraints and normal-day session settings were checked against
current Pydantic models without a database or broker call. Exact contract evidence,
full runtime policy approval and existing-journal global-budget compatibility remain
pre-session requirements. Frontend validation does not validate live MCX quotes or
authorize deployment; no frontend container image was built or deployed.

Local compatibility validation after the correction: 53 tests passed across
`dashboard`, `commodity-acts`, `commodity-paper` and `route-isolation`; ESLint,
`tsc --noEmit` and `next build` passed. The production build includes `/`,
`/commodity-acts` and `/phase15`. All four effective local commodity activation
flags were confirmed false. Proposal field constraints, budget sums and normal-day
session settings passed isolated checks; no database or broker call was made.
The backend engine was not changed and its previously passing suites were not rerun.
