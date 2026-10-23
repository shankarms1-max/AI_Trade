# Responsive trading dashboard v2

## Source and scope

Integration branch: `feat/responsive-trading-dashboard-v2`.
Base: unified dashboard `71fd9e4c39a85096ca1e6e02fc8d48ea8919638e`.
ORB observability port: `dc9ca0493f1225a86c3b521bc7dc2713861bf249`.

The newer unified lineage and its independent read-only Commodity API source are preserved. The root view prioritizes NIFTY exposure; the existing cross-market portfolio, charts and full journals remain available in an on-demand disclosure. Market research remains available at `/nifty/research`. Commodity automation is unchanged.

The only backend runtime changes are read-only projections in `parallel_paper/monitor.py`, `trade_monitor.py` and `orb_evidence.py`. Stored decision diagnostics and exact cursor-bound futures evidence are exposed without evaluating a strategy. Entered ORB trades use their stored historical features. Missing session clock boundaries are derived from the existing session definition; market highs/lows are never recomputed. Option execution evidence remains separate from futures levels.

No scoring, signals, windows, confirmation, re-entry, daily trade cap, risk, pricing, spread width, mirror exits, expiry lifecycle, journal writes, workers, schema, production configuration or live-order paths changed. No deployment or production access was performed.

## Information hierarchy

- Compact health, futures and observation status followed by gross accounting and exposure counts.
- Six strategy states remain visible above Open Positions; detailed evidence follows exposure.
- Explicit unresolved attention; unknown combined marks remain N/A even when realized P&L is known.
- Position cards show exact contracts, captured gross mark/debit, holding time and stored management rules. Older marks are labelled historical. Missing quotes are never substituted.
- Strategy rows expose distinct ACTS, ORB, EMA and Supertrend evidence, with diagnostics in disclosures.
- ORB tabs show stored NIFTY futures ranges; trade details preserve frozen historical ranges and separate short/hedge option fills and captured executable marks.
- Five-item mobile navigation, keyboard-accessible More dialog, responsive trade/research records, accessible mobile sorting, shared light/dark styling and tabular figures.
- UI polling pauses while hidden, refreshes on return, prevents overlap and ignores late responses for a previous scope. Trading-worker cadence is untouched.

## Measured comparison

The prior audit and this run use synthetic fixtures; these are layout measurements, not a trader usability study or production trade evidence.

| Measurement | Audit | Redesigned |
|---|---:|---:|
| Open Positions heading, Phase 15 at 390px | 2,693px | 610px |
| Open Positions heading, Phase 15 at 1440px | 1,343px | 450px |
| Overview document width at 390px | 469px | 390px |
| Mobile navigation | Full route list | Five task destinations |

Both themes give identical geometry. At 390px, the stale fixture places exposure at 660px and unavailable fixture at 676px. All 28 route/theme renders at each of 360, 390 and 430px have zero page overflow, browser errors, unexpected API requests or detected clipped financial values. These checks do not constitute real-device, screen-reader or timed comprehension studies. Layout-shift improvements were not separately quantified.

## Validation

- Full backend: **1,952 passed, 68 skipped**, one existing Starlette deprecation warning.
- Frontend: **141 passed across 17 files**.
- Focused backend deployment/monitor/trade projection checks: **53 passed** (included in the full suite).
- Affected backend flake8 and mypy: PASS. Frontend ESLint, TypeScript and production build: PASS. `git diff --check`: PASS.
- One inherited deployment test asserted the old API concatenation variable. It now asserts the already-existing source-specific URL routing. The API client itself is unchanged and a frontend behavior test verifies source isolation.
- Chromium: **196 route/viewport/theme combinations**, plus five representative state scenarios and navigation/filter interactions. No browser-side request may reach a production or external endpoint: all API GETs are intercepted synthetic fixtures; unknown requests and mutation methods fail.

Routes: `/`, `/phase15`, `/nifty`, `/positions`, `/trades`, `/nifty/research`, `/history`, `/history/1`, `/shadow`, `/shadow/7`, `/system`, `/commodity-acts`, `/commodity-acts/CRUDEOILM`, `/commodity-acts/NATGASMINI`.

Viewports: 1440×900, 1280×800, 1024×768, 768×1024, 430×932, 390×844, 360×800. All routes run in light and dark themes.

State coverage includes profitable/losing open positions, unresolved exposure, expired unpriced journal rows, no setup, blocked entry, above/inside/below ORB range, stale and unavailable data. Tests cover N/A preservation, exact/frozen evidence, status semantics, source isolation and polling races. The More dialog closes with Escape and returns focus; theme toggling and mobile filtering are exercised.

## Reproduce locally

From `frontend`, install dependencies and Chromium if needed, then:

```sh
npm test
npm run lint
npx tsc --noEmit
npm run build
npm run start -- --hostname 127.0.0.1 --port 3108
```

In another local terminal:

```sh
RESPONSIVE_BASE_URL=http://127.0.0.1:3108 RESPONSIVE_ARTIFACT_DIR=/tmp/ai-trade-responsive npm run test:responsive
```

The validator refuses nonlocal frontend hosts. Fixtures are explicitly synthetic; it never runs a worker, places an order or writes to a trading database.

Backend validation from repository root: `python -m pytest backend/tests`; run affected-module flake8 with the repository's 119-character limit and mypy with backend import configuration.

## Evidence artifacts

Final screenshots and machine-readable measurements:
`/workspaces/frontend-responsive-v2-20261023/final/`.

Prior audit: `/workspaces/frontend-responsive-audit-20261023/`.

The local artifact index lists screenshots for all seven widths and both themes. Screenshots and build artifacts are not committed. Production behavior and real-device rendering still require normal review before any separately authorized deployment.
