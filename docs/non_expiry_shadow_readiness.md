# Non-expiry shadow readiness audit — 2026-10-06

Branch: `research/agent-analysis-2026-10-06`. No deployment, production flag
change, broker request, migration, merge or commit was performed by this audit.

## Verified October 6 evidence

Run `85611f41-1d29-4b70-9209-f15d2206b655`, recorded commit
`6d96cd2a28a0862179fea499f1ee246237f647fc`:

- The manifest/dataset fingerprints, all 405 event hashes and the summary seal verify.
- 124/124 observations and features, 09:18–15:27 IST, no missing/duplicate/off-cadence observations.
- 124 causal feature reconstructions, 78 decisions, 78 final outcomes, one summary.
- All decisions and outcomes say `ZERO_DTE_EXCLUDED`; no risk approval, fill
  attempt or entry execution was recorded. Every snapshot expires on October 6.
- `NOT_EVALUABLE` and `profitability_claim=false` are correct. This is a successful
  infrastructure smoke test, not a test of the strategy's trading performance.
- The 78 scheduled decision observations are 09:36 through 13:27. The cadence
  and 09:35–13:30 entry window explain this count without missing observations.
- Of 5,208 contract rows, 5,131 books pass the registered book checks, 74 exceed
  the registered spread percentage limit and three lack bid prices. All quantities
  have `depth_unit=UNKNOWN`; the recorded cost schedule is incomplete. There are
  no alpha rows and the registered regime has statistical-alpha usage disabled.

0DTE is the sole **recorded terminal reason**, not proof every subsequent gate
would pass. Removing that guard is prohibited. The independent book diagnostics
do not simulate trades or change any ledger event.

Before edits, the current implementation hash matched the manifest. Any source
change, including this audit utility, changes the implementation hash. Keep the
old run sealed; execute new research through a newly registered manifest. Never
edit the old manifest to accept a new source hash.

## Controls are separate

| Control | Existing implementation | Meaning |
| --- | --- | --- |
| Replay integrity | `research/config.py`, `manifest.py`, `ledger.py`, `replay.py` | Fixed 0DTE exclusion, data/config/code provenance, causal features, next-observation fills, gap/TTL checks, sealed evidence |
| Strategy selection | `strategy/policy.py`, candidate engine, regime engine | Alpha/direction, survival/carry, widths and scores; no values changed |
| Risk | `risk/engine.py`, configured limits and shadow state | Approval is distinct from a fill; no thresholds changed |
| Executable fills | `research/quotes.py` and integrity replay | Exact contracts, positive finite uncrossed books, source age/skew, confirmed depth units and adequate quantity |
| Forward pipeline | `pipeline/service.py`, `shadow/entry.py`, `shadow/lifecycle.py` | Older same-observation simulation; not the integrity replay engine |
| Production flags | `Settings`, collector factory | Phase 14.2 selects strategy versions; Phase 14.2.1 does **not** provide a live/forward integrity engine |

The older shadow entry can use LTP estimates and only warns about absent/stale
quote times. Its valuation can accept a zero bid or stale book. Missing exits can
be changed to INVALID rather than retained as authoritative unresolved exposure.
Shadow SQL rows and marks are mutable; they are not the immutable replay ledger.
The after-snapshot pipeline updates shadow positions before computing the current
regime, so a current opposite-regime exit is also not guaranteed on a fresh run.
Do not treat legacy shadow metrics (including their pre-cost `net_pnl` label) as
primary evidence or a profitability claim.

## Small safety changes

`PIPELINE_DECISION_ONLY=true` explicitly skips both legacy shadow marking and
entry. It still stores normal features, optional alpha, regimes, candidates and
risk decisions. It refuses to run if open shadow positions exist, to avoid
silently abandoning their manager. The default is false for compatibility;
the new mode must be chosen explicitly. Existing production settings were not edited.

Setting `PHASE14_2_1_REPLAY_INTEGRITY_ENABLED=true` when constructing the forward
pipeline now raises `INTEGRITY_REPLAY_IS_OFFLINE_ONLY`. The existing offline CLI
still uses its registered JSON configuration. It is unchanged.

`scripts/audit_integrity_run.py` verifies existing evidence read-only and reports
book/depth blockers without recomputing alpha or inventing data.

## October 7 operating configuration

The supported immediate workflow is **capture + decision rehearsal + daily
isolated replay**, with no simulated fills in the forward rehearsal. It is not
yet the requested full automatic paper-entry/exit workflow.

For a future explicitly authorized decision-only deployment, the minimum overlay
on the existing validated market-data deployment is:

```dotenv
PIPELINE_AFTER_SNAPSHOT=true
PIPELINE_DECISION_ONLY=true
PIPELINE_RUN_AI_RESEARCH=false
PHASE14_2_STRATEGY_LOGIC_ENABLED=false
PHASE14_2_1_REPLAY_INTEGRITY_ENABLED=false
REPLAY_ALLOW_0DTE=false
REPLAY_ALLOW_UNKNOWN_DEPTH=false
RISK_REQUIRE_BID_ASK=true
RISK_ALLOW_LTP_ESTIMATE=false
RISK_ALLOW_EXPIRY_DAY=false
REGIME_USE_STATISTICAL_ALPHA=false
```

Keep existing approved monetary risk limits and all selection thresholds
unchanged. Missing limits must continue to reject approvals. Alpha capture is
optional: `ALPHA_ENGINE_ENABLED=true` records LIVE_ORIGINAL context without
turning on statistical-alpha regime selection. It requires sufficient historical
warm-up; absent alpha is not repaired or synthesized. Leave it false unless that
capture is explicitly enabled. Phase 14.2 selection may be tested separately in
registered offline research or a separately authorized isolated research database;
the overlay above intentionally uses the existing legacy selection version.

**Do not rebuild/deploy this branch as the production collector.** Its current
`worker/collector.py` still uses a startup-relative interval and its calendar
ends at exactly 15:27:00; its adapter/snapshot path also lacks the newer batch-depth
capture code. Those already-validated production capture fixes must first be
reconciled into the research build and regression-tested. The October 6 dataset
proves production capture worked, not that this checkout matches that deployment.

## Commands (not executed against production)

Read-only local audit:

```bash
python scripts/audit_integrity_run.py --namespace research-output --run-id 85611f41-1d29-4b70-9209-f15d2206b655 --dataset-json integrity_2026-10-06.json
```

After a reviewed image contains the changes, this **one-shot decision rehearsal**
starts no collector and no migration. It writes analysis records in the configured
database, so use a research database for evaluation; it creates no shadow fills:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm --no-deps -e PIPELINE_DECISION_ONLY=true -e PHASE14_2_STRATEGY_LOGIC_ENABLED=false -e PHASE14_2_1_REPLAY_INTEGRITY_ENABLED=false -e PIPELINE_RUN_AI_RESEARCH=false -e RISK_REQUIRE_BID_ASK=true -e RISK_ALLOW_LTP_ESTIMATE=false -e RISK_ALLOW_EXPIRY_DAY=false -e REGIME_USE_STATISTICAL_ALPHA=false -e ALPHA_ENGINE_ENABLED=false -e TELEGRAM_ENABLED=false backend python /app/scripts/run_research_pipeline.py --latest
```

This command inherits the database configuration; it does not create or isolate a
database automatically. Never run two research writers for the same snapshot/version.
Automatic scheduling uses the overlay above only after explicit deployment approval
and capture-code reconciliation. There is no valid flag-only command to enable a
forward Phase 14.2.1 fill engine: that integration does not exist yet.

Daily existing operational summary (descriptive legacy metrics, not evidence):

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml run --rm --no-deps backend python /app/scripts/daily_research_summary.py --date 2026-10-07
```

After the non-expiry export, use `run_integrity_replay.py --register` with a new
date-specific frozen configuration and then `--run-id` for that registration.
Do not reuse the October 6 run ID or change its sessions after registration.

## Remaining work before fill-capable forward paper research

1. Obtain broker confirmation of derivative depth units; preserve UNKNOWN until
   then. No code change can turn numeric quantities alone into confirmed capacity.
2. Reconcile the proven capture code into this branch without replacing production.
3. Add a bounded forward execution integration: durable decision-at-t / fill-at-t+1
   state, exact-contract fill-time revalidation through the existing quote/risk gates,
   restart idempotency, immutable decision/fill/exit journaling, and retained
   unresolved exposure at gaps/session end. Keep it isolated from production keys.
4. Use observed exit-side books and record source timestamps/quantity on both legs;
   use the same observation-driven exit rules and effective cost accounting as replay.
5. Supply a complete effective cost schedule before net evidence; do not copy the
   permissive smoke-test monetary limits into production or tune selection thresholds.
6. Record more non-expiry sessions, preserving all rejections. No minimum trade
   count alone guarantees primary evidence or a profitability conclusion.

This is an execution/audit integration task, not strategy optimization. The
three-minute callback is synchronous with collection, `max_instances=1`, and
coalescing: a slow pipeline can skip observations. Keep AI/network work off, inspect
recorded stage timings, and demonstrate a sub-cadence runtime before automation.

The application broker adapter/protocol expose only market-data methods. No
application order-placement call was found. SDK order capabilities are not wired
into these paths. Decision-only mode cannot produce a broker order or a paper fill.
