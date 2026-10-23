# NIFTY responsive release v2 — reconciliation

**No deployment performed.**

- Release branch: `release/nifty-responsive-v2`.
- NIFTY backend/policy base: `dc9ca0493f1225a86c3b521bc7dc2713861bf249`.
- Responsive frontend source: `a02a7bd926dbe8fcb7a0b79b32a578351417f8bd`.
- Method: isolated worktree from the exact NIFTY base; imported frontend files, not source-branch ancestry or a merge.

## Runtime boundary

All 253 protected base files under backend runtime, migrations and workers, plus deployment/configuration inputs, were compared byte-for-byte. The only runtime differences are:

1. `backend/app/parallel_paper/monitor.py`: allowlisted stored ACTS/EMA/Supertrend diagnostics and direction. No evaluation or market calculation.
2. `backend/app/parallel_paper/trade_monitor.py`: stored mirror exit request reason and frozen forced-exit clock. No execution or journal writes.

`backend/app/parallel_paper/orb_evidence.py` is identical to the NIFTY base. Exact stored futures ranges, cursor-bound futures prices, frozen trade ranges and separate option execution evidence are retained.

ACTS cap six, scores and signals, ORB windows/confirmation/re-entry, EMA, Supertrend, MIRROR_EXIT_V1, PHASE15_EXIT_BOOK_V1, exact 400-point spreads, entry/exit prices, risk limits and CONTRACT_EXPIRED_UNPRICED lifecycle remain byte-identical to the base implementation. Historical trades/events are untouched. This remains PAPER-only and adds no broker-order path.

### Explicit exclusions

The following are absent, not merely disabled:

- `alembic/versions/0020_commodity_acts_research.py`
- `alembic/versions/0021_commodity_paper_trades.py`
- All Commodity migrations; migration head remains `0019_parallel_paper`.
- `backend/app/commodity_acts/` (including Commodity models, broker integration and PAPER execution).
- `backend/app/api/commodity_acts.py`; no Commodity routes or tables registered.
- `scripts/deploy_commodity_automation.py`.
- `worker/commodity_acts_*` and `worker/commodity_session_*`.

`backend/app/main.py`, all NIFTY workers, all Alembic files, both Compose files, environment templates, requirements and Caddy configuration are identical to the base. No production files or credentials were read or changed.

Read-only Commodity frontend pages/types and the authenticated GET-only monitoring bridge are retained to preserve the unified deployed frontend lineage. They do not supply execution code, workers, models or migrations. Missing Commodity endpoints show UNAVAILABLE/N/A; failure does not hide NIFTY exposure or fabricate combined portfolio totals. No Commodity origin, credentials, service or activation configuration is installed by this branch.

## Frontend import

The source frontend tree is preserved, including light/dark styles, fonts, mobile navigation, positions/trades, strategy-specific evidence, responsive records, visibility-aware polling and regression tooling. Release-specific frontend differences are an additional browser-validation scenario for absent Commodity endpoints and removal of an inherited blank line at EOF in the cross-market component (no runtime behavior change).

Changed/imported frontend files relative to the NIFTY base (66 files):

