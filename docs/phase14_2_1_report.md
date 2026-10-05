# Phase 14.2.1 — Replay Integrity & Research Controls

Internal integrity version: `phase14_2_1_v1`. Strategy logic remains `phase14_2_v1`.
Branch: `phase14.2.1-replay-integrity`. Audited parent: `b0a22b59464bb59df2d2e02322c630a2b7c29b83`.

This is an offline integrity/accounting correction, not a strategy redesign or evidence of profitability. No production services, credentials, feature flags, deployments, broker/market/order APIs, OpenAI APIs or Telegram sends were used. Test fixtures select the research implementation explicitly in memory; application/environment flags remain disabled. No real dataset replay or final-test inspection was performed.

## 1. Files changed

New files:

- `alembic/versions/0014_phase14_2_1_replay_integrity.py`
- `backend/app/research/analytics.py`, `config.py`, `costs.py`, `features.py`, `isolation.py`, `ledger.py`, `manifest.py`, `moves.py`, `oi.py`, `quotes.py`, `replay.py`
- `backend/app/risk/repricing.py`
- `backend/tests/test_phase14_2_1_integrity.py`
- `scripts/run_integrity_replay.py`
- `docs/phase14_2_1_report.md`

Modified files:

- `.env.example`, `.env.production.example`, `.gitignore`
- `backend/app/alpha/replay.py`, `backend/app/core/config.py`
- `backend/app/data/models.py`, `backend/app/db/models.py`, `backend/app/db/repositories.py`
- `backend/app/features/models.py`, `backend/app/features/repository.py`
- `backend/app/pipeline/models.py`, `backend/app/pipeline/repository.py`
- `backend/app/regime/engine.py`, `market_state.py`, `models.py`, `quality.py`, `repository.py`
- `backend/app/research/__init__.py`
- `backend/app/risk/engine.py`, `models.py`, `repository.py`
- `backend/app/shadow/repository.py`
- `backend/app/strategy/candidate_engine.py`, `credit_spread_engine.py`, `economics.py`, `economics_models.py`, `models.py`, `policy.py`, `repository.py`
- `backend/tests/test_phase14_2_strategy_logic.py`
- `scripts/run_alpha_experiments.py`

No frontend implementation, production environment, order functionality or migration 0013 was edited.

## 2. Migration changes

Additive revision `0014_phase14_2_1_replay_integrity` follows unchanged `0013_phase14_2_strategy_logic`. It adds only `option_contract_snapshots.depth_unit` (string, non-null, default `UNKNOWN`) and nullable `tick_size` (numeric). Confirmed raw metadata can round-trip; historical depth is never guessed or relabeled as units/lots. `UNKNOWN` describes absence of proof, not a numerical conversion.

Manifests, cost schedules, run identities and ledgers are persisted in a separate file namespace, so they do not require shared SQL tables/indexes. Upgrade/downgrade is tested on temporary SQLite, including old rows and a clean chain. PostgreSQL-compatible DDL does not establish PostgreSQL lock/concurrency safety. No migration was applied to a user/production database. Downgrade removes the two new metadata columns; export confirmed metadata first if it must survive a downgrade.

## 3. Independent risk repricing

When Phase 14.2 policy is selected, hard risk reconstructs short/long exact raw contracts independently of candidate credit. It derives width, observed executable credit, gross profit/loss, ratios, breakeven, confirmed-lot economics and the defined-risk capital proxy. It verifies strategy/candidate-set/regime versions and, for isolated integrity replay, run/hash/execution identity. Candidate comparisons use absolute tolerance `1e-6` and relative tolerance `1e-9`.

Mutating credit and every dependent payoff field cannot evade the raw repricing or monetary cap. Missing/duplicate identities, nonfinite numbers, pricing-basis mismatch and policy/version mismatch fail closed. Gross max loss keeps its existing meaning; `max_loss_including_estimated_costs_per_lot` is separately labeled estimated, never silently substituted for gross payoff loss. Replay monetary caps account for requested lots without changing approved absolute limits.

## 4. Quote/depth validity contract

Shared primitives separate exact identity, observation age, source age, skew, spread, tick validity and depth. Required observed bid/ask books must be finite, positive, uncrossed, and tick-aligned when metadata exists. No LTP/theoretical/synthetic fallback is permitted in the authoritative replay.

Default observation/source age limits are 30 seconds, interleg skew 10 seconds, future tolerance zero, and book spread limit 30%. These are explicit preregistered configuration, not a claim of market calibration. Candidate construction and risk reuse the run's time contract; their existing spread rules can tighten it. ATM context uses explicitly tighter spread rules. Fill checks apply to the actual information-availability time, not just collector wall time.

