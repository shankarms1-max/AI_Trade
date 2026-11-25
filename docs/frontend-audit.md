# Frontend and financial-data audit

Audited commit: `37f27af` (consolidated MCX/ACTS release; previously `e715d77` in production). Method: the real
frontend + real backend were run locally (production build, same-origin proxy mimicking Caddy routing) against a
database seeded with the **real NIFTY ACTS journal** from the read-only production extract of 2026-11-24 (153
trades, 13,973 journal events, including the genuine expired open position) plus synthetic MCX rows. The browser
audit is `frontend/scripts/audit-browser.mjs`. **No production browsing was possible** (no authorised access in this
session); production conclusions below are marked as such.

## Coverage

| Check | Result |
|---|---|
| Routes audited | 21 (every `page.tsx` + sample dynamic params + a non-existent path) |
| Route x viewport runs | 105 (1920x1080, 1440x900, 1280x800, 768x1024, 390x844) |
| Sidebar links clicked | 15 of 15 land on their route (an early "failure" was my script reading the URL before Next's client navigation finished; fixed, not a defect) |
| Back / forward / reload | pass (`/` -> `/positions` -> `/trades`, back, back, forward, reload) |
| Page-level horizontal overflow | 0 of 105 runs |
| Interactive controls found | 2 on `/`, 2 on `/nifty`, 2 pagination buttons on `/trades`, none elsewhere |
| Not testable | production site; AST data (isolated stack, unreachable); live broker feed (market closed during the audit) |

## Confirmed findings

| ID | Sev | Where | Finding and reproduction | Root cause | Impact | Status |
|---|---|---|---|---|---|---|
| F-01 | **P1** | Overview `/`, `/nifty`, `/phase15`; `GET /api/phase15/summary` | Open position from the previous session: summary reports **Unrealized -104.00** while `GET /api/phase15/positions` reports `mark_available:false` for the same trade. Seen in your production screenshot of 24 Nov 17:30 IST (Unrealized -₹104.00 beside "Gross mark N/A / MARK UNAVAILABLE") and reproduced locally. | `parallel_paper/monitor.py` builds `failed_marks` from **today's** events only (`_summary_events ... .where(today)`); a trade with no event today is absent from the dict, so `mark_available` defaulted to true and the stored 23-Nov mark was summed. `trade_monitor._project` already rejected stale marks. | A stale price shown as live P&L. | **FIXED** (`monitor.py`, narrowed so a mark-less trade entered today keeps its previous behaviour). Regression test `test_open_position_carried_over_from_a_previous_session_does_not_count_its_stale_mark` verified to fail without the fix (`-104.0 is None`) and pass with it. |
| F-02 | **P1** | Overview, header, `/api/phase15/summary` | The open bear call spread 25600/26000 CE **expired 23 Nov** and was never closed, yet the UI shows `Unresolved 0`, system badge IDLE / MARKET_CLOSED, and no alert. | Legacy summary counts only `state == UNRESOLVED_EXPOSURE`; nothing compares an OPEN trade's contract expiry or the 15:20 forced-exit time with the clock. | Expired exposure invisible; operator relies on a stale -104. | **FIXED in the new read model**: `EXPIRED_NOT_SETTLED` and `PAST_FORCED_EXIT` flags, CRITICAL alert, header alert count, excluded from totals with `pnl.partial=true` and an explicit count. The legacy endpoint is unchanged on purpose (existing contract) and is no longer used by the new Overview/Positions. |
| F-03 | **P1** | Worker `mcx_futures_paper_worker.py` in the consolidated release `37f27af` | `MCX_FUTURES_TRADING_ENABLED` defaults to **false**. The running production workers never set it (their `e715d77` image does not read it), so swapping the image under the same command would have **silently turned both paper-trading workers into collectors**. | Default encoded the opposite of the deployed intent; the explicit `MCX_FUTURES_PAPER_ENABLED` flag already means "take paper entries". | Paper trading would stop with no error. | **FIXED**: collect-only is now an explicit opt-out (`MCX_FUTURES_TRADING_ENABLED=false`). Test `test_paper_entries_are_enabled_by_default_and_collect_only_is_an_explicit_opt_out`. |
| F-04 | P1 | Information architecture | Overview showed one gross total and an engine list: no strategy table, no intraday P&L, no orders; **no Orders, Analytics or Risk page exist**; 15 flat sidebar links mix live and retired engines. | Pages were added per engine over time. | Cannot answer "what is my P&L / what is wrong" in five seconds. | **REDESIGNED** (see terminal pages; verification below). |
| F-05 | P2 | `/commodity-acts/CRUDEOILM`, `/NATGASMINI`, `/system`, `/nifty/research` | Each renders 24 / 24 / 40 / 23 `N/A` values and 15 / 15 / 10 / 7 UNAVAILABLE-style labels. The legacy Commodity ACTS **options** engine has no deployed worker, so the pages read as "commodity trading is broken" while the futures engine runs. | Pages for an inactive engine kept as primary navigation. | Misleading operational picture. | **FIXED**: moved under More / Legacy; the Commodity overview shows the live MCX futures first (commit `f586e2c`, Codex). |
| F-06 | P2 | All data pages | At most 2 controls per page; no filters, tabs, drawers, sorting or export. | Read-only status cards only. | Cannot inspect a position, order or trade. | **REDESIGNED**. |
| F-07 | P2 | Production deployment | The deployed backend returned 404 for `/api/mcx-futures/*` and `/api/engines/status`; frontend was an older image. | Images predated the MCX release. | MCX trades invisible. | Fixed on 24 Nov by deploying `e715d77`; superseded by the next release. |
| F-08 | P3 | `/mcx-futures/NIFTY` (unknown instrument) | React hydration error #418 in 5 of 5 viewports; page correctly 404s. `/does-not-exist` has none. | Next 16 `dynamicParams=false` + `notFound()` path. | Console noise only. | Not fixed; no data impact. |
| F-09 | **P0** | Production deploy 25 Nov, MCX workers | After Block 3 both `mcx-futures-*` containers ran Uvicorn (the image default CMD) instead of `mcx_futures_paper_worker.py`: journal sequences stuck at 567 / 346, `LEGACY_MODE_UNVERIFIED`, CPU 103%. | The deployment script's `swap()` recreated containers without carrying over the old container's command. Its healthcheck passed vacuously because MCX was still closed at 08:55 IST (`MARKET_CLOSED` counts as healthy). | No MCX collection or paper trading from about 08:55 IST until corrected. Flat, so no position affected. | Corrected command-preserving recovery block (rehearsed with real Docker incl. rollback); `swap()` now copies the command; verification now fails on non-worker commands and on a journal that does not advance. |
| F-10 | **P0** | `/api/portfolio/summary`, alerts, header | The worker terminalised the expired NIFTY 25600/26000 spread as `EXPIRED_UNPRICED`; the summary then reported `unresolved_positions=0`, `partial=false`, no alert. Only the Positions "unresolved" tab showed it. | `summary()` considered only status `CLOSED` and `OPEN/UNRESOLVED`; `EXPIRED_UNPRICED` was in neither list, so a position with UNKNOWN P&L vanished from totals, counts and alerts. | Unknown exposure silently omitted. | **FIXED**: such positions are flagged `EXPIRED_UNPRICED`, counted in `unresolved_positions` and `counts.expired_unpriced`, listed in `attention`, raise CRITICAL `EXPIRED_POSITION_UNPRICED`, set `partial=true` with `excluded_unpriced_exposure`, in EVERY date window until reconciled. The position itself is untouched. Tests added. |
| F-11 | P1 | Costs | Engine-recorded net for the first CRUDEOILM trade (369.81) differed from the portfolio net (417.01) by 47.20. | Trace: both use identical statutory charges (12.99). The engine's journal used a Rs 20/order brokerage ASSUMPTION (2 x 20 = 40) plus 18% GST on it (7.20) = 47.20; the portfolio used Rs 0. Kotak's Trade-API page publishes no brokerage on API orders (https://www.kotakneo.com/trade-api/); the MCX futures transaction charge 0.0021% matches the engine's rate. | Engine net understated vs actual cost. | Engine default now Rs 0 (schedule V2, status `PUBLISHED_SCHEDULE_ACCOUNT_PLAN_UNCONFIRMED`) for NEW trades only; historical journal records are unchanged and still carry the V1 Rs 20 assumption. The verifier proves the statutory parts agree per trade. The account plan itself is still unconfirmed. |
| F-12 | P1 | Charges and counts | Summary `incurred_charges` 2,362.94 vs analytics `charges` 2,358.70; summary 130 closed trades vs Positions 131. | Different scopes, not an arithmetic error: incurred = entry charges of positions ENTERED in the window + exit charges of positions CLOSED in it (it therefore includes the 4.24 entry charges of the unpriced expired spread); analytics = gross - net of closed priced trades. Positions "closed" = 130 priced CLOSED + 1 EXPIRED_UNPRICED. | Confusing but each figure was correct for its definition. | Scopes are now explicit in the API: `charges_on_closed_trades`, `charges_not_in_closed_trades`, `incurred_charges_scope`, `counts.expired_unpriced`, `tab_counts.closed_priced / expired_unpriced`, analytics `charges_scope`. |
| F-13 | P1 | Header freshness, MCX session | `fresh_feeds=1, stale_feeds=0, feeds=3` while both MCX feeds were `DATA_UNAVAILABLE`; MCX session showed last-entry / forced-exit times of the PREVIOUS day. | Only `DATA_STALE` was counted as stale (unavailable fell through); the session block was the worker's persisted copy from the last observation. | Header said connected while two engines were not. | **FIXED**: each active feed is classified fresh / stale / unavailable / market_closed exactly once; connectivity is `PARTIAL_INFERRED` when only some in-session feeds are fresh; `session` is calendar-derived for now and the persisted copy is `last_observed_session` flagged historical. |

## Verified NOT defects (checked and discarded)

| Observation | Why it is not a bug |
|---|---|
| `/api/phase15/positions` returned an empty list on the first audit run | The query inner-joins `scalper_market_snapshots`; my audit database had no snapshot rows. After seeding them the position appears. Production has them. |
| AST page showed "HTTP 401 AUTHENTICATION_REQUIRED" | My local proxy added no ingress `Authorization: Basic` header. With the header (as Caddy supplies) the bridge returns 503 `SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED`, which the page renders as "isolated stack, not configured". |
| 404s on `/history/1`, `/shadow/1` | No such record in the audit database. |

## Financial data trace (source of truth per metric)

| Metric | Authoritative source | New read-model definition | Verification |
|---|---|---|---|
| Positions / legs | `acts_paper_trades.document`, `mcx_futures_trades.document` | one parent per trade, legs nested, never double counted | unit tests; unique ids |
| Orders / fills | journal lifecycle fields (`decision_*`, `entry_*`, `exit_*`, events) | **simulated** per-leg orders; broker order id always null; fills only for FILLED orders | unit tests |
| Gross realized | recorded fills | `(short_entry - hedge_entry - short_exit + hedge_exit) x qty`; futures `(exit - entry) x sign x qty` | recomputed gross equals the engine's record for **129 of 129** closed NIFTY ACTS trades (0 discrepancies) |
| Charges | dated model, `portfolio/charges.py` | STT/CTT, exchange, SEBI, stamp, GST by date; brokerage is a configured scenario | hand-computed examples in tests |
| Net realized | gross - charges | as above | reproduces my independent earlier calculation exactly: V3 6,782.75 / 6,178.77 and V2 15,102.75 / 7,272.22 at Rs 10/order, 6,650.77 / 12,889.02 at Rs 0 |
| Unrealized | latest mark event | only a **fresh (<= 60 s) executable** mark counts; stale, unavailable and expired marks are excluded and counted, never summed as zero | tests incl. the expired position |
| Margin / capital | not recorded | unavailable, with reason; futures notional is labelled notional, never margin | tests |

Known limitations: brokerage is **not verified** (Kotak's published Trade-API schedule says Rs 0 per order but the
account plan could not be confirmed; reported as `UNVERIFIED_ASSUMPTION`); the MCX exchange transaction charge is
an unverified assumption; ACTS records no costs of its own (GROSS_ONLY), so every ACTS net figure is model-derived;
the MCX engine records its own net using a Rs 20/order assumption, which is shown next to the portfolio net
(`engine_recorded_net`) because the two use different brokerage assumptions.

## Verification after the redesign

Same method and data as the audit (production build, same-origin proxy, real NIFTY journal + synthetic MCX rows).
Before/after screenshots: `docs/screenshots/` (1440x900 and 390x844; see its README for what is real and synthetic).

| Check | Result |
|---|---|
| Frontend unit/component tests | 444 passed (37 files) + clean `tsc --noEmit` |
| Backend suite | 2,313 passed, 72 skipped, 0 failed (after the 25 Nov fixes) |
| Portfolio-layer tests | 32 passed, including expired-position, stale-mark, partial-total and no-double-count cases |
| Cross-page financial consistency (`frontend/scripts/e2e-consistency.mjs`) | 40 of 40 checks pass: the realized net, realized gross and closed-trade count shown on Overview, Tradebook, Analytics, Positions (closed tab) and the CSV export equal the `/api/portfolio/summary` values for 5 filter windows (including an empty one) |
| Viewports | 1920x1080, 1440x900, 1280x800, 768x1024, 390x844: 0 page-level horizontal overflow on the terminal pages (a 390px overflow on `/strategies/mcx-natgasmini` was found, root-caused to a nowrap identifier chip and fixed; 0 overflow in 70 subsequent runs) |
| Console errors on terminal pages | 0 |
| Legacy deep links | preserved (grouped under More / Legacy); F-08 (hydration #418 on an unknown MCX instrument, 404 page) is unchanged and cosmetic |
| Production browser verification | **NOT DONE**: no authorised production access in this session. Procedure in `docs/aws-execution-procedure.md`, Block 4 prints RECONCILE PASS/FAIL from inside the backend container |

Residual limitations (unchanged): brokerage and the MCX exchange charge rate are unverified assumptions; ACTS records
no costs, so ACTS net is model-derived; margin/capital is not recorded and is shown as unavailable; AST data comes from
an isolated host and was not reachable.
