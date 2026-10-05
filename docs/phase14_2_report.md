Phase 14.2 implements versioned, opt-in intraday credit-spread research in the Codespace. No deployment, production database access, broker request, live trading request, order execution, AI API call, or notification send was performed. All new decision flags remain disabled by default.

1. **Files changed.** The complete file manifest follows this report. The main additions are `strategy/policy.py`, `strategy/economics_models.py`, `strategy/economics.py`, `strategy/credit_spread_engine.py`, `regime/market_state.py`, the migration, candidate economics UI, and backend/frontend tests. Existing engines delegate to the new path only when enabled. Repositories, APIs, pipeline metadata, scripts, AI input, shadow entry and analytics use explicit versions.

2. **Migration.** `alembic/versions/0013_phase14_2_strategy_logic.py`, revision `0013_phase14_2_strategy_logic`, follows `0012_phase14_1`. It adds nullable `strategy_logic_version` and `strategy_context_json` plus version indexes to regime, candidate-set and shadow tables; regime bias/strength/family eligibility columns; and candidate-set/shadow family columns. Candidate economics remain individually named inside versioned JSON. No historical backfill or relabeling occurs. Fresh temporary SQLite migration and an upgrade containing an existing legacy regime row both passed. No production migration was applied.

3. **Market-bias model.** `BULLISH`, `BEARISH`, `NEUTRAL`, `CONFLICT`, `INSUFFICIENT` describe market evidence independently of trade permission. Outputs also expose alpha, positioning, price structure, basis, volatility and data-quality states. Missing acceptable quality/history, session or expiry continuity, usable OI, or valid identity-matched alpha when alpha integration is selected gives `INSUFFICIENT`. Balanced substantial opposing evidence, rank/sign conflict or strong alpha contradiction gives `CONFLICT`.

4. **Directional-strength model.** `STRONG`, `MODERATE`, `WEAK`, `NONE` derive from capped deterministic evidence. With bull/bear/range weighted scores B/S/R, directional score is `100 * abs(B-S)/max(B+S+R,1) * min(1,max(B,S)/DIRECTIONAL_EVIDENCE_SCALE)`. The default evidence scale is 5; research boundaries are 70/40/15. The absolute-evidence term prevents a tiny unopposed signal from appearing strong. Confidence remains a separate legacy evidence diagnostic. All thresholds are configurable. Broker-reported ΔOI is kept out of the new directional voting path; correlated price and OI votes retain caps, and unverified basis remains context.

5. **Strategy-family model.** Families are `DIRECTIONAL_CREDIT_SPREAD`, `THETA_CARRY_CREDIT_SPREAD`, `NO_TRADE`; eligibility is `DIRECTIONAL_ONLY`, `THETA_CARRY_ONLY`, `BOTH`, `NONE`. Strong or moderate aggregate directional evidence can qualify without confirmed strong alpha. `DIRECTIONAL_ALPHA_MIN_STRENGTH` controls the directional-strength policy floor; alpha is an input, not a universal prerequisite. Carry requires its own flag, economics and side checks. Strong opposing alpha exceeds the default carry opposition limit of MODERATE and vetoes carry. If BOTH produces the same physical spread twice, only its highest-ranked family interpretation survives; family-specific replays compare alternatives without duplicate-position collisions.

6. **Survival-score formula/components.** Components are clipped to 0..1 and weighted, then multiplied by 100. Default weights: distance 15%, distance beyond structure 20%, expected-move distance 20%, volatility 15%, local static OI structure 10%, liquidity 10%, DTE 5%, gap to opposing structure 5%. Distance uses `distance/(3*required_buffer)`; structure uses `distance_beyond_structure/required_buffer`; expected-move uses `distance_units/2`; opposing structure uses `gap/(3*expected_move)`; DTE uses fractional days/gamma moderate-DTE threshold. Volatility combines the VIX bucket, collector-observed range and recent adverse movement. Liquidity combines relative OI and observed bid/ask spread. OI levels are observed structure, not guarantees or fresh-writing claims. Persisted points, percentages, components and context remain visible. The score is uncalibrated survival quality, not probability.

