# Nifty AI Credit Spread Research

Phase 2 is a research-only recorder that fetches normalized NIFTY snapshots from
Kotak Neo and atomically stores them in PostgreSQL. It records raw normalized broker
values every three minutes during configured NSE hours. It does not calculate
features, infer missing values, select strategies, or place orders.

> This repository contains no paper-trading, live-trading, or order API code.

## What is recorded

Each `market_snapshots` row contains the IST observation time, NIFTY spot, optional
NIFTY future, optional India VIX, ATM strike, expiry, and source. Its related
`option_contract_snapshots` rows preserve prices, broker-provided OI fields, volume,
and nullable IV/bid/ask/Greeks exactly as represented by the Phase 1 normalized
model. `collector_runs` records STARTED/SUCCESS/FAILED outcomes without credentials
or raw authenticated responses.

One snapshot and all its contracts commit in one transaction. A unique
`collection_bucket_ist` (the observation time floored to the configured interval)
prevents repeated or racing invocations from duplicating a contract set. Prices or
OI are deliberately not used for duplicate detection.

## Setup

Python 3.12 or newer and Docker with Compose are recommended. From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit the ignored `.env`. Set `KOTAK_CONSUMER_KEY` and use a non-example database
password outside local development. Never commit `.env`.

Relevant settings are:

```dotenv
DATABASE_URL=<set the PostgreSQL URL from .env.example>
COLLECTOR_INTERVAL_MINUTES=3
COLLECTOR_START_TIME=09:18
COLLECTOR_END_TIME=15:27
TZ=Asia/Kolkata
COLLECTOR_HOLIDAYS=2026-01-26,2026-03-03
```

`COLLECTOR_HOLIDAYS` is an optional comma-separated ISO-date list. Weekends are
always excluded. This is intentionally an abstraction rather than an external
holiday service, so operators must configure exchange holidays.

## PostgreSQL and migrations

Start the local database and apply the versioned schema:

```powershell
docker compose up -d postgres
alembic upgrade head
```

Docker uses the named `postgres_data` volume. Production schema creation uses
Alembic; the application does not call `create_all()`.

The schema has these tables:

- `market_snapshots`: one row per unique collection bucket, indexed by timestamp
  and expiry.
- `option_contract_snapshots`: contracts linked with a cascading foreign key and a
  unique `(market_snapshot_id, expiry, strike, option_type)` safeguard.
- `collector_runs`: attempt status, counts, optional snapshot link, and sanitized
  error metadata.
- `market_feature_snapshots` and `market_regime_snapshots`: versioned deterministic
  Phase 3 and Phase 4 research outputs.
- `ai_research_snapshots`: versioned, on-demand Phase 5 AI research plus safe
  provider, latency, token, cost, and failure metadata.
- `strategy_candidate_sets`: versioned Phase 6 hypothetical defined-risk candidates.
- `risk_decisions`: one versioned Phase 7 hard-risk decision per candidate fingerprint.
- `shadow_trades` and `shadow_trade_marks`: Phase 8 entries, lifecycle state, and
  the complete observed-price P&L path.

No retention deletion runs automatically.

## Run

Run all offline tests (none contact Kotak):

```powershell
pytest
```

Fetch and persist one live snapshot regardless of the scheduler window:

```powershell
python scripts/record_nifty_snapshot.py
```

Start the continuous collector:

```powershell
python worker/collector.py
```

The worker uses IST, runs only from 09:18 through 15:27 inclusive on eligible
weekdays, and uses both an in-process lock and APScheduler `max_instances=1` to
prevent overlap. A failed interval is safely recorded when the database is
available; the scheduler continues to the next interval.

Start the read-only API:

```powershell
uvicorn app.main:app --app-dir backend --reload
```

Endpoints:

- `GET /health`
- `GET /api/snapshots/latest`
- `GET /api/snapshots?limit=50`
- `GET /api/snapshots/{id}` (includes option contracts)

## Phase 3 — deterministic feature engine

Phase 3 reads persisted raw snapshots and writes reproducible `phase3_v1` research
features to `market_feature_snapshots`. Raw snapshot and option rows are never
modified. Rebuilding the same `(market_snapshot_id, feature_version)` updates the
existing derived row instead of creating a duplicate.

Apply the Phase 3 migration, then build features:

```powershell
alembic upgrade head
python scripts/build_features.py --latest
python scripts/build_features.py --snapshot-id 123
python scripts/build_features.py --all
```

Backfill processes raw snapshots chronologically and makes no broker API calls.
Feature endpoints are:

- `GET /api/features/latest`
- `GET /api/features?limit=50`
- `GET /api/features/{snapshot_id}`

### Formulas and terminology

- `local_pcr_oi = sum(stored-window put OI) / sum(stored-window call OI)`.
- `local_pcr_oi_change` uses broker-provided OI change only, is gated by data
  quality, and is null when unavailable or its call-change denominator is zero.
- Top-1/3/5 concentration is top-N side OI divided by side OI in the captured
  window. Weighted strike is `sum(strike × OI) / sum(OI)`.
- Clusters select side strikes whose metric is at least the configurable fraction
  of that side's maximum, then merge adjacent strikes using the inferred minimum
  strike spacing. Strength is `0.5 × cluster share + 0.3 × relative maximum +
  0.2 × proximity`, where proximity is `1 / (1 + distance / strike step)`.
- Futures basis is `future - spot`; basis percent is `basis / spot × 100`.
- Configurable VIX research defaults are LOW below 12, NORMAL below 16, ELEVATED
  below 22, and HIGH from 22 onward.
- Contract positioning requires prior LTP for the same expiry/strike/type plus the
  broker OI change: price/OI up is LONG_BUILDUP; price down/OI up is
  SHORT_BUILDUP; both down is LONG_UNWINDING; price up/OI down is SHORT_COVERING.
- The observed opening range uses only collector observations from the configured
  window. It is complete only with the configured sample count and observations
  covering both boundaries.

PCR covers the persisted ATM window, not the full NSE chain. OI addition alone does
not prove writing, and positioning labels are contract-level heuristics. Potential
support/resistance clusters are research features, not trade signals. Session
open/high/low fields are collector-observed values, not official exchange OHLC.
VWAP remains unavailable because option volume is not NIFTY underlying volume.
Intraday OI interpretation remains pending market-hours validation.

