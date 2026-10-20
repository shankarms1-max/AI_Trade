# Commodity ACTS research V1

`COMMODITY_ACTS_PAPER_V1` is an isolated, opt-in, read-only-broker experiment for
exactly **CRUDEOILM** and **NATGASMINI**. It captures real MCX futures/options and
journals original ACTS V2 scores. PAPER means research observations and shadow
decisions here, **not paper spread fills**. There are no entries, positions,
exits, broker orders, P&L or profitability claims in this version.

## Architecture and isolation

```text
exact MCX master family -> selected future quote -> actual ATM strike neighborhood
                       -> batched option quotes -> immutable commodity capture
                       -> instrument-scoped original Alpha -> original ACTS V2
                       -> immutable SHADOW_DECISION -> read-only API / UI
```

- Each worker serves one instrument. Its specification contains the configured
  alias, instrument family, verified session and mandatory futures/options.
  Resolved broker tokens, symbols, expiries and lots are separate from aliases.
- No commodity data is converted into `MarketSnapshot`, `nifty_spot` or a NIFTY
  table. No commodity events appear in Phase 15 or parallel-paper journals.
- New tables are `commodity_market_snapshots`, `commodity_option_quotes`,
  `commodity_acts_cursors`, `commodity_acts_events`, `commodity_acts_decisions`.
  Instrument identity is part of semantic uniqueness, capture keys, quote keys,
  cursor ownership and hash chains. Equal 15-second buckets for both commodities
  cannot collide. Surrogate integer IDs are scoped to the new tables.
- Captures and their exact quote rows are immutable. Decisions, state, policy,
  capture hashes and per-instrument sequence/hash linkage are journaled together.
  Restart verifies the journal, state and decision projections, raw capture
  hashes and quote identities. Unprocessed captures are drained before newer
  decisions. Duplicate buckets retain the first capture; they are not repaired.
- The two-method SDK facade exposes only `search_scrip` and `quotes`. Adapter and
  service capability guards refuse objects exposing executable order methods.
  Worker/service have no broker order-adapter imports or trade-creation path.
- Existing NIFTY ACTS, parallel-paper, Phase 15, 400-point spread construction and
  MIRROR_EXIT_V1 implementations are untouched. Generic Alpha changes are an
  opt-in context-local clock and neutral future/reference accessors. Defaults
  retain NSE IDs, 09:15–15:30 clock and existing NIFTY field behavior.

## Market data and provenance

Installed `kotakneoapi` 3.0.7 (`neo_api_client`) supports MCX through
`search_scrip(exchange_segment="mcx_fo", symbol=<exact mini>,
ignore_50multiple=True)` and `quotes(..., quote_type="all")`.
Search is substring-based in the SDK; this adapter independently requires the
exact supported master family and exchange. The earliest nonexpired unique
future and earliest option expiry are selected separately. Ambiguous identities,
lot sizes or metadata fail closed. Non-derivative `COM` rows are excluded.

The option chain is assembled from resolved master contracts and batch quotes,
not the NIFTY option-chain method. ATM is the closest actual master strike to
future LTP (lower strike breaks a tie). Capture selects up to ten actual strikes
on either side, quotes at most 50 instruments per batch and paces requests.
Missing quote rows remain missing. Quote token/exchange and any returned trading
symbol must agree with the selected master identity.

Authoritative SDK/master evidence:

- [Official search interface](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/search_scrip.md)
- [Official SDK search implementation](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/services/scrip_search.py)
- [Official quote interface](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/quotes.md)
- [Official master interface](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/scrip_master.md)
- [MCX master expiry encoding](https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/instruments.md)

`pExpiryDate` is checked against MCX Unix `lExpiryDate` when supplied. The installed
SDK strike filter compares `dStrikePrice;` with requested strike times 100; the
captured mini symbols corroborated that scaling. Both raw strike and divisor
are stored. Runtime tokens and lots always come from the current master, never
the dated test fixtures below.

Only observed `ltp`, `open_int`, `last_volume`, depth prices/quantities and known
timestamp fields are used. Zero or absent prices do not become executable
prices. Missing quantities/OI/volume remain null; observed zero values remain
zero. No IV/Greeks, previous OI or quote values are synthesized. Derived OI
changes are causal feature evidence from exact identities, not replacements of
stored broker fields. Integer OI/volume/lot fields are parsed without a lossy
intermediate float conversion.

`reference_price` and `future_price` explicitly identify the same selected MCX
future. There is no commodity cash-feed requirement and no India VIX substitution.
The original ACTS context's internal `spot` dictionary key is a compatibility
input for its opening-structure arithmetic, explicitly sourced from this future;
it is not a cash-spot observation or a NIFTY model field. Commodity Alpha is
explicitly configured to use FUTURE identity, not SPOT identity.