7. **Expected-move methodology.** VIX proxy: `spot * (VIX/100) * sqrt(fractional_calendar_days/365)`. ATM straddle proxy: same-expiry ATM CE midpoint plus PE midpoint, only with unique contracts, valid positive uncrossed quotes, instrument identity, fresh source timestamps and at most 10 seconds of leg skew. AUTO prefers a valid straddle and falls back to VIX; explicit sources fail closed if unavailable. Neither proxy is a probability distribution. Source, points, percent and strike distance in expected-move units are persisted. The minimum-distance gate is unset by default, while distance still affects scores.

8. **DTE methodology.** Calendar DTE is expiry date minus observation date in Asia/Kolkata. Fractional days are actual seconds until 15:30 IST on expiry divided by 86,400. Expiry at or before the observation is rejected. Session minutes remaining are stored separately. Configurable bucket upper bounds default to `[0,1,3,7]`, producing `LE_0_DTE`, `LE_1_DTE`, `LE_3_DTE`, `LE_7_DTE`, `GT_7_DTE`. These are research buckets, not optimized parameters or an overnight holding policy.

9. **Carry-score formula/components.** The default weighted score is 30% cost-adjusted credit/width, 25% cost-adjusted credit/max loss, 15% cost-adjusted credit/fractional DTE, 15% premium retention, 15% expected-move distance. Ratios are clipped against explicit research targets (.15, .20, 20 points/day, .50 retention, 2 move units respectively). Gross spread credit, credit/width, credit/max loss, credit/expected move and credit/DTE are retained separately from costs and adjusted components. `carry_model=TRANSPARENT_CARRY_PROXY_NO_GREEKS`. Brokerage, exchange charges, STT, GST, stamp duty and four-leg round-trip slippage are exposed individually. Candidate costs assume exit turnover equal to entry turnover; replay uses actual observed buy/sell turnover for Phase 14.2 tax estimates. Rates are user-supplied research inputs. Costs default to incomplete, and missing lot size cannot produce a complete monetary estimate.

10. **Theta/gamma balance.** Gamma proxies are LOW/MODERATE/HIGH/EXTREME. Defaults: EXTREME when fractional DTE <= .25 and distance < .5 move units; HIGH when DTE <= 1, distance < 1 unit or adverse movement exceeds its configured stress scale; MODERATE when DTE <= 3 or VIX is elevated/high; otherwise LOW. Ranking applies severity plus an extra near-expiry penalty, so accelerated carry is not automatically attractive. Balance is EXTREME_RISK for extreme gamma, UNFAVORABLE for high gamma, FAVORABLE for sufficiently strong survival and carry, otherwise BALANCED. Extreme candidates are excluded. Missing volatility context fails closed. No actual theta, gamma, IV or Black-Scholes Greeks are synthesized.

11. **300/400 width handling.** Width is actual same-expiry leg strike difference. All configured positive widths are considered, including 100/150/200/250/300/350/400 when quotes exist. Net credit is short bid minus long ask, with existing explicitly marked LTP fallback. `max_loss_per_unit=width-credit`; lot loss and capital proxy multiply by confirmed lot size. Both 300 and 400 have construction, payoff, approval, monetary-veto and exact-quote replay tests. Existing `RISK_MAX_SPREAD_WIDTH=200` stays unchanged; research users must explicitly configure a higher cap to approve larger widths. Capital and loss limits never increase automatically.

12. **Candidate-ranking formula.** `max(0, 100*weighted_mean(components)-sum(penalties))`. Defaults: survival 30%, carry 25%, directional alignment 10%, structure 10%, liquidity 15%, credit quality 10%. Default maximum penalty scales are gamma 20, volatility stress 10, loss burden 10, cost burden 10, poor move-distance 10; loss burden scales against configured per-lot research capacity. Both raw components and applied penalty amounts are persisted. Width alone is never a ranking preference.

13. **Adaptive strike-distance logic.** Base buffer is `max(min_points, spot*min_percent/100)`. Default strength/family multipliers are strong 1, moderate 1.5, carry 2; elevated/high VIX multiplies by 1.25/1.5; low DTE multiplies by 1.5. All are policy settings. Moderate survival/carry floors default to 60/30 versus strong 45/20; carry requires 70/40. An optional expected-move gate can add distance. Short legs remain OTM and respect configured structural references. Explicit existing high-VIX veto policy is honored when configured.