Each attempted quote records identity, observed side price, basis, source timestamp/age, observation age, requested lots/units, available units, depth unit/status, validity state and reason. Both required sides are inspected even when one fails.

## 5. Depth unit semantics

`UNITS`: compare available units against `requested_lots × confirmed_lot_size`.
`LOTS`: first convert available lots with that same confirmed lot size.
`UNKNOWN`: not executable-size evidence. Missing depth is likewise unknown, not infinite liquidity.

Entry needs short bid depth and long ask depth; exit needs short ask depth and long bid depth. Known zero/insufficient required depth is never erased by another missing/unrelated side. Default unknown depth is unevaluable. Explicit `allow_unknown_depth` produces only the distinct `UNKNOWN_DEPTH_SIMULATION` cohort. Its diagnostic outcomes never enter the global executable-depth gross/net evidence aggregates.

Existing ingestion is not asserted to prove depth units. Consequently historical chains defaulting to `UNKNOWN` may yield no primary executable fills; that is a data limitation, not a reason to infer units.

## 6. Path continuity and coverage

Preregistered defaults: expected interval 180 seconds, interval tolerance 30 seconds, maximum fill delay 240 seconds, maximum holding gap 240 seconds, session 09:18–15:27 IST. Session membership and exact dataset timestamps are frozen. Holidays/nonstandard sessions require explicit membership/session policy rather than fabricated observations.

Raw observations without a feature row remain in the export/manifest and coverage denominator. Misaligned feature identity/timestamp/expiry/spot is unavailable too. Session coverage counts expected cadence slots, unique observed slots, missing observations, usable slots, missing features, duplicate and off-cadence observations. Trade path coverage includes the entry baseline and expected holding observations. A missing expected snapshot/30-minute hole cannot become 100% coverage merely by discarding absent rows.

Information availability is `max(observation_timestamp, response_received_at)` when a valid receipt exists. Late/duplicate source-order observations are rejected; unavailable future observations cannot enter prior feature history. Chronological features are rebuilt from raw history using the frozen `FeatureEngineConfig`; stored feature hashes remain provenance, not trusted directional values.

## 7. Unresolved exposure

A filled trade with a missing feature/exit contract, excessive holding gap, invalid quote, session boundary or unfinished registered path remains `UNRESOLVED_EXPOSURE` with `PARTIAL_PATH`, entry truth, marks and reason. No invented exit, zero exposure or zero P&L is supplied. Gross/net final outcome stays unavailable. The affected daily monetary state is incomplete and further entries are blocked.

Closed gross-only trades also make subsequent monetary P&L state incomplete because realized net loss is unknown. Other sessions start their own daily state; no overnight position or fabricated next-session liquidation is modeled. Unresolved exposure stays visible through ledger open-exposure queries and denominators.

## 8. OI quality separation

Phase 14.2 static eligibility uses current finite nonnegative OI coverage, valid unique contract identities and valid snapshot/session evidence, with explicit 80% coverage and both CE/PE sides. Broker baseline change availability/nonzero status is separate. Zero or absent broker ΔOI does not gate static OI. Legacy intraday-OI semantics remain selected when Phase 14.2 is disabled.

Local ΔOI is diagnostic/context only: exact exchange/token/expiry/strike/type, same session, positive acceptable time interval, valid current/prior OI and valid known source time. A valid zero is retained as zero. No broker values are overwritten and no positive ΔOI is described as proof of writing. Full chain-wide local directional integration is not introduced.

## 9. Expected-move source policy

Exactly one fixed named source per manifest: `VIX_SCALED_EXPIRY_MOVE` or `ATM_STRADDLE_PREMIUM`. No silent AUTO fallback or pooled source switching. The source enters policy hash, manifest, attempts and analytics cohorts. Source failure is explicitly unevaluable.

VIX must be available, finite and positive. The straddle requires the supplied ATM to be nearest eligible strike to spot within an explicit default 25-point tolerance, same expiry, unique CE/PE identities, fresh source times, default 10-second skew, and spread limits of 10 points/20% (or stricter run limits). These are structural/premium proxies, not calibrated expected distributions. No IV/Greeks are fabricated.

## 10. Cost schedule and quantity accounting

`CostSchedule` freezes version, effective date bounds, fixed-per-order brokerage, turnover exchange/STT/GST/stamp/SEBI rules, per-executed-leg point slippage and per-component half-up two-decimal rounding. Supported rules are named; unknown models fail instead of being interpreted. No external rates were supplied from memory. Fixture rates are explicitly test-only.