`lstup_time` is passed through the existing documented numeric quote-timestamp
parser. Unsupported encodings remain null, and their small raw scalar is retained
for evidence after the existing sensitive-value redactor. A future-dated parsed
source timestamp is rejected. Known stale
future quotes fail capture; stale option books remain raw evidence but cannot
contribute usable book/credit diagnostics. Request start, response receipt,
capture persistence, feature calculation and decision timestamps remain distinct.

Depth quantity semantics are unconfirmed: every quote has `depth_unit=UNKNOWN`.
Raw quantities are retained without conversion. Master `dTickSize` is retained
as raw evidence, while normalized `tick_size` remains null until MCX scaling is
confirmed. Options and futures share the exact master underlying, but the
broker's precise option-to-future contract linkage is not yet confirmed.

### Safe live discovery on 2026-10-20

The following identities were actually returned in read-only discovery, not
guessed. They are dated observations, **not configuration or future guarantees**.

| Instrument | Selected future | Exchange token | Future expiry | Broker lot | Option expiry |
| --- | --- | --- | --- | --- | --- |
| CRUDEOILM | CRUDEOILM19NOV26FUT | 573423 | 2026-11-19 | 10 | 2026-11-17 |
| NATGASMINI | NATGASMINI27OCT26FUT | 570751 | 2026-10-27 | 250 | 2026-10-23 |

The sampled CRUDE ATM contracts were `CRUDEOILM17NOV269450CE` (583275) and
`CRUDEOILM17NOV269450PE` (583425), against future LTP 9453. NAT contracts were
`NATGASMINI23OCT26290CE` (581133) and `NATGASMINI23OCT26290PE` (581134), against
future LTP 291.9. Separate allowlisted master fixtures also preserve CRUDE 7900
CE/PE identities 582831/582832. These numbers occur only in dated evidence/tests.

**Live evidence blocker:** all six sampled quotes returned zero depth prices,
zero quantities and volume zero. Their `lstup_time` was:

```text
Mon Jan 01 1900 05:21:10 GMT+0521 (India Standard Time)
```

This sentinel is not converted to a real source timestamp. The adapter does not
repair it or substitute receipt time. These observations do not establish an
open-session API limitation: repeat verification during an independently verified
MCX session. Until nonzero usable books and valid source times are observed, these
quotes cannot support usable depth/credit or executable replay evidence. The
tests preserve this exact failure behavior using allowlisted fixtures, not raw
unfiltered broker objects.

## Session, causality and Alpha reuse

No MCX session hours are assumed. Per-instrument start, end, forced-exit time and
a nonempty verified source are required before an enabled worker connects.
Settings are IST wall times, with `start < forced_exit <= end`. V1 supports
same-day weekday sessions and an explicit holiday list; overnight sessions and
special weekend sessions require a future explicit calendar extension. Operators
must maintain exchange holiday/seasonal-hour changes. Forced-exit is recorded as
policy metadata only: V1 cannot hold or exit a position.

Session IDs are `MCX_CRUDEOILM_YYYY-MM-DD` and
`MCX_NATGASMINI_YYYY-MM-DD`. A thread/task-local scoped clock wraps the unchanged
Alpha implementation; scope is restored even on failure. NIFTY remains
`NSE_YYYY-MM-DD` by default.

The original Alpha mathematics, ranks, validity rules and thresholds are reused.
History is queried only for the same instrument and exact future token, symbol,
future expiry and option expiry, strictly earlier and already persisted at the
current observation time. A change in future token, symbol, future expiry or
option expiry discards cached Alpha/watch context, even within the same bucket.
Original option identity-continuity rules still apply.
The first captured observation in each 3-minute bucket feeds Alpha, matching its
existing cadence; Alpha is reused causally between buckets with its actual
calculation time and original freshness checks. It is not relabeled as a new
calculation every 15 seconds. Rank history may span eligible prior sessions
within original lookbacks; ACTS context horizons remain same-session. No broker
data is forward-filled. Instrument identity is checked again before evaluation.

ACTS context uses observed 1/3/5/15-minute reference endpoints, actual strike
neighborhoods and exact-identity quote/OI changes. Local PCR requires complete
paired regular observed grids. Wall clusters use one observed modal strike step,
not NIFTY's fixed point intervals. Missing history, opening coverage or baskets
remain unavailable rather than invented.

## Original ACTS V2 scoring, no execution policy

The original V2 scorer, weights, alpha factors and thresholds are reused, not
retuned. Bands remain `<40 NO_TRADE`, `40–54 WATCH`, `55–69 TRADEABLE`,
`70–84 STRONG`, `>=85 VERY_STRONG`. Missing components are disclosed. The
original unavailable-Alpha factor remains in effect; it is not silently replaced
or renormalized. Execution contribution is zero because no validated commodity
candidate exists. With the current original weights the maximum without those
execution components is 86/100, **not a rescaled 100**.