14. **Neutral-market side selection.** Each side gets an exposed score from nearby structural-level proximity in move units (35%), local OI cluster strength (35%), recent adverse movement (20%) and volatility (10%). Default required safety is 55 and required asymmetry margin is 10. Weak/neutral carry uses only a clearly safer side: downside selects bull put, upside selects bear call. Symmetry, missing observed movement or no adequate structure produces no candidate. Moderate/strong bias may supply the side, subject to opposing-alpha veto and candidate survival/carry requirements. Tests cover both sides, symmetry, weak and neutral strength.

15. **Risk-engine changes.** Phase 14.2 validates current family eligibility and side instead of imposing a directional-confidence/alpha-strength-only veto. Consecutive state confirmation remains, including same-session strictly chronological timing and maximum gaps. Exact-contract quote prices must match the candidate's observed fields and satisfy validity/freshness. Existing max loss, capital, daily loss, max trades, duplicate-position, width, time-window, expiry, liquidity, pricing, structure, evidence-quality, stability and configured-event checks remain vetoes. The default legacy path retains its previous confidence checks. There is no live order authority.

16. **Shadow metadata.** Entry records persist family, bias, strength, directional/survival/carry scores and components, expected move and distance units, calendar/fractional DTE and bucket, width, structure distance, buffer, gamma/balance, cost components and completeness. Source candidate and approved risk decision remain attached. Entry risk state is APPROVED only after approval. Database round-trip is tested. Existing same-day exits remain unchanged.

17. **Analytics changes.** Breakdowns include logic version, family, directional strength, survival and carry buckets, DTE bucket, move-distance bucket, gamma risk and theta/gamma state. Width breakdown retains separate 100/200/300/400 groups. Historical entries remain LEGACY_UNCLASSIFIED/UNAVAILABLE where appropriate. Pipeline related IDs and regime/candidate/risk daily counters select the requested logic version, avoiding mixed-version counts.

18. **Experiment changes.** Bounded grids and CLI support allowed widths, buffer, expected-move minimum, carry floor, directional-strength floor, family mode and DTE bucket. All use chronological reconstruction and deterministic risk with current data. Newly constructed DTE candidates are constrained during replay; incompatible recorded historical outcomes are never filtered into alternative-strategy P&L. Replay retains causal alpha continuity, exact contracts, next-observation fills, timestamps, skew, depth, same-day exits, boundary purging, sealed final-test auditing and NOT_EVALUABLE missing paths. Phase 14.2 revalidates construction/risk on the fill observation and scales monetary limits for quantity. Strong confirmed alpha is not a universal replay prerequisite on the new path. Configured events remain usable. No experiment or production-data replay was run against a live database in this task.

19. **AI-agent changes.** New inputs include current deterministic family and candidate economics, with explanatory-only authority. Pipeline AI preparation computes the same pure candidate function before commentary when needed. Phase 14.2 uses `phase14_2_ai_v1`/`phase14_2_prompt_v1`; legacy prompt, payload and AI versions remain selected when disabled. Prompt guidance covers moderate conviction, survival/carry, intentional wider spreads, gamma exposure, incomplete costs and prohibited profitability/probability claims. AI cannot select, rerank, override risk or authorize a trade. Tests exercise the payload/prompt locally; no OpenAI call was made.

20. **Feature flags.** Settings and production examples keep `ALPHA_ENGINE_ENABLED=false`, `REGIME_USE_STATISTICAL_ALPHA=false`, `STRATEGY_VOLATILITY_BUFFER_ENABLED=false`, `PIPELINE_RUN_AI_RESEARCH=false`, `PHASE14_2_STRATEGY_LOGIC_ENABLED=false`, `THETA_CARRY_ENABLED=false`. The new path is versioned `phase14_2_v1`. Legacy `REGIME_REQUIRE_ALPHA_FOR_DIRECTIONAL` remains available for historical/default logic and is not a prerequisite on the new path. New widths and family settings affect construction only behind the Phase 14.2 flag. No runtime environment or production settings were edited.

21. **Backend validation.** 656 backend tests passed: 516 prior tests plus 140 Phase 14.2 cases. Executed with a dummy consumer key and local import paths: `KOTAK_CONSUMER_KEY=offline-test PYTHONPATH=.:backend python -m pytest -o addopts='' -q`. Tests use fixtures/mocks and temporary SQLite, including migration/history retention. The existing Starlette/httpx deprecation warning remains. Python compilation and `git diff --check` passed.

