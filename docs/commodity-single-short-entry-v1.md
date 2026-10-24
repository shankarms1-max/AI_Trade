# Commodity single-short entry integrity

Base: `release/nifty-commodity-responsive-v3` at `573dc702ed59757c197f4c55d2151addbb8f5873`.
Fix branch: `fix/commodity-single-short-entry-v1`. PAPER only; no deployment or broker requests.

## Root cause

The former `paper_naked.decide` constructed a `SimpleNamespace` with one option's
book in both the short and long roles. It passed premium divided by estimated
stress-close premium as `credit_to_width`. The unchanged NIFTY scorer interpreted
these as two spread books and a spread-width return. Its execution component
could supply up to 14 of the 100 points under the default weights, although a
single short has no hedge, width or defined maximum loss. Short ranking also used
a generic blended quality function with OI/volume minima of zero, yielding
liquidity points without a meaningful evidence-backed eligibility floor. The
research stage's SCORE_TRANSFERABILITY label did not describe the automated path.

## Explicit separation

`COMMODITY_ACTS_DIRECTIONAL_CONTEXT_V1` calls the existing ACTS V2 context evaluator
with **no candidate**. Momentum, market state, watch/resumption, Alpha factors,
positioning, participation and opening/session structure use the original
functions and configuration. Only the old executor's missing-spread blocker is
removed from this context-only result. Other hard blockers and the configured
strong-opposite Alpha veto remain.

Execution supplies no directional points. The context components have 86 possible
raw points with the existing default weights. The separately named directional
score is `100 * raw_context_points / sum(context_weights)`. Raw components and
maximum remain persisted. Existing configured percentage bands 40/55/70/85 apply
to this context score. This deterministic normalization removes the obsolete
14-point execution allocation without rewarding a manufactured book or inventing
new numeric thresholds. It is **not** the original ACTS V2 opportunity score.
NIFTY's original 100-point scorer and its bands are untouched.

`COMMODITY_SINGLE_SHORT_ENTRY_V1` requires a qualifying direction plus native
single-option eligibility and all existing engine risk/session gates. BULL selects
OTM PE only; BEAR selects OTM CE only. The candidate stores a single short and
observed OTM distance/strike steps, two-sided book, unit depth, source age, OI,
volume, premium, expiry/DTE, dated future identity and operator stress scenario.
Premium in INR divided by estimated stress loss in INR is a diagnostic explicitly
named `premium_to_stress_loss_estimate`. It is never a credit-to-width ratio or a
loss bound. Zero scenario loss produces an unavailable ratio, not infinity.

No synthetic long leg, width, hedge quality or defined maximum loss is built.
The legacy spread reader/manager remains for actual historical spread documents;
new spread entries remain disabled as before.

## Configuration and provenance

All existing dated execution policy limits still apply: maximum bid/ask spread,
actual one-lot unit depth, 30-second configured source age, contract/expiry
identity, minimum premium, immediate unwind/cash stop, stress ceiling, margin if
configured, entry TTL, market/session/pre-expiry cutoffs and portfolio/session
risk reservations. No monetary, lifecycle or session default is changed here.
Approved CRUDE/NAT cash triggers 2500/1500, stress ceilings 6000/8000, daily limits
6000/8000, one lot/one position/two entries and global two positions/14000 stress
and daily ceilings remain runtime operator configuration, not newly invented
constants. Existing 50% profit, 2x stop, 25%/15% trailing, 30-minute holding,
opposite-entry signal and configured 22:30 exit mechanisms remain.

`COMMODITY_ACTS_SINGLE_SHORT_ENTRY_POLICIES_JSON` is a separate per-instrument map:

```json
{"CRUDEOILM": {}, "NATGASMINI": {}}
```

Supported optional positive floors: `min_open_interest`, `min_volume`,
`min_otm_observed_steps`, `min_premium_to_stress_loss`. No supplied or approved
Commodity fixture establishes a defensible positive numeric cutoff for these.
Unset floors are therefore **DIAGNOSTIC_ONLY**, never silently zero-valued or
liquidity score rewards. Existing malformed/missing OI/volume rejection is retained
by the original book validator. Explicit positive configured floors become real
eligibility gates and are rechecked on the subsequent fill observation. They do
not prevent a risk-reducing BUY exit. Unknown fields, wrong versions, zero,
negative and nonfinite thresholds are rejected.

Eligible candidates are ordered lexicographically by observed spread percent
ascending, stress-loss estimate ascending, OTM distance descending, premium
descending, then stable candidate identity. This is transparent native ordering,
not a fitted blended quality score. The ordinal ranking is only an ordering, not
an entry score. Selected, eligible-not-selected and rejected contracts have
persisted diagnostic evidence and explicit reasons.