## Phase 4 — deterministic market-regime engine

Phase 4 classifies each persisted `phase3_v1` feature snapshot as `BULLISH`,
`BEARISH`, `RANGE`, or `NO_TRADE`. **This is a research classifier, not a
trading signal.** It does not construct strategies, select strikes, or place orders.

Build or backfill versioned `phase4_v1` results with no broker calls:

```powershell
alembic upgrade head
python scripts/build_regime.py --latest
python scripts/build_regime.py --snapshot-id 123
python scripts/build_regime.py --all
```

Read-only endpoints are `GET /api/regime/latest`, `GET /api/regime/{snapshot_id}`,
and `GET /api/regime?limit=50`.

### Signal groups and weights

Every group returns a direction, normalized strength, stable reason codes, and
details. Default maximum weights are:

| Group | Weight | Role |
|---|---:|---|
| Price structure | 3 | Prior move, observed opening range, collector session structure |
| Dynamic OI | 4 | Broker OI additions/reductions; unavailable when intraday OI is unusable |
| Option positioning | 4 | Same-contract price/OI heuristic with location weighting |
| Futures | 2 | Spot/future move confirmation and basis change; no futures OI |
| Static OI | 1.5 | Weak structural context only |
| Local PCR | 1 | Light ATM-window context |
| VIX | 0 | Risk context only; never directional |

Dynamic OI, option positioning, and PCR share the same underlying OI evidence, so
their combined directional contribution is proportionally capped at `6`. This
prevents three representations of the same data from dominating the result.

Positioning heuristics are explicit: CALL short buildup and CALL long buildup lean
bearish; CALL short covering leans bullish; PUT short buildup leans bullish; PUT
long buildup and PUT short covering lean bearish. Long unwinding has half weight.
Contracts within two inferred strike steps receive full location weight, those from
two through four receive half weight, and farther contracts are excluded. These are
research heuristics, not market truths.

### Evidence quality, confidence, and safety gates

Evidence quality is HIGH only with sufficient contracts, usable dynamic OI and
volume, future, VIX, and prior price comparison. Missing secondary inputs degrade it
to MEDIUM or LOW; inadequate contracts/OI are INSUFFICIENT.

Confidence is deterministic:

```text
45% winning-score strength
+ 35% margin over the runner-up
+ 20% independent confirming-group count (full at three groups)
- 10 points per strong contradictory group
then multiply by evidence quality
```

LOW evidence is capped at 55 and INSUFFICIENT evidence at 30. `NO_TRADE` is
returned when evidence is insufficient, confidence is below 60, the winning margin
is below 2, directional classification lacks usable dynamic OI or prior features,
or RANGE lacks at least two positive range groups.

RANGE requires positive evidence such as muted movement, containment inside the
collector-observed opening range, confinement between OI clusters, or balanced
local PCR. A bull/bear tie alone does not create RANGE. Static OI cannot create a
high-confidence direction. Local PCR remains limited to the persisted ATM window.
VIX emits only LOW/NORMAL/ELEVATED/HIGH volatility risk flags.

## Phase 5 — on-demand AI research agent

Phase 5 adds a research-only interpretation layer over compact, structured Phase 3
and Phase 4 outputs. Phase 4 remains the deterministic source of regime
classification; the AI cannot modify it, place trades, select strikes, or issue
broker/order instructions. The full option chain, broker responses, credentials,
sessions, and database rows are not sent to the model.

The provider boundary is replaceable, with OpenAI as the first implementation.
The OpenAI call uses strict Pydantic structured output rather than parsing prose.
The prompt requires supplied evidence only, uncertainty when evidence is weak,
explicit missing evidence and conflicts, and no trade recommendations. Results use
`ai_version=phase5_v1` and `prompt_version=phase5_prompt_v2` so future behavior can
be versioned independently.

Post-validation applies configurable confidence ceilings of 40, 55, 75, and 90
for INSUFFICIENT, LOW, MEDIUM, and HIGH evidence respectively. Both original and
final confidence, whether a cap was applied, and the cap reason are retained.
AI confidence always uses a 0–100 percentage scale: `40` means 40%, while `0.4`
means 0.4% and is never automatically converted.
Unavailable intraday OI cannot be presented as confirmed fresh writing or
unwinding. Agreement with Phase 4 is recorded as AGREE, PARTIAL, DISAGREE, or
NOT_COMPARABLE; disagreement does not overwrite the deterministic result.

Configure these only in the ignored `.env` file:

```dotenv
OPENAI_API_KEY=<your API key>
AI_RESEARCH_MODEL=<a Structured Outputs-capable model available to your API project>
AI_MAX_RETRIES=1
AI_INPUT_COST_PER_MILLION=<current input price, optional>
AI_OUTPUT_COST_PER_MILLION=<current output price, optional>
```

Pricing is configuration rather than embedded business logic. When the API reports
usage and both price settings exist, input/output/total tokens and an estimated USD
cost are persisted. Otherwise cost remains null.

Apply migration `0004_phase5_ai_research`, then create research manually:

```powershell
alembic upgrade head
python scripts/build_ai_research.py --latest
python scripts/build_ai_research.py --snapshot-id 123
python scripts/build_ai_research.py --latest --force
```

The default is idempotent for `(market_snapshot_id, ai_version)` and returns an
existing successful result without another paid request. `--force` explicitly
regenerates it. There is no automatic three-minute AI scheduling in Phase 5.

Read-only endpoints are `GET /api/ai-research/latest`,
`GET /api/ai-research/{snapshot_id}`, and `GET /api/ai-research?limit=50`.

## Phase 6 — deterministic credit-spread candidates

Phase 6 generates hypothetical, defined-risk research candidates from persisted raw
quotes, `phase3_v1` features, and the authoritative `phase4_v1` regime. It never
uses Phase 5 AI to select strikes and contains no order placement, fill simulation,
position lifecycle, or naked option exposure. BULLISH maps only to a Bull Put
Spread; BEARISH maps only to a Bear Call Spread. RANGE and NO_TRADE produce the
valid structured result `NONE` with zero candidates.