Four orders with two lots remain four orders unless an explicit additional-order model is supplied. Variable charges scale by actual premium turnover and confirmed quantity. Sold premium is entry short plus exit long; bought premium is entry long plus exit short. Actual exit prices drive replay `ACTUAL` turnover. Candidate cost uses a labeled `ESTIMATED` equal-premium exit-turnover proxy, not known future prices.

Missing components, out-of-date schedules, unconfigured versions, false declaration or all-zero defaults produce `GROSS_ONLY`; total/net cost-dependent P&L remains unavailable. Inherited completeness is cleared. Output separates points per unit, gross rupees per lot, rupees for quantity, allocated net per lot, each fee, known partial cost, complete total and net. Gross-only scores are explicitly diagnostic, not validated family net ranking.

## 11. Frozen experiment manifest

Canonical deep-immutable JSON includes UUID run identity; full Git SHA; content hash of implementation files (including uncommitted code); strategy/integrity/policy versions; policy/config hash; raw identities/content hash; feature/alpha identity, version, mode and content provenance; event config/hash; dated cost schedule/hash; fixed move source; all three explicit split memberships; exact start/end; active/rejected axes; creation time; optional selected-policy/comparison authorization.

Runtime validates config, quantity, cost schedule, implementation content, ledger identity and every registered raw/feature/alpha record. Duplicate runtime IDs are rejected. New rows are ignored rather than moving existing sessions across splits. Source edits after registration require a new manifest. No claim is made that a hash alone validates the economic correctness of supplied data.

## 12. Holdout controls

The initial CLI allows only `TRAIN` and `VALIDATION`. `FINAL_TEST` cannot be selected via this CLI. Library-level authorization requires the explicitly selected single policy or a bounded preregistered policy-hash comparison protocol; unrestricted grids and mismatched policies are rejected. Future authorized exposure is append-only and detects overlapping final sessions within the same namespace using a serialized registry update.

Tests use synthetic temporary fixtures for authorization and overlap, not real held-out data. Moving to another namespace can evade a local exposure registry; governance must keep an authoritative common registry. Final test remains sealed for the next real replay.

## 13. Research isolation

Immediate approved design is separate output storage, not mixed production SQL. Namespace layout:

```text
research-output/<run UUID>/manifest.json
research-output/<run UUID>/<TRAIN|VALIDATION>/events/00000000.json ...
research-output/<run UUID>/<TRAIN|VALIDATION>/summary.json
research-output/final-test-exposures/<exposure UUID>.json
```

Every event is scoped to UUID, logic version, policy hash, execution mode and split. Each replay owns daily counts, realized P&L and open fingerprints. Ledger queries reconstruct scoped attempts/counts/open exposures/fingerprints/latest; another run has no access through that identity. Changing identity is rejected.

Shared candidate, regime, risk, shadow-create/update and pipeline repositories explicitly reject tagged research writes before touching a SQL session. Existing shared latest/list/count/duplicate endpoints therefore cannot ingest this replay's outputs or overwrite its open policy. Pipeline run keys do not collide because replay never creates shared pipeline records. Mixed-storage research is unsupported; it is not represented as tested/safe. Legacy shared repository regression coverage is retained.

## 14. Persistence identity

Regime persistence includes session, expiry, strategy logic, policy hash, reference identity/expiry, calculation mode and move-source policy. A changed identity resets consecutive-state approval. Candidates/regime/risk objects also preserve run/hash/mode so fill-time validation cannot switch policy silently.

Observation persistence is not independent statistical confirmation: adjacent overlapping 3-minute states remain dependent observations. Tests change each identity dimension and exercise the actual hard risk confirmation check.

## 15. Active/inactive axes

Declared active: widths, family mode (carry only when explicitly selected in research policy), entry window, strike buffer, credit/width minimum, move-distance minimum, carry-score floor, directional strength floor, DTE bucket, exit target/stop/time and hard-risk regime persistence. Statistical alpha threshold is active only when statistical alpha is selected.

Legacy alpha confirmations, legacy volatility multiplier, disabled-alpha threshold overrides and unknown axes are rejected. Singleton registration axes must match effective configuration rather than being a logged no-op; each manifest registers one policy, not an unbounded grid. Width and family tests exercise changed candidate construction/eligibility. No strategy weights/default thresholds were tuned.

## 16. Authoritative lifecycle

Authoritative evidence engine: `chronological replay_parameters / reconstructed candidate path` dispatched to isolated integrity replay. At each causal observation it rebuilds available features/state, generates candidates, performs hard risk and preserves decision truth. An approved decision attempts entry only at the next valid cadence observation within TTL and entry window. It validates exact executable depth and reruns candidate eligibility/hard risk at fill time without overwriting decision-time fields.