The original scorer intentionally reports `NO_EXECUTABLE_CANDIDATE` when there is
a directional thesis but no candidate. Consequently V1 normally records scores
and blocked decisions, not eligible ENTERs. This guard is not bypassed to inflate
opportunity counts. Even if an ENTER result is supplied, service handling only
appends a `SHADOW_DECISION`; there is no trade type or fill path.

Each decision contains the score/band, market state/direction, original component
and positioning/participation evidence, Alpha states, missing inputs, blockers,
policy hash, geometry and identities/timestamps. Original score behavior is
unchanged when inputs are missing. Cash spot and commodity VIX are explicitly
listed as unavailable, not substituted.

## Geometry and deterministic readiness

Persist sorted strikes, adjacent gaps, uniquely determinable modal step,
complete pairs, ATM-pair presence, bid/ask/depth/OI/volume/source-time coverage,
median/maximum relative bid/ask spread and every outward same-side observed
`short_bid - hedge_ask` comparison grouped by actual strike-index distance.
These comparisons are quote geometry, **not executable candidates or fills**;
they do not assume depth units, hedging viability, margin or profitability.
The full evidence is stored; API/UI show compact credit comparison summaries.

Capture failures are safely journaled as `FUTURE_UNRESOLVED`,
`OPTION_EXPIRY_UNRESOLVED`, `OPTION_CHAIN_UNAVAILABLE`, or `NOT_CAPTURED` with a
safe reason. For captured decisions the priority is:

1. `INSUFFICIENT_OPTION_PAIRS`: missing ATM pair or too few CE/PE pairs.
2. `QUOTE_DEPTH_INSUFFICIENT`: too few fresh, positive noncrossed books with
   positive bid/ask quantities. Units stay UNKNOWN even when quantities exist.
3. `ALPHA_WARMING_UP`: original Alpha validity is not yet VALID; not an error.
4. `READY_FOR_ACTS_RESEARCH`: sufficient research inputs, **not execution ready**.

Default minimum is three complete pairs; default quote age ceiling is 30 seconds.
Readiness is distinct from ACTS bands and the execution-disabled blocker. Collect
multiple sessions of actual strike spacing, spreads, depth coverage, expiry/lot
behavior and credit geometry before a separate reviewed commodity execution
policy is proposed. Never copy NIFTY's 400-point width into this module.

## Additive schema and controlled operation

Revision `0020_commodity_acts_research`, following `0019_parallel_paper`, adds only
the five commodity tables, their constraints/indexes, immutable-evidence triggers
and two empty commodity cursors. It neither updates nor alters existing NIFTY
tables or historical rows. PostgreSQL triggers also reject evidence TRUNCATE;
SQLite triggers reject UPDATE/DELETE. Downgrade removes commodity evidence and
must not be used as a casual rollback after collection.

After operator review, backup and independent deployment approval, run from the
repository root in the server's existing backend Python environment:

```bash
python -m alembic current
python -m alembic upgrade 0020_commodity_acts_research
```

No migration or server deployment was performed for this implementation. Migration
tests used temporary local SQLite databases plus offline PostgreSQL DDL. Workers
and APIs require the new schema; deploy the schema before using them. Commodity
worker processes are deliberately **not** added to production compose/services.

All three flags default false in code and `.env.example`:

```dotenv
COMMODITY_ACTS_PAPER_ENABLED=false
COMMODITY_ACTS_CRUDEOILM_ENABLED=false
COMMODITY_ACTS_NATGASMINI_ENABLED=false
```

For later controlled capture, explicitly enable the global flag and only each
reviewed instrument flag, and supply each enabled instrument's settings (values
below are placeholders, not assumed trading hours):

```dotenv
COMMODITY_ACTS_CRUDEOILM_SESSION_START=<verified IST HH:MM:SS>
COMMODITY_ACTS_CRUDEOILM_SESSION_END=<verified IST HH:MM:SS>
COMMODITY_ACTS_CRUDEOILM_FORCED_EXIT=<verified IST HH:MM:SS>
COMMODITY_ACTS_CRUDEOILM_SESSION_SOURCE=<authoritative source and verification date>
COMMODITY_ACTS_NATGASMINI_SESSION_START=<verified IST HH:MM:SS>
COMMODITY_ACTS_NATGASMINI_SESSION_END=<verified IST HH:MM:SS>
COMMODITY_ACTS_NATGASMINI_FORCED_EXIT=<verified IST HH:MM:SS>
COMMODITY_ACTS_NATGASMINI_SESSION_SOURCE=<authoritative source and verification date>
COMMODITY_ACTS_HOLIDAYS=<comma-separated verified YYYY-MM-DD dates, or blank>
```