Eligibility requires configurable regime confidence and evidence quality, the
snapshot's current expiry, two valid same-type legs, adequate quotes, defined-risk
payoff, liquidity, distance, credit, and an eligible width. By default, a Bull Put
short PE must be below spot and at or below the nearest meaningful support-zone low;
its long PE must be lower. A Bear Call short CE must be above spot and at or above
the nearest meaningful resistance-zone high; its long CE must be higher. Missing
structure conservatively produces no candidate. Only the snapshot's attached expiry
is considered.

Pricing uses the short bid and long ask when both are available. Otherwise it uses
the two LTPs and emits `PRICING_USING_LTP_ESTIMATE`; this is explicitly not an
executable credit. Missing delta is never estimated and emits `DELTA_UNAVAILABLE`.
When volume quality is unusable, the default emits `VOLUME_UNAVAILABLE` instead of
silently rejecting everything; requiring usable volume is configurable. OI,
volume, bid/ask width, short premium, net credit, and credit-to-width thresholds are
also configurable.

Allowed spread widths default to 50, 100, 150, and 200 points, but a spread is built
only when both actual strikes exist. Lot-level values stay null unless a confirmed
lot size is configured. Per-unit payoff is:

```text
net credit = short premium - long premium
max profit = net credit
max loss = spread width - net credit
bull put breakeven = short put strike - net credit
bear call breakeven = short call strike + net credit
```

Candidates rank deterministically on a 0–100 score: structure alignment 30%, credit
efficiency 25%, liquidity/pricing quality 20%, OTM risk buffer 15%, and deterministic
regime confidence 10%. Premium alone therefore cannot dominate. Stable reason codes
explain both eligible and no-candidate outcomes.

Apply migration `0005_phase6_strategy_candidates` and build from persisted data:

```powershell
alembic upgrade head
python scripts/build_strategy_candidates.py --latest
python scripts/build_strategy_candidates.py --snapshot-id 123
python scripts/build_strategy_candidates.py --all
```

`--latest` enforces the configured live freshness limit. Explicit snapshot IDs and
`--all` are historical research modes and do not call Kotak or reject old snapshots
for age. Recomputing `(market_snapshot_id, phase6_v1)` updates the existing row.
Read-only endpoints are `GET /api/strategy-candidates/latest`,
`GET /api/strategy-candidates/{snapshot_id}`, and
`GET /api/strategy-candidates?limit=50`.

## Phase 7 — hard deterministic risk approval

Phase 7 evaluates every Phase 6 candidate independently as `APPROVED`, `REJECTED`,
or `NOT_APPLICABLE`. Risk has absolute veto authority: checks are never averaged,
and any `FAIL` rejects the candidate. Phase 5 AI has zero approval authority. A
Phase 6 `NONE` result produces `NOT_APPLICABLE / NO_STRATEGY_CANDIDATE`, not an
error. This phase has no broker orders, fills, positions, or shadow trades.

Checks cover defined-risk structure, payoff reconciliation, Phase 4 regime and
evidence, raw/feature/regime identity, intraday OI policy, live freshness, entry
window, expiry-day policy, pricing quality, credit, width, both legs' liquidity,
lot availability, monetary limits, daily state hooks, duplicates, configured event
risk, consecutive regime confirmation, and optional candidate stability. `WARN` and
`NOT_AVAILABLE` checks are retained in structured output but cannot offset a fail.

Lot size is nullable raw snapshot metadata. New Kotak snapshots preserve only the
confirmed `common_data.mktLot` value; no NIFTY lot size is hardcoded or inferred.
Historical rows created before migration `0006` remain null. Missing lot size vetoes
lot-level approval. Per-unit max loss is `spread width - net credit`; per-lot max
loss multiplies that value by the snapshot-specific lot size.

`estimated_capital_required`, when available, is explicitly labelled
`DEFINED_RISK_MAX_LOSS_PROXY`. It is the theoretical maximum loss, not Kotak margin.
Both maximum-loss and maximum-capital monetary limits must be configured for an
approval; blank limits reject with `RISK_LIMIT_UNCONFIGURED` rather than assuming
unlimited capital.

Bid/ask pricing passes the pricing-quality check. A permitted LTP estimate produces
`NON_EXECUTABLE_PRICING_ESTIMATE`; requiring bid/ask converts it to a hard failure.
Live evaluations enforce the 09:35–13:30 IST entry window and snapshot freshness.
Explicit snapshot IDs and backfills use `HISTORICAL` context and bypass wall-clock
freshness/window mismatch while retaining all structural, expiry, data, and risk
checks. Expiry-day entry is disabled by default.

`RiskStateProvider` supplies trades today, realized P&L, and open strategy
fingerprints. The research provider supplies explicit zeros only in historical
mode and is not accepted as authoritative live state. Duplicate keys use trading
date, expiry, strategy type, and both strikes. `MarketEventProvider` supports local
timezone-aware configured events without web calls; blocking windows veto entry.
Consecutive same-direction Phase 4 confirmation defaults to two snapshots.
Candidate stability is implemented but disabled until live data supports it.

Apply migration `0006_phase7_risk`, configure explicit monetary limits, and build:

```powershell
alembic upgrade head
python scripts/build_risk_decisions.py --latest
python scripts/build_risk_decisions.py --snapshot-id 123
python scripts/build_risk_decisions.py --all
```

Read-only endpoints are `GET /api/risk/latest`, `GET /api/risk/{snapshot_id}`,
`GET /api/risk?limit=50`, and `GET /api/risk/{snapshot_id}/approved`.

## Phase 8 — observed-price shadow lifecycle

Phase 8 creates hypothetical shadow trades only from persisted Phase 7 `APPROVED`
decisions. It never sends a broker order, creates a broker/paper position, invents a
fill, or synthesizes an option price. By default it selects only Phase 7's
`best_approved_candidate`, which is the existing Phase 6 ranking winner among
approved candidates. Daily entry count and existing-open-position gates prevent a
new shadow trade every three minutes.

Entry preserves the exact Phase 6 legs, source snapshot, candidate JSON, risk
decision JSON, prices, pricing basis, lot size, and structural reference. BID/ASK
entry observes the short bid and long ask. A risk-approved LTP candidate uses the
exact short and long LTP observations and is labelled `ENTRY_PRICING_LTP_ESTIMATE`.
No slippage is added.