22. **Frontend and image validation.** `npm test`: 29 passed; `npm run lint`: passed; `npm run build`: passed. Both development and production `docker compose config` passed; production config used a temporary dummy env file. Local backend and frontend production image builds passed. The refreshed backend image also passed a network-disabled default-flags/legacy-width smoke check. No Compose application services were started or deployed. Existing npm dependency audit findings were not changed by this implementation.

23. **Known limitations.** Scores and defaults are uncalibrated research heuristics, with correlated components exposed rather than claimed to be independent evidence. VIX/straddle are context proxies; no probability, expected-return, full implied distribution or actual Greeks are estimated. Session-observed range and snapshot motion are coarse volatility proxies; premium-convexity and historical premium-decay scoring are not added. OI context is static local structure; broker baseline ΔOI does not become fresh-writing evidence. Costs require supplied rates, an estimated exit-turnover assumption at ranking time and complete quote/lot data. Three-minute marks can miss adverse intrainterval moves. Missing quote paths remain NOT_EVALUABLE; no live profitability or validated tuning claim is made. Basis synchronization remains unverified context. No overnight strategy or iron condor was added.

24. **Default production behavior.** The new regime/candidate/risk path is bypassed while the Phase 14.2 flag is false, and theta carry requires its own flag. Default width set, legacy versions, candidate selection, risk confidence policy and intraday shadow exits stay selected; regression tests verify this. Additive schemas/API metadata and analytics fields are intentional changes, so payloads are not promised to be byte-identical. Production itself was untouched. This is a code-path and regression guarantee under the existing valid configuration, not a claim that a future deployment or altered configuration has been tested on production.

25. **Exact next step before Phase 15.** Review this patch and the additive migration, then run a preregistered Codespace-only chronological replay on stored data with Phase 14.2 enabled in an isolated research environment. Compare 100/200/300/400 widths, directional versus carry families, survival/carry/DTE/gamma buckets, net costs, missing-path coverage, drawdown and holdout stability. Configure wider risk caps only with unchanged approved monetary limits; enable carry explicitly only for research. Require adequate data and sample coverage and validate policy on training/validation before inspecting the sealed final test. Keep production flags off. Phase 15 begins only after the credit-spread foundation and intraday exit behavior are accepted; no production deployment is implied by this next step.

Complete file manifest:

```text
.env.example
.env.production.example
README.md
alembic/versions/0013_phase14_2_strategy_logic.py
backend/app/ai/input_builder.py
backend/app/ai/models.py
backend/app/ai/openai_provider.py
backend/app/ai/prompts.py
backend/app/ai/repository.py
backend/app/ai/service.py
backend/app/alpha/experiments.py
backend/app/alpha/replay.py
backend/app/api/ai_research.py
backend/app/api/dashboard.py
backend/app/api/regime.py
backend/app/api/risk.py
backend/app/api/shadow.py
backend/app/api/strategy_candidates.py
backend/app/core/config.py
backend/app/db/models.py
backend/app/notifications/router.py
backend/app/pipeline/repository.py
backend/app/pipeline/service.py
backend/app/regime/engine.py
backend/app/regime/market_state.py
backend/app/regime/models.py
backend/app/regime/repository.py
backend/app/risk/engine.py
backend/app/risk/limits.py
backend/app/risk/repository.py
backend/app/risk/service.py
backend/app/shadow/analytics.py
backend/app/shadow/entry.py
backend/app/shadow/models.py
backend/app/shadow/repository.py
backend/app/strategy/candidate_engine.py
backend/app/strategy/credit_spread_engine.py
backend/app/strategy/economics.py
backend/app/strategy/economics_models.py
backend/app/strategy/models.py
backend/app/strategy/policy.py
backend/app/strategy/repository.py
backend/app/strategy/service.py
backend/tests/test_phase14_2_strategy_logic.py
docs/phase14_2_report.md
frontend/components/candidate-economics.tsx
frontend/components/dashboard.tsx
frontend/tests/candidate-economics.test.tsx
frontend/types/index.ts
scripts/build_ai_research.py
scripts/build_regime.py
scripts/build_risk_decisions.py
scripts/build_shadow_trade.py
scripts/build_strategy_candidates.py
scripts/run_alpha_experiments.py
scripts/run_shadow_replay.py
scripts/update_open_shadow_trades.py
```