Holding observations mark exact executable exit books and evaluate the shared target/stop/opposite-regime/structure/time rules. Trigger and liquidation use that same observed book, explicitly `CONTEMPORANEOUS_OBSERVED_BOOK_LIQUIDATION`; entry is `NEXT_OBSERVATION_SIMULATION`. Neither is a broker fill claim. Daily entry count observes both risk and shadow limits. Missing paths stop affected-state entries rather than allowing clean-looking continuation.

Legacy replay is labeled `LEGACY_DIAGNOSTIC_REPLAY`, `evidence_eligible=false`, and is not pooled/evidence-equivalent. The previous experiment CLI rejects Phase 14.2 evidence use and points to the frozen-manifest runner. Legacy behavior while Phase 14.2 is disabled is preserved, apart from additive diagnostic metadata.

## 17. Initial 0DTE restriction

The initial integrity configuration rejects `allow_0dte=true`. Expiry-day/past-expiry entry attempts are `RESTRICTED` with `ZERO_DTE_EXCLUDED`; fill-time hard risk also disallows expiry-day entry. This is declared research policy, not accidental missing arithmetic. No overnight logic is added.

## 18. Immutable ledger

Exclusive-create numbered events preserve observations, causal feature reconstruction, all decision attempts, fill attempts, fill-time validation, entry execution, path marks, exit attempts and final outcomes. Decision metadata and fill recomputation stay separate. Events form a verified sequence/hash chain. A summary seals event count and terminal hash with its own content hash; sealed ledgers reject append. Duplicate run/split creation and summary overwrite fail.

Tampering, wrong identity, changed content, broken chain and sealed-tail truncation are detected. This is application-level append-only/tamper-evident storage, not OS write-once media or protection against an administrator rewriting all hashes. Backups/access controls remain operational requirements. Concurrent replay writers for a split are unsupported and exclusive creation fails closed.

## 19. Analytics

Explicit denominator counts: decisions, approvals, fill attempts, entries, closed evaluable trades, unresolved, no fill, invalid fill, missing path, gross-only, primary net-complete and incomplete-state blocks. Coverage shows expected/observed/usable/missing slots rather than survivors alone. Unknown-depth simulations have separate counts and cohort diagnostic metrics and cannot enter primary aggregates.

Summary separates gross points per unit, gross rupees per lot/quantity, complete total costs, net points per unit and net rupees for quantity. Gross-only cannot enter net aggregates. Closed cumulative gross/net drawdown is separate from explicitly all-depth diagnostic sampled marked-equity gross drawdown. The marked-equity aggregate is unavailable when exposure is unresolved, rather than assuming it contributes zero. MAE/MFE starts at zero entry baseline and is sampled, not continuous intrainterval extrema.

Breakdowns: family, directional strength, survival/carry buckets, DTE, width, move distance, expiry stress, move source, depth status and cost completeness. Exit constants match shared lifecycle rules. No profitability/production-readiness claim is emitted.

## 20. Terminology

New evidence outputs describe premium/structural proxies, expiry stress and gross/net units explicitly. Large sample count is `LARGE_SAMPLE_DIVERSE_SESSIONS`, not `STRONG_CANDIDATE` or calibrated confidence. Existing analytical fields remain backward compatible; no broad UI redesign or true theta/gamma/survival probability claim is introduced.

## 21. Flags/defaults

Existing defaults remain false: alpha engine, statistical-alpha regime input, volatility buffer, AI pipeline, Phase 14.2 strategy logic and theta carry. New defaults: integrity disabled, unknown depth disabled, 0DTE disabled, cadence 180s, tolerance 30s, fill TTL/path gap 240s. Only example env files/config declarations changed; actual environment files were not edited.

The isolated CLI does not load application Settings, secrets, database connections or external clients. It accepts explicit offline JSON configuration and frozen data. Production code does not auto-select it from the new flags.

## 22. Backend validation

Final full regression: **791 passed**, one existing Starlette/httpx deprecation warning, in **142.15 seconds**. This includes all **656 baseline cases** and **135 new integrity cases**. Earlier failures were corrected (obsolete broker-ΔOI eligibility expectation, historical-schema ORM seeding, and integer/float singleton-axis equivalence); the final suite is green. No tests were skipped or marked expected-failure to obtain this result.