Later persisted snapshots are matched by exact expiry, option type, and strike,
with an instrument identifier used only to disambiguate exact matches. Strikes and
expiry never move. BID/ASK close valuation observes short ask minus long bid. LTP
fallback is allowed only when the original entry was an LTP research estimate. A
missing leg produces no mark and never substitutes a nearby contract.

For every valid mark:

```text
exit debit = short close price - long close price
P&L per unit = entry credit - exit debit
P&L per lot = P&L per unit × snapshot-specific lot size
MFE = maximum observed P&L since entry, including initial zero
MAE = minimum observed P&L since entry, including initial zero
```

Observed values are not silently clamped. A mark outside theoretical payoff bounds
is retained with `PAYOUT_ANOMALY`. Missing lot size leaves lot-level P&L null.

Deterministic exit defaults are 50% credit capture, a loss equal to 1.5 times entry
credit, opposite Phase 4 regime, breach of the entry-time structural reference, and
15:20 IST. The stop compares the positive magnitude of negative P&L with
`entry_credit × stop_loss_credit_multiple`; it is not an exit-debit multiple. If a
15:20 quote is unavailable, the first valid same-day quote through the hard cutoff
may close the trade. No next-day quote or invented expiry settlement is used;
unresolved trades become invalid with an explicit reason.

Replay processes snapshots in chronological order. At each timestamp it first
marks trades opened strictly earlier, then considers that timestamp's approved
entry. This ordering prevents future leakage and self-marking. Unique trade
fingerprints and unique `(shadow_trade_id, market_snapshot_id)` marks make reruns
idempotent. Replay and all manual commands read PostgreSQL only and are not wired
into the live collector.

```powershell
alembic upgrade head
python scripts/build_shadow_trade.py --latest
python scripts/build_shadow_trade.py --snapshot-id 123
python scripts/update_open_shadow_trades.py --latest
python scripts/run_shadow_replay.py --all
```

Analytics report trade counts, wins/losses, win rate, gross and net P&L, averages,
profit factor, expectancy, extremes, holding duration, MFE/MAE, and sequential
closed-trade drawdown separately for per-unit and available per-lot results.
Breakdowns cover strategy, entry hour, weekday, expiry DTE, confidence, evidence,
VIX regime, pricing basis, width, and credit efficiency. They describe outcomes and
do not infer causality.

Read-only endpoints are `GET /api/shadow/latest`, `GET /api/shadow/trades`,
`GET /api/shadow/trades/{id}`, `GET /api/shadow/trades/{id}/marks`,
`GET /api/shadow/performance`, and `GET /api/shadow/performance/breakdown`.

## Phase 9 — research pipeline orchestration

Phase 9 connects persisted snapshots to the existing deterministic research
services. It does not fetch broker data and has no execution capability. For each
snapshot it uses this strict order:

```text
revalue/exit previously open shadow trades
→ features → regime → optional AI → candidates → SHADOW risk → possible shadow entry
```

An entry created at a snapshot is therefore never marked against that same
snapshot. Backfill processes snapshots chronologically, so consecutive-regime
confirmation and existing lifecycle rules operate without future leakage. Every
phase retains its existing unique version key; `pipeline_runs` adds one summary row
per `(market_snapshot_id, phase9_v1)`, making reruns idempotent.

Risk evaluation now names three separate contexts. `HISTORICAL` accepts the
explicit zero-state research provider for backfills. `SHADOW` requires the
PostgreSQL-backed `ShadowRiskStateProvider`, which counts shadow entries, sums only
closed lot-level realized P&L, and returns open strategy fingerprints. That provider
is authoritative only for shadow records. `LIVE` still requires a future real
account provider and explicitly rejects both research and shadow state.

Shadow monetary policy is deliberately separate from future live policy. Configure
all applicable values before expecting a shadow approval:

```dotenv
SHADOW_RISK_CAPITAL_BASE=
SHADOW_RISK_MAX_LOSS_PER_TRADE=
SHADOW_RISK_MAX_CAPITAL_PER_TRADE=
SHADOW_RISK_MAX_DAILY_LOSS=
SHADOW_RISK_MAX_TRADES_PER_DAY=1
```

Blank mandatory shadow limits reject with `SHADOW_RISK_LIMIT_UNCONFIGURED`; they do
not mean unlimited capital and do not inherit `RISK_*` monetary values. At most one
lot is observed; Phase 9 does not size multiple lots.

Apply migration 0008 and run persisted snapshots without broker calls:

```powershell
alembic upgrade head
python scripts/run_research_pipeline.py --latest
python scripts/run_research_pipeline.py --snapshot-id 123
python scripts/run_research_pipeline.py --all
python scripts/run_research_pipeline.py --latest --with-ai
```

AI defaults off. `--with-ai` is explicit for manual runs; an AI failure is recorded
but never prevents candidate, risk, or shadow processing. Deterministic phases do
not consume AI output.

Collector triggering is also opt-in. Recommended settings for Monday's shadow-only
research run are:

```dotenv
PIPELINE_AFTER_SNAPSHOT=true
PIPELINE_RUN_AI_RESEARCH=false
```

The raw snapshot commits before the callback. Pipeline failure cannot roll it back
or stop later scheduled collection. Review a day with:

```powershell
python scripts/daily_research_summary.py
python scripts/daily_research_summary.py --date 2026-10-05
```

The summary is deterministic: snapshot/OI counts, regimes, candidate sets, risk
decisions, shadow entries/exits/P&L/open count, and recorded AI calls/cost. Read-only
pipeline endpoints are `GET /api/pipeline/latest`,
`GET /api/pipeline/{snapshot_id}`, and `GET /api/pipeline?limit=50`.

## Phase 10 — read-only research terminal

The `frontend/` application is a desktop-first Next.js, TypeScript, Tailwind CSS,
and Recharts interface over the existing FastAPI read models. It contains no
execution controls and never calculates regimes, candidate ranks, risk decisions,
P&L, MFE, or MAE. Those values remain backend-owned.

Routes:

- `/` — current market, deterministic regime, OI structure, positioning,
  candidates, risk checks, shadow state, performance, and pipeline health