- `frontend/Dockerfile`
- `frontend/app/commodity-acts/[instrument]/page.tsx`
- `frontend/app/commodity-acts/page.tsx`
- `frontend/app/design-system.css`
- `frontend/app/globals.css`
- `frontend/app/layout.tsx`
- `frontend/app/manifest.ts`
- `frontend/app/monitor-data/[source]/[...path]/route.ts`
- `frontend/app/monitoring.css`
- `frontend/app/nifty/page.tsx`
- `frontend/app/nifty/research/page.tsx`
- `frontend/app/page.tsx`
- `frontend/app/phase15/page.tsx`
- `frontend/app/positions/page.tsx`
- `frontend/app/trades/page.tsx`
- `frontend/components/candidate-economics.tsx`
- `frontend/components/charts.tsx`
- `frontend/components/commodity-acts.tsx`
- `frontend/components/commodity-paper.tsx`
- `frontend/components/cross-market-overview.tsx`
- `frontend/components/dashboard.tsx`
- `frontend/components/data-table.tsx`
- `frontend/components/history.tsx`
- `frontend/components/icons.tsx`
- `frontend/components/navigation.tsx`
- `frontend/components/phase15-monitor.tsx`
- `frontend/components/phase15-orb-evidence.tsx`
- `frontend/components/phase15-paper-trades.tsx`
- `frontend/components/portfolio-evidence.tsx`
- `frontend/components/responsive-table.tsx`
- `frontend/components/shadow-pages.tsx`
- `frontend/components/snapshot-detail.tsx`
- `frontend/components/system-health.tsx`
- `frontend/components/theme-toggle.tsx`
- `frontend/components/trading-workspace.tsx`
- `frontend/components/ui.tsx`
- `frontend/components/unified-overview.tsx`
- `frontend/hooks/use-polling.ts`
- `frontend/lib/api.ts`
- `frontend/lib/monitoring.ts`
- `frontend/lib/portfolio.ts`
- `frontend/lib/read-only-bridge.ts`
- `frontend/package-lock.json`
- `frontend/package.json`
- `frontend/public/favicon.svg`
- `frontend/public/fonts/Inter-LICENSE.txt`
- `frontend/public/fonts/InterVariable.woff2`
- `frontend/scripts/validate-responsive.mjs`
- `frontend/tests/api-sources.test.ts`
- `frontend/tests/commodity-acts.test.tsx`
- `frontend/tests/commodity-paper.test.tsx`
- `frontend/tests/dashboard.test.tsx`
- `frontend/tests/design-system.test.tsx`
- `frontend/tests/fixtures/monitoring.json`
- `frontend/tests/phase15-monitor.test.tsx`
- `frontend/tests/phase15-paper-trades.test.tsx`
- `frontend/tests/pwa.test.ts`
- `frontend/tests/read-only-bridge.test.ts`
- `frontend/tests/responsive-evidence.test.tsx`
- `frontend/tests/route-isolation.test.tsx`
- `frontend/tests/trading-workspace.test.tsx`
- `frontend/tests/unified-overview.test.tsx`
- `frontend/tests/use-polling.test.tsx`
- `frontend/types/commodity-acts.ts`
- `frontend/types/commodity-paper.ts`
- `frontend/types/index.ts`

## Test changes

- `backend/tests/test_phase15_monitor.py`: stored diagnostic projection evidence.
- `backend/tests/test_phase15_trade_monitor.py`: frozen management projection evidence.
- `backend/tests/test_phase13_deployment.py`: assertion updated for the imported frontend's existing source-specific read-only API routing.
- `backend/tests/test_nifty_release_boundary.py`: excluded files/workers, Alembic head, API routes and metadata boundary. Uses resolved OpenAPI routes to support FastAPI's lazy router representation.

## Validation evidence

The completed validation results and responsive measurements are recorded below. Browser fixtures are synthetic; no production database, broker API, orders, worker or service is used. The local browser harness intercepts every API GET, rejects unexpected requests and all mutation methods, and refuses a nonlocal preview URL.

### Final results

| Check | Result |
|---|---|
| Full backend suite | 1,695 passed; 8 skipped; 0 failed; one existing Starlette deprecation warning |
| Focused release/API/projection tests | 56 passed |
| Full frontend suite | 141 passed across 17 files |
| Affected Python flake8 / mypy | PASS |
| ESLint / TypeScript | PASS |
| Production frontend build | PASS |
| Responsive browser matrix | 196 route/viewport/theme combinations passed |
| Diff checks | PASS |

The lower backend count than the unified source branch reflects exclusion of Commodity tests/runtime. No Commodity implementation was imported to inflate the NIFTY test suite.

Seven viewports: 1440×900, 1280×800, 1024×768, 768×1024, 430×932, 390×844, 360×800. Fourteen routes in both themes: `/`, `/phase15`, `/nifty`, `/positions`, `/trades`, `/nifty/research`, `/history`, `/history/1`, `/shadow`, `/shadow/7`, `/system`, `/commodity-acts`, `/commodity-acts/CRUDEOILM`, `/commodity-acts/NATGASMINI`.

At each of 360/390/430px, all 28 route/theme renders have zero page overflow, browser errors, unexpected requests or detected clipped financial values. Open Positions begins at 610px at 390px in both themes (overview and Phase 15 monitor), retaining the responsive source behavior.

Representative evidence covers OPEN/profit/loss, CLOSED, UNRESOLVED, EXPIRED UNPRICED, ORB inside/above/below, blocked, stale/unavailable and absent Commodity endpoints. Six extra state scenarios plus navigation/theme/filter interactions passed. Commodity HTTP 404s leave NIFTY exposure accessible, with missing combined metrics N/A and Commodity pages explicitly unavailable. No real fills or market observations are claimed.

Final evidence: `/workspaces/nifty-responsive-release-20261023/browser-final/` (screenshots and `results.json`). Source-boundary checks, SHA-256 inventories and validation logs are in `/workspaces/nifty-responsive-release-20261023/`. An earlier browser run interrupted after local generated directories disappeared is retained separately and is not counted; the final run used freshly restored lockfile dependencies and a complete rebuilt preview.

No real-device or timed trader-comprehension study is claimed. No deployment, production access, migrations, worker activation or trading-state changes occurred.