## Execution, journals and compatibility

New decisions require actual native candidate eligibility plus independent global
risk/session gates. A direction mismatch cannot qualify the opposite option side.
Entry still SELLS at a **later observed BID**; mark/exit still BUYS at subsequent
observed ASK. No LTP fallback, partial/synthetic fill, same-observation fill,
pricing, cost model or exit threshold changes. New entry floors are carried with
the candidate and checked again at fill time.

Risk policy documents/hashes retain their original schema and values, including
historical hashes. New trades additionally freeze the versioned native entry
policy and an entry-policy hash covering native policy plus direction parameters.
Future entry configuration does not rewrite an open trade's stored policy.
Research cursors accept only their exact known predecessor hash or new hash;
they append the transition in subsequent evidence. Arbitrary policy changes still
fail closed. Historical snapshots, decisions, orders, fills, positions and events
are never migrated or rewritten by this patch.

Read-only APIs already project persisted JSON; no new write route is added.
The existing Commodity frontend separates directional score/state, native
qualification/components, candidate/rejection evidence and paper lifecycle. Trade
rows display their **frozen** entry qualification rather than latest context.
Historical opportunity scores remain labelled legacy compatibility scores; new
qualification is never labelled ACTS V2 execution score. Missing values stay N/A.

## Historical diagnostic and evaluation limits

No actual October 23 production Commodity trade exports or database records were
available in this release worktree. Existing MCX fixtures and the paper-test
journals are synthetic (2099), not yesterday's trading evidence. No production
access or outcome-based fitting was performed.

Read an already exported local JSON array (or API `items` object) without a DB or
broker connection:

```sh
python scripts/diagnose_commodity_single_short.py --trades-file /path/to/positions.json
```

The diagnostic prints recorded direction, option, distance, entry bid/ask/depth,
Alpha/positioning, legacy compatibility score when present, stored native
qualification, reasons and gross/net P&L. Legacy data lacking causal inputs is
explicitly NOT reconstructed or retrospectively declared eligible. Synthetic
closed-trade regression verifies that only recorded values are reported.

Trade frequency is expected to change because candidate ordering and the meaning
of qualification are corrected. Context no longer varies with option premiums or
fake spread-quality points. Positive OI/volume/ratio/distance gates are not guessed,
so the default does not add an unsupported no-trade filter. BULL/BEAR scenarios
for both instruments still qualify, enter on a later book and complete normal
paper exits in regression tests. A reasonable 30–40-session opportunity count or
profitability cannot be claimed from synthetic data or one day of P&L; evaluate
prospectively with the versioned evidence before making that claim.

## Validation

Final commands/results and exact changed-file inventory are recorded with the
fix validation evidence. All non-Commodity source, NIFTY workers, migrations,
Dockerfiles, Compose and production environment files must match the base bytes.

Final validation:

- Full backend: **2018 passed**, including **293 Commodity tests**; zero failures/skips.
- PostgreSQL 16 focused Commodity run: **290 passed**; final native-entry module: **30 passed**.
  The three additional native/API assertions added after focused collection also pass in the full suite.
- Frontend: **146 passed**. ESLint, TypeScript and production build PASS.
- Mypy: **7 affected source modules PASS**; flake8 all affected Python PASS.
- Diff check PASS; **336 protected source files byte-identical** to the base.
- PostgreSQL and all fills/quotes in regression are isolated disposable/synthetic evidence, not production.
- No production access, deployment, live data request or broker order occurred.

Exact changed files:

- `.env.example`
- `backend/app/commodity_acts/config.py`
- `backend/app/commodity_acts/entry.py`
- `backend/app/commodity_acts/paper_engine.py`
- `backend/app/commodity_acts/paper_naked.py`
- `backend/app/commodity_acts/read.py`
- `backend/app/commodity_acts/service.py`
- `backend/tests/test_commodity_acts.py`
- `backend/tests/test_commodity_acts_controls.py`
- `backend/tests/test_commodity_acts_timing.py`
- `backend/tests/test_commodity_entry.py`
- `backend/tests/test_commodity_paper.py`
- `backend/tests/test_commodity_paper_controls.py`
- `backend/tests/test_commodity_paper_naked.py`
- `docs/commodity-single-short-entry-v1.md`
- `frontend/app/commodity-acts/page.tsx`
- `frontend/components/commodity-acts.tsx`
- `frontend/components/commodity-paper.tsx`
- `frontend/tests/commodity-acts.test.tsx`
- `frontend/tests/commodity-paper.test.tsx`
- `frontend/types/commodity-acts.ts`
- `frontend/types/commodity-paper.ts`
- `scripts/diagnose_commodity_single_short.py`