- `/history` and `/history/[snapshotId]` — snapshot list and complete audit trail
- `/shadow` and `/shadow/[tradeId]` — shadow-trade list, marks, P&L path, and source evidence
- `/system` — API connectivity and accurately labelled data freshness

The dashboard and open-shadow marks poll every 15 seconds by default. Historical
tables load on navigation and do not poll. All timestamps render in Asia/Kolkata.
Missing records remain visibly unavailable; no placeholder market values are used.
`GET /api/dashboard/latest` aggregates existing read models for the current view
and exposes only safe configuration booleans and collection timings.

Create `frontend/.env.local` from `frontend/.env.example`:

```dotenv
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
NEXT_PUBLIC_POLL_INTERVAL_MS=15000
```

Local development:

```powershell
# Terminal 1
docker compose up -d postgres

# Terminal 2
uvicorn app.main:app --app-dir backend --reload

# Terminal 3
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`. Frontend verification commands are `npm test`,
`npm run lint`, and `npm run build`.

## Logging and data quality

Collector logs are concise event names and counts. Full broker responses and
credentials are never stored or logged. Phase 1 aggregate option-chain quality
counters remain active. The recorder does not recompute OI change, calculate IV or
Greeks, backfill volume, replace a null VIX/future, or reuse an earlier snapshot.

## Phase 11 — observability and operations

Phase 11 adds a read-only operations layer. It does not participate in feature,
regime, strategy, risk, or shadow decisions. The scheduled collector persists one
heartbeat row per `OBS_COLLECTOR_WORKER_ID` every 60 seconds by default. Snapshot
freshness is a separate signal: during the configured collection window it is
healthy through five minutes, degraded through ten minutes, and unhealthy after
that. On weekends, configured holidays, and outside the collection window the
market-dependent signals are `IDLE`, so Sunday does not create false incidents.

The operations model uses the existing collector and pipeline histories plus two
new stores: `collector_heartbeats` and `operational_events`. Events contain bounded,
redacted messages rather than public stack traces. Pipeline runs now retain JSON
timings for shadow pre-update, features, regime, optional AI, strategy, risk,
shadow entry, and total duration. AI health is inferred from configuration and
persisted calls; health polling never makes a paid provider request or broker call.

Read-only endpoints:

- `GET /api/health` — compact aggregate and component states
- `GET /api/system/health` — detailed collector, data, DB, pipeline, shadow, and AI health
- `GET /api/system/events?limit=100` — filtered safe operational events
- `GET /api/system/metrics` — compact current-day operations metrics

The `/system` terminal page polls these endpoints every 30 seconds and the global
top bar links its compact system state to that page. Raw log files are never
exposed. Local logs rotate at `LOG_MAX_BYTES`, retain `LOG_BACKUP_COUNT` files, and
are split into `app.log`, `collector.log`, `pipeline.log`, and `error.log`.

Operations commands:

```powershell
python scripts/system_health.py
python scripts/daily_operations_summary.py
python scripts/daily_operations_summary.py --date 2026-10-05
```

`system_health.py` returns 0 for healthy/idle, 1 for degraded/unknown, and 2 for
unhealthy. Phase 11 settings are `OBS_COLLECTOR_WORKER_ID`,
`OBS_COLLECTOR_HEARTBEAT_SECONDS`, `OBS_EXPECTED_SNAPSHOT_INTERVAL_SECONDS`,
`OBS_DATA_FRESH_HEALTHY_SECONDS`, `OBS_DATA_FRESH_DEGRADED_SECONDS`,
`OBS_PIPELINE_DEGRADED_FRACTION`, `LOG_DIR`, `LOG_MAX_BYTES`, and
`LOG_BACKUP_COUNT`. Exchange holidays continue to use `COLLECTOR_HOLIDAYS`.

## Phase 12 — outbound Telegram notifications

Telegram is an optional reporting side effect and is disabled by default. The
integration only calls Telegram's outbound `sendMessage` API. It has no inbound
commands, callbacks, setting changes, order buttons, remote control, or broker
execution. A Telegram failure is recorded after bounded delivery attempts and is
never allowed to roll back market data, research results, risk decisions, or
shadow trades.

Delivery history is persisted in `notification_deliveries`. The unique channel
and semantic dedupe key prevents repeated alerts across polling, replay, and
process restarts. Likely transient failures—timeouts, transport failures, HTTP
429, and HTTP 5xx—receive at most `TELEGRAM_MAX_RETRIES` retries. HTTP 400, 401,
and 403 fail immediately. The safe retry script selects only persisted transient
`FAILED` rows; it never recomputes research or resends `SENT` deliveries.

Supported routing covers Phase 11 operational transitions, newly confirmed or
changed directional regimes, the top candidate, approved or important rejected
risk decisions, shadow entries/exits, and one combined research/operations EOD
summary. Repeated directional snapshots are suppressed until direction changes.
Market freshness alarms are suppressed outside the configured market session and
on weekends or configured holidays. The EOD job runs on the existing collector
scheduler and is deduplicated by trading date.

Configuration:

```dotenv
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_TIMEOUT_SECONDS=10
TELEGRAM_MAX_RETRIES=2
TELEGRAM_NOTIFY_OPERATIONAL=true
TELEGRAM_NOTIFY_REGIME=true
TELEGRAM_NOTIFY_STRATEGY=true
TELEGRAM_NOTIFY_RISK=true
TELEGRAM_NOTIFY_SHADOW=true
TELEGRAM_NOTIFY_DAILY_SUMMARY=true
TELEGRAM_NOTIFY_RANGE=false
TELEGRAM_SEND_STARTUP_MESSAGE=false
TELEGRAM_DAILY_SUMMARY_TIME=15:40
```

Bot setup:

1. Open Telegram and start a chat with `BotFather`.
2. Create a bot and copy the generated bot token.
3. Start a chat with the new bot and send it one message.
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the
   numeric `message.chat.id` from the response. Do not share or commit that URL.
5. Store the token and chat ID only in `.env`.
6. Set `TELEGRAM_ENABLED=true` and run the safe manual test below.

Never commit the real token or chat ID. The health and history APIs expose only
enabled/configured booleans and safe delivery metadata:

```powershell
python scripts/test_telegram.py
python scripts/retry_failed_notifications.py --today --limit 20
```

`GET /api/notifications` supports `limit`, `status`, `event_code`, and `date`
filters. Telegram state and recent deliveries are shown read-only on `/system`;
tokens, chat IDs, full endpoints, and resend controls are deliberately absent.

## Known limitations

- India VIX is requested through Kotak's exact documented index name `INDIA VIX`.
  It remains nullable if that official REST route is unavailable or returns no LTP.
- Exchange holidays must be supplied with `COLLECTOR_HOLIDAYS`.
- There is no historical backfill or automatic retention deletion.
- The project remains research-only. Live broker state, paper/live execution,
  multi-lot sizing, and broker orders are out of scope.

## AWS LIGHTSAIL DEPLOYMENT

Phase 13 packages the read-only research platform for one Ubuntu Lightsail VM. It
does not create AWS resources, change DNS, or deploy itself. The production path is:

```text
Internet -> Caddy :80/:443 -> Next.js :3000
                         \-> FastAPI :8000 for /api/*
FastAPI --------------------> PostgreSQL :5432
one collector -> PostgreSQL -> deterministic research pipeline
```

Only Caddy publishes host ports. PostgreSQL, FastAPI, and Next.js are reachable
only by service name on the Compose network. The collector is a distinct, single
service; neither API startup nor API workers own its scheduler. Its existing 15:40
Telegram summary therefore also has one owner. Restarting during market hours
resumes the normal schedule without backfilling or fabricating missing observations.

### 1. Create and secure the instance

Use an Ubuntu Lightsail instance with at least 4 GB RAM (for example, 2 vCPU/4 GB)
and sufficient SSD space for the expected PostgreSQL history and images. The
absolute smallest tier risks out-of-memory failures when Postgres, Python, Next.js,
and Docker build together. Scale the plan or disk later as retained data grows.

Attach a Lightsail static IP. In the Lightsail firewall, allow only:

- TCP 22 from trusted administration addresses where practical
- TCP 80 from the internet
- TCP 443 from the internet

Do not add public rules for 3000, 5432, or 8000. Then connect by SSH and install
Git plus Docker Engine and the current Compose plugin:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and reconnect so Docker group membership applies, then verify:

```bash
docker --version
docker compose version
```

Optional swap can provide a safety margin on a smaller instance. It is host
administration, not application setup; confirm free disk space first:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 2. Clone and configure

Keep the source in a private GitHub repository. An SSH deploy key is recommended:
add the server's public key to the repository, then clone the SSH URL. A fine-grained
PAT is an alternative, but enter it through Git's credential prompt/helper—never put
it in a clone URL, script, environment template, or commit.

```bash
git clone git@github.com:YOUR_ACCOUNT/YOUR_PRIVATE_REPOSITORY.git
cd YOUR_PRIVATE_REPOSITORY
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Replace every placeholder. Use a unique database user and strong password, and
URL-encode special characters in the password portion of `DATABASE_URL`. The URL's
host must remain `postgres`, for example
`postgresql+psycopg://USER:ENCODED_PASSWORD@postgres:5432/DB`. Store Kotak, OpenAI,
and Telegram credentials only in the ignored `.env.production`. Never use a secret
in `NEXT_PUBLIC_*`; those variables are embedded into the browser build.

Production defaults intentionally use `PIPELINE_AFTER_SNAPSHOT=true`,
`PIPELINE_RUN_AI_RESEARCH=false`, and `TELEGRAM_ENABLED=false`. The deterministic
pipeline can run after each stored snapshot without paid AI calls. OpenAI remains
optional. Telegram becomes active only after setting its two credentials and
`TELEGRAM_ENABLED=true`; no Telegram credential is required otherwise.

Keep `NEXT_PUBLIC_API_BASE_URL` blank so browser requests use same-origin `/api`.
For local development, `frontend/.env.local` may continue using
`http://127.0.0.1:8000`. Same-origin production needs no CORS entry. If a separate
trusted browser origin is required, list it explicitly in `CORS_ALLOWED_ORIGINS`;
wildcards are rejected. `ENABLE_API_DOCS=false` disables `/docs`, `/redoc`, and
`/openapi.json` in production without changing the local default.

### 3. Start via static IP

For initial IP-only validation, leave `SITE_ADDRESS=:80`. Validate, build, run the
one-shot Alembic migration, and start the stack with:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml config --quiet
bash scripts/deploy.sh
docker compose --env-file .env.production -f docker-compose.prod.yml ps
python3 scripts/deployment_smoke_test.py --base-url http://127.0.0.1
docker compose --env-file .env.production -f docker-compose.prod.yml exec -T backend python /app/scripts/system_health.py
```

The `migrate` service runs `alembic upgrade head` after PostgreSQL becomes healthy.
Backend and collector depend on its successful completion, so migrations never race
from multiple application processes. Open `http://LIGHTSAIL_STATIC_IP/`. The smoke
test calls only read endpoints—it never calls Kotak, OpenAI, Telegram, or an order
API. Outside market hours, `collector`, `data_freshness`, `market_data`, `pipeline`,
and `shadow` may correctly be `IDLE`; an overall `IDLE` is not a deployment failure.

Inspect bounded Docker logs (10 MB per file, five files per container):

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f collector
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f backend
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f caddy
```

PostgreSQL uses the named `postgres_data` volume, so restarts, container recreation,
and image rebuilds retain data. Caddy certificate state uses `caddy_data` and
`caddy_config`. Never use `docker compose down -v` during normal operations.
Docker is enabled at boot and the long-running services use `restart:
unless-stopped`, so the stack recovers after a VM reboot. Resource limits are not
hard-coded; monitor memory and disk and resize the instance when required.

### 4. Add a domain and automatic HTTPS (optional)

Create a DNS A record pointing the domain to the Lightsail static IP. After DNS is
publicly resolving, set `SITE_ADDRESS=trade.example.com` in `.env.production`, then:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml up -d caddy
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f caddy
```

Caddy obtains and renews a public certificate automatically. Static-IP mode stays
plain HTTP deliberately; it does not create a confusing self-signed certificate.

### 5. Update safely

Commit and test locally, push to the private repository, then run on Lightsail:

```bash
cd YOUR_PRIVATE_REPOSITORY
bash scripts/update_server.sh
```

The update uses `git pull --ff-only`, validates Compose, rebuilds images, reruns the
one-shot migration dependency, recreates changed services without deleting volumes,
and runs the read-only smoke test. `bash scripts/deploy.sh --pull` is the equivalent
first-deploy flow with an optional fast-forward pull.

### 6. Back up and restore PostgreSQL

Create a compressed custom-format dump owned only by the current server user:

```bash
bash scripts/backup_postgres.sh
ls -lh backups/
```

`BACKUP_RETENTION_DAYS` defaults to 14 and can be set in `.env.production` or the
host environment. The script deletes only matching old dump files inside the
project's `backups/` tree. An optional daily cron entry is:

```cron
15 16 * * * cd /home/ubuntu/YOUR_PRIVATE_REPOSITORY && /usr/bin/bash scripts/backup_postgres.sh >> /var/log/nifty-backup.log 2>&1
```

Restore is intentionally manual and destructive to the target database. Confirm the
file and environment, take a fresh backup first, and perform it only during a
maintenance window:

```bash
cd YOUR_PRIVATE_REPOSITORY
bash scripts/backup_postgres.sh
docker compose --env-file .env.production -f docker-compose.prod.yml stop backend collector
cat backups/nifty_research_YYYYMMDDTHHMMSSZ.dump | docker compose --env-file .env.production -f docker-compose.prod.yml exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm migrate
docker compose --env-file .env.production -f docker-compose.prod.yml up -d backend collector frontend caddy
python3 scripts/deployment_smoke_test.py --base-url http://127.0.0.1
```

The dashboard is initially public and read-only. It contains market research and
must never expose credentials; authentication can be designed separately. This
deployment adds no order execution, broker position management, inbound Telegram
commands, CI/CD, or automatic cloud backup.

## Phase 14 — historical statistical alpha design (superseded by Phase 14.1 below)

Phase 14 adds a deterministic, versioned `phase14_v1` evidence layer. It does not
replace derivatives structure, classify the final regime by itself, select strikes,
approve risk, or create orders. Production remains unchanged because both
`ALPHA_ENGINE_ENABLED` and `REGIME_USE_STATISTICAL_ALPHA` default to `false`.

For source `FUTURE` (default) or `SPOT`, the horizon observation is the strictly
prior persisted snapshot closest to 300 seconds within the configured tolerance.
There is no interpolation. With `P_t` as the selected current reference, `P_h` as
that prior reference, and `P_open` as the first persisted reference in the current
session:

```text
price_return_t = (P_t - P_h) / P_open
alpha_1_t = historical_midrank(price_return_t)
```

The rank window is duration-based, not row-count based. Only values strictly before
`t` are in the comparison population. Midrank tie handling is `(count_less +
0.5 × count_equal) / historical_count`. Fewer than
`ALPHA_MIN_RANK_OBSERVATIONS` returns null with `INSUFFICIENT_ALPHA_HISTORY` and
`WARMING_UP`; lookbacks are never shortened automatically.

ATM is the nearest strike to the selected reference for which both CE and PE exist
at the snapshot expiry. An exact distance tie selects the lower strike. Interval
volume is the current cumulative broker volume minus the previous same-contract,
same-expiry cumulative volume. A negative difference is a reset and is invalid.
Each leg's activity ratio divides that interval volume by its rolling mean of prior
valid intervals; zero baselines and missing/reset observations remain null. The raw
put/call interval ratio and signed `(CE - PE) / (CE + PE)` imbalance are stored but
are not independently labelled bullish or bearish.

`ATM_OBSERVED_PRICE_VOLATILITY` is the mean of the population standard deviations
of observed log-price returns for the continuous ATM CE and PE contracts. It is not
implied volatility. Missing legs, non-positive prices, inadequate returns, sparse
sequences, or volatility at/below epsilon invalidate Alpha 2:

```text
atm_volume_activity = mean(CE interval-volume ratio, PE interval-volume ratio)
atm_option_volatility = mean(CE observed return volatility, PE observed return volatility)
directional_impulse_raw = price_return × atm_volume_activity / atm_option_volatility
alpha_2 = historical_midrank(directional_impulse_raw)
```

Joint states preserve disagreement as `CONFLICT`; conflicting extremes are never
averaged. Directional confirmation counts only consecutive same-direction joint
states available at or before the current snapshot. Evidence quality considers rank
depth, same-contract ATM coverage, valid interval volume, observed-return coverage,
and timestamp continuity. High signal values with low quality stay low-quality.

When both feature flags are deliberately enabled, the pipeline order becomes:

```text
shadow pre-update → Phase 3 → Phase 14 alpha → Phase 4 → optional AI
→ candidates → risk → possible shadow entry
```

Phase 4 gives Alpha 1 and Alpha 2 initial weights of 4 each but caps their combined
statistical-alpha contribution at 5 because both contain the same price return.
The existing dynamic-OI/positioning/PCR cap remains separate. A directional regime
requires confirmed same-direction alpha plus at least one non-alpha confirmation.
Strong alpha versus strong opposite derivatives evidence produces
`STRONG_SIGNAL_CONTRADICTION`, a confidence penalty, and `NO_TRADE`. VIX remains
non-directional context.

Phase 6 keeps OTM, beyond-structure short strikes. The optional volatility buffer
multiplies minimum point and percentage distances by configured LOW/NORMAL/ELEVATED/
HIGH factors; it is disabled by default. A separate configured HIGH-volatility
policy can return no candidate. Phase 14 never changes the default to selling ATM.

Apply migration and build persisted data without Kotak or OpenAI calls:

```powershell
alembic upgrade head
python scripts/build_alpha_features.py --latest
python scripts/build_alpha_features.py --snapshot-id 123
python scripts/build_alpha_features.py --all
```

Read-only endpoints are `GET /api/alpha/latest`, `GET /api/alpha/{snapshot_id}`,
`GET /api/alpha?limit=50`, and `GET /api/alpha/history?date=YYYY-MM-DD`. The dashboard
shows the current values, confirmation, quality, warnings, and a same-day Alpha 1/
Alpha 2 chart with 0.20/0.50/0.80 references. Shadow records preserve entry alpha
context for descriptive—not causal—performance breakdowns.