`python -m compileall -q backend/app scripts worker alembic`, import smoke (`app.main`, isolated replay and risk repricing), and `git diff --check` passed. Migration 0013 has an empty diff. Temporary SQLite clean/existing-row upgrade/downgrade and raw metadata round-trip passed; PostgreSQL **offline** upgrade DDL compilation passed without a server connection. No PostgreSQL runtime/concurrency/lock test was performed. The credential-free CLI registration/TRAIN test passed and rejects FINAL_TEST at argument parsing.

The regression command uses a process-local dummy consumer key and local paths:

```powershell
$env:KOTAK_CONSUMER_KEY='offline-test'
$env:PYTHONPATH='.;backend'
.\.venv\Scripts\python.exe -m pytest -o addopts='' -q
```

Tests use mocks, raw-data fixtures and temporary SQLite/file namespaces only. Historical-schema seeding uses reflected Core tables rather than today's ORM against an older revision.

## 23. Frontend and Docker validation

Frontend `npm test`: **29 tests passed across 3 files**. `npm run lint`: **passed**. `npm run build`: **passed**, including optimized compilation, TypeScript and all six static pages. No frontend source was changed.

`docker compose config --quiet` passed. `docker compose --env-file .env.production.example -f docker-compose.prod.yml config --quiet` passed with dummy example configuration; resolved configuration/secrets were not printed. Local image builds could not run: Docker CLI is installed but the Docker Desktop Linux daemon/named pipe is absent. No service was started and no production operation was attempted.

## 24. Known limitations

- Independent audit is still required; no actual family/width replay, threshold tuning or profit optimization was run.
- Historical raw depth units/source times may be unproven/missing. Strict primary fills then remain unevaluable. Do not relabel UNKNOWN or substitute data to obtain a result.
- Costs require an explicitly approved dated schedule. Test rates are not financial/tax guidance and must not be copied into a real schedule. Estimated scoring turnover differs from actual liquidation turnover.
- VIX lacks an independent per-index quote timestamp in the existing raw model; its value is snapshot context, not independently verified synchronous index execution evidence. Option straddle quotes do have explicit source checks.
- Features are reconstructed causally from registered raw history. Alpha ranks/provenance are supplied audited inputs; when selected, relabeling/identity checks do not independently refit statistical reference models. Their upstream causal generation must be audited before evidence use.
- Missing raw input outside the registered session window cannot be inferred. Frozen full intended session boundaries must be supplied; shortened windows must not be described as full-session coverage.
- Exit execution is sampled same-book liquidation; queue position, deeper order books, intrainterval extrema and real fills are unknown. Explicit slippage is not proof of actual execution.
- File isolation is supported; mixed shared production/research SQL and concurrent same-split writers are not. Shared APIs do not serve these files. Final exposure governance is namespace-local, not a global cross-machine registry.
- Hash chaining/sealing is not administrator-proof WORM storage. Interrupted runs retain recorded entry exposure and failure events, not a fabricated complete summary; operators must treat them as incomplete.
- Research-only SQLite timezone restoration assumes collector wall times are IST and SQLite server-created timestamps are UTC; this is not evidence of every upstream source timezone. Legacy timestamp mapping remains unchanged. PostgreSQL concurrency/lock safety and local container builds were not exercised without a running daemon.

## 25. Exact next step (not Phase 15)

Independent audit of Phase 14.2.1 first. If accepted, preregister isolated TRAIN/VALIDATION chronological replay from offline stored raw/feature/alpha exports (including raw rows without features), an approved cost schedule, fixed source, session membership and policy configuration. Compare strategy families and widths only through reconstructed causal replay. Keep production flags disabled and FINAL_TEST sealed. Do not deploy or advance to Phase 15.

After that audit approval, the local-only commands are:

```powershell
python scripts/run_integrity_replay.py --dataset-json <offline-export.json> --configuration-json <preregistered-config.json> --namespace research-output --register
python scripts/run_integrity_replay.py --dataset-json <offline-export.json> --namespace research-output --run-id <registered-UUID> --split TRAIN
python scripts/run_integrity_replay.py --dataset-json <offline-export.json> --namespace research-output --run-id <registered-UUID> --split VALIDATION
```

The dataset is an array of `{snapshot_id, snapshot, feature_id, feature, alpha}`; absent features/alpha are null. Registration requires `parameters`, `strategy`, `risk`, `regime`, `shadow`, `integrity`, `cost_schedule`, `expected_move_source`, `sessions` and optional `event_config`, `requested_lots`, singleton `experiment_axes`. Configurations are typed dataclasses (times ISO `HH:MM:SS`, dates ISO dates). The three research policies must match. Registration emits UUID/hash and freezes configuration; replay reads the registered configuration and rejects edits. The CLI test exercises a complete fixture register/TRAIN round-trip without application credentials/clients. No such commands were run on real user data in this task.
