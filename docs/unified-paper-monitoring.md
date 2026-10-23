# Commodity automation and unified paper monitoring

Built on completed `e88bdc2` (itself based on deployed `5449ff7`), not a replacement
ACTS strategy. MCX renewal preserves existing scoring, policy hashes, risk budgets,
one-lot quantities, next-observation BID/ASK fills and exit safeguards. No schema
change or migration. NIFTY backend, database and worker code are unchanged.

## Commodity runtime

Keep existing opt-in flags/policies/session configuration. The deployed renewal
flag must remain true. Actual exact-master pAssetCode/tick/lot linkage and fresh
quotes remain mandatory. Held, nonexpired contracts pin capture for exits. Expired
unpriced exposure stays reserved/unresolved; never fabricate an exit at rollover.

New `COMMODITY_ACTS_MASTER_REFRESH_SECONDS` defaults to 1800 (60..86400). A failed
refresh cannot reuse expired metadata. Reselection still occurs on each observation,
so a cutoff does not wait for the next refresh. Restart triggers fresh discovery.

Both workers must share the existing `/app/cache` mount. The default
`COMMODITY_ACTS_REQUEST_BUDGET_FILE=/app/cache/commodity-market-budget.json` is a
private regular file containing only pacing clocks. Kernel locks coordinate both
processes/restarts: at most one Commodity request start per second, up to three
seconds budget wait, no automatic request retries, and a shared 30-second cooldown
after SDK exceptions. Corrupt, inaccessible or unsafe state fails closed. This
limits Commodity traffic; it is NOT an account-wide quota guarantee because the
separate NIFTY engine is intentionally unchanged. The existing staggered 15-second
schedule and bounded batch quotes remain unchanged. Docker restart unless-stopped
and existing journal recovery/health checks are retained; no synthetic backfill.

## Unified UI and private cross-server bridge

`/` adds an independent unified overview; `/nifty` reuses the six-strategy monitor,
positions/journal and old dashboard. Existing routes stay available. Commodity
detail routes `/commodity-acts/CRUDEOILM` and `/commodity-acts/NATGASMINI` reuse
the original components. New today-only API totals use stored aware entry/exit
timestamps in IST. Existing cumulative totals are retained, never mislabeled today.
Absent/stale fields remain unavailable. Configuration/activity never proves process
liveness. Net/capital metrics are unavailable unless supported by existing evidence.

Build the existing NIFTY frontend with same-origin `NEXT_PUBLIC_API_BASE_URL=""`
and `NEXT_PUBLIC_COMMODITY_API_BASE=/monitor-data/commodity`. Only the new frontend
container is replaced, using `up --no-deps --no-build --pull never frontend`.
Never run generic Compose up/down or restart NIFTY backend/PostgreSQL/workers.

Runtime server-only configuration (never NEXT_PUBLIC):

- `UNIFIED_NIFTY_ORIGIN`: existing authenticated HTTPS NIFTY gateway origin. The
  bridge verifies the viewer's existing Basic Authorization against its read-only
  health endpoint. Missing/failed authentication never exposes Commodity data.
- `UNIFIED_COMMODITY_ORIGIN`: existing authenticated, TLS-verified Commodity gateway
  origin, or an explicitly managed loopback SSH tunnel. Do NOT expose port 8080,
  private APIs or PostgreSQL to the public internet to make this work.
- `UNIFIED_COMMODITY_AUTH_FILE`: read-only, owner-private regular file, readable by
  frontend UID 1001, containing that gateway's Basic/Bearer Authorization header.
  Provision via an authorized encrypted channel; never put a value in source,
  image build args, browser storage or logs. The existing loopback-only Commodity
  gateway needs a reviewed authenticated read-only transport before configuration.

`/monitor-data/{source}/...` exports GET only with exact path and query allowlists,
five-second request timeouts, bounded response sizes and eight in-flight requests.
It refuses arbitrary origins/path traversal, redirects, invalid JSON, unauthenticated
viewers and mutation endpoints. Credentials and upstream error bodies are never
returned. Existing Caddy authentication is preserved; this does not trust a forged
"authenticated user" header or add a public unauthenticated dashboard endpoint.

Deploy only after authenticated SSH identity is established for the NIFTY host.
The local known-host gate currently requires an independently confirmed ED25519
fingerprint; do not disable strict SSH/TLS checking to clear it. Without NIFTY
access/approved private bridge configuration, frontend deployment is BLOCKED even
when local tests/build pass. The isolated Commodity deployment can proceed without
changing the NIFTY system or provisioning a second broker consumer key.