The old Phase 14 shadow-outcome filter below is deprecated; it cannot price altered
strikes, widths, times, or exits. Phase 14.1's chronological replay is documented below.

```powershell
python scripts/run_alpha_experiments.py --split TRAIN
python scripts/run_alpha_experiments.py --split VALIDATION
python scripts/run_alpha_experiments.py --split FINAL_TEST --inspect-final-test
```

The current utility reconstructs alternative candidates and quote paths from stored
observations, labels missing exact-leg quotes `NOT_EVALUABLE`, models configured costs,
and records every attempted trial. FINAL_TEST requires explicit inspection and reports
repeat inspection. Split-overlapping outcomes are purged.

Kotak's current live option-chain endpoint does not reconstruct the project's exact
historical three-minute snapshots, cumulative-volume sequence, or continuously
matched ATM option prices. Existing persisted observations can be backfilled only
for the dates already recorded. Otherwise Phase 14 must warm up prospectively; no
synthetic history, interpolated option chain, fabricated volume, or inferred IV is
permitted.

### Monday market-hours validation sequence

Keep both feature flags false during the first deployment. Back up PostgreSQL,
deploy, and let the existing collector record the complete session unchanged:

```bash
bash scripts/backup_postgres.sh
bash scripts/update_server.sh
docker compose --env-file .env.production -f docker-compose.prod.yml exec -T backend alembic -c /app/alembic.ini current
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f collector
```

After market close, build alpha retrospectively from only the persisted Monday
snapshots and inspect the read API:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml exec -T backend python /app/scripts/build_alpha_features.py --all
curl -s "http://127.0.0.1/api/alpha/history?date=YYYY-MM-DD"
docker compose --env-file .env.production -f docker-compose.prod.yml exec -T backend python /app/scripts/system_health.py
```

Confirm that ATM tokens/strikes stay on the snapshot expiry, interval volumes equal
the differences between consecutive broker cumulative values, resets remain null,
observed volatility is based only on positive same-contract LTPs, early rows show
`WARMING_UP`, and ranks appear only after at least 50 historical raw values. Review
`ALPHA_VOLUME_UNUSABLE`, `ALPHA_VOLATILITY_UNUSABLE`, sparse-sequence, and reset
counts rather than suppressing them.

Do not enable alpha on Monday. Keep `ALPHA_ENGINE_ENABLED=false` and
`REGIME_USE_STATISTICAL_ALPHA=false` through collection, after-close calculations,
and anomaly review. Diagnostic activation is a later explicit decision.

## Phase 14.1 — corrected causal research semantics

`phase14_1_v1` preserves Phase 14 rows under their original version. Alpha 1 uses
`ln(P_t/P_(t-h))` from a strictly prior positive price of the same instrument and
session; the reference must lie 240–420 seconds back (target 300), with actual
horizon and error persisted. The old session-open-normalized return remains a
comparator only. Ranks use prior valid same-reference, comparable-horizon values and
historical midranks. An upper rank alone never means a positive return: continuation
requires positive return plus upper rank for bullish, negative return plus lower rank
for bearish. Reversal is a distinct, research-only hypothesis.

`ALPHA_LOOKBACK_CLOCK_MODE=TRADING_MINUTES` makes 800 minutes count eligible NSE
09:15–15:30 time across observed sessions, skipping nights, weekends, and configured
holidays. `WALL_CLOCK` and `SESSION_ONLY` are explicit alternatives. Neither returns,
interval volume, nor signal-persistence streaks bridge sessions. Futures require the
same broker-provided symbol and expiry; absent identity means unavailable.

CE/PE cumulative-volume deltas require exact exchange, token, expiry, strike, type,
and session continuity; the first observation is a baseline, long gaps are diagnostic
only, and resets never feed the activity baseline. Each leg's prior valid intervals
use a configurable median or trimmed mean. Option-price volatility is diagnostic,
never the Alpha 2 divisor. Default Alpha 2 ranks signed underlying horizon return
divided by prior comparable underlying-return volatility only above a configured
minimum; otherwise it is unavailable. Unsigned participation cannot vote direction.
The legacy return × activity / option-volatility composite is diagnostic only.

When future alpha influence is deliberately enabled, underlying-price signals share
one score cap and one confidence evidence mechanism. Broker OI change with unclear
baseline does not provide a fresh directional vote. Separate synchronized basis is
unavailable until quote timestamps support it. Confidence is a deterministic evidence
score, not a calibrated probability of profit. Signal persistence is overlapping
observations, not independent confirmation.

The optional replay requires exact stored contract quotes at the next observation,
fresh non-crossed books, bounded leg timestamp skew, and modeled depth where known.
Missing quotes or path marks produce `NOT_EVALUABLE`; no nearby-contract substitution
or synthetic fill is allowed. Results include configurable brokerage, exchange charges,
STT, GST, stamp duty, and slippage stress, sampled MAE/MFE, session count, and tiered
sample labels. The old shadow outcomes remain descriptive, not counterfactual.

After a full validation session, with all four production feature flags still false:

```powershell
python scripts/build_alpha_features.py --all
python scripts/validate_alpha_session.py --date YYYY-MM-DD
python scripts/validate_alpha_session.py --date YYYY-MM-DD --prefix-snapshot-id 123
python scripts/run_alpha_experiments.py --split TRAIN
python scripts/run_alpha_experiments.py --split VALIDATION
```

Do not inspect `FINAL_TEST` while tuning. `--inspect-final-test` is an explicit,
audited final step. Historical computations are labeled `HISTORICAL_REPLAY`; live
original rows are insert-once, and `RESEARCH_RECOMPUTE` stays separate. Broker source
timestamps and depth remain nullable where Kotak does not supply them, so many
historical alternatives can honestly remain `NOT_EVALUABLE`.

Phase 14.2 adds opt-in, versioned intraday credit-spread economics. Directional strength, survival, carry, expected move, DTE and gamma context are separate from hard risk. Both new flags default to false. See [the implementation and validation report](docs/phase14_2_report.md) for formulas, configuration, migration, validation and the required offline replay before Phase 15.