Use existing `DATABASE_URL` and the existing consumer-key read-only market-data
configuration; never print either. This module introduces no trading credentials
or order flags. Changing the research configuration after journaled decisions
fails closed on policy-hash mismatch rather than silently mixing experiments.

Read-only status, one selected instrument per command:

```bash
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM --status
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI --status
```

Only after explicit deployment/configuration approval, one process per instrument:

```bash
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument CRUDEOILM
python worker/commodity_acts_paper_worker.py --mode PAPER --instrument NATGASMINI
```

`--once` performs at most one in-session capture/research pass. Disabled workers
exit before opening DB/broker connections. Enabled workers refuse missing verified
session settings. LIVE is not a valid mode. Capture runs every 15 seconds at
seconds 3/18/33/48 for CRUDE and 7/22/37/52 for NAT, with one in-flight job,
coalescing and session gating. Offsets reduce contention; instrument keys and DB
locking supply correctness. There is no synthesized catch-up market data.

Read-only surfaces:

```text
GET /api/commodity-acts/status
GET /api/commodity-acts/summary
GET /api/commodity-acts/decisions?instrument=CRUDEOILM&limit=50&offset=0
GET /api/commodity-acts/decisions?instrument=NATGASMINI&limit=50&offset=0
/commodity-acts
```

The UI separates instruments and is labeled RESEARCH / PAPER ONLY, NO BROKER
ORDERS and NO P&L YET. Flags report configured intent, not inferred process
liveness; worker process state is explicitly unavailable without a heartbeat.
No broker call or research write is performed by the API or status command.

## Remaining controls before collecting useful evidence

- Verify current MCX mini contract metadata, trading calendar and per-instrument
  session settings with an authoritative source; no session is preconfigured.
- Repeat live quote checks during that session: the discovery sample had zero
  books and invalid source times. Do not equate captured LTP with usable depth.
- Confirm depth quantity units, normalized tick scaling and option/future linkage
  before designing any future executable policy. Preserve raw evidence meanwhile.
- Review actual 15-second capture latency/rate limits alongside existing workers.
  A late job is coalesced rather than inventing observations.
- New PostgreSQL DDL was compiled, but live PostgreSQL application remains an
  operator-controlled deployment step. Existing optional PostgreSQL integration
  tests require a separately configured disposable test database.
- This release is suitable for a gated capture/score research deployment after
  migration/session review, not commodity trading or spread paper execution.

## Changed-file manifest

Validation on 2026-10-20: full backend **1,444 passed / 8 skipped** (the optional
PostgreSQL integrations need `PHASE15_POSTGRES_TEST_URL`); commodity-only focused
tests **49 passed**; frontend **65 passed**; lint, TypeScript, production build
and `git diff --check` passed. The build includes `/commodity-acts`, `/phase15`
and `/`. Backend emitted the existing Starlette/httpx deprecation warning.
Full backend command: `python -m pytest backend/tests --tb=short --durations=10`.
The test process used `PYTHON_DOTENV_DISABLED=1` to prevent Alembic's dotenv loader
from importing local integration flags into tests; no environment file was edited.

```text
.env.example
alembic/versions/0020_commodity_acts_research.py
backend/app/alpha/clock.py
backend/app/alpha/price_alpha.py
backend/app/api/commodity_acts.py
backend/app/commodity_acts/__init__.py
backend/app/commodity_acts/broker.py
backend/app/commodity_acts/config.py
backend/app/commodity_acts/context.py
backend/app/commodity_acts/domain.py
backend/app/commodity_acts/geometry.py
backend/app/commodity_acts/models.py
backend/app/commodity_acts/read.py
backend/app/commodity_acts/repository.py
backend/app/commodity_acts/service.py
backend/app/db/models.py
backend/app/main.py
backend/tests/fixtures/mcx_master_safe_20261020.json
backend/tests/fixtures/mcx_quotes_safe_20261020.json
backend/tests/test_commodity_acts.py
backend/tests/test_commodity_acts_controls.py
backend/tests/test_commodity_acts_migration.py
backend/tests/test_parallel_paper_migration.py
backend/tests/test_scalper_migration.py
docs/commodity-acts-paper-v1.md
frontend/app/commodity-acts/page.tsx
frontend/app/globals.css
frontend/components/commodity-acts.tsx
frontend/components/navigation.tsx
frontend/lib/api.ts
frontend/tests/commodity-acts.test.tsx
frontend/types/commodity-acts.ts
worker/commodity_acts_paper_worker.py
```

The two existing migration tests received only revision bookkeeping: the parallel
test is pinned to the revision it tests, and the optional PostgreSQL full-upgrade
test expects the new head. No existing NIFTY strategy test or assertion was
removed or relaxed.
