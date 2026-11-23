/**
 * AST Futures read-only dashboard validation against a disposable local server.
 *  Part A: responsive render states (empty journal, live position, stale worker, journal failure) from
 *          explicitly synthetic bridge fixtures.
 *  Part B: live security checks against the real Next.js bridge and a disposable read-only API.
 * Requires: AST_VALIDATION_VIEWER (e.g. "Basic ..."), AST_VALIDATION_SECRET_FILES (comma-separated server-only
 * files whose contents must never reach a browser). Local origins only.
 */
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const base = process.env.AST_VALIDATION_BASE_URL ?? "http://127.0.0.1:3108";
const apiOrigin = process.env.AST_VALIDATION_API_ORIGIN ?? "http://127.0.0.1:8010";
for (const url of [base, apiOrigin]) if (!["127.0.0.1", "localhost"].includes(new URL(url).hostname)) throw Error("Local disposable servers only");
const viewer = process.env.AST_VALIDATION_VIEWER;
if (!viewer?.startsWith("Basic ")) throw Error("AST_VALIDATION_VIEWER required");
const secrets = (process.env.AST_VALIDATION_SECRET_FILES ?? "").split(",").filter(Boolean).map(file => fs.readFileSync(file, "utf8").trim());
const output = process.env.AST_VALIDATION_ARTIFACT_DIR ?? "/tmp/ast-futures-validation";
fs.mkdirSync(path.join(output, "screenshots"), { recursive: true });
const results = { notice: "SYNTHETIC FIXTURES AND DISPOSABLE SERVERS ONLY; no production, broker or trading data", renders: [], security: [], failures: [] };
const check = (name, ok, detail = null) => { results.security.push({ name, ok, detail }); if (!ok) results.failures.push(name); };

const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
const fresh = () => new Date(Date.now() - 5000).toISOString();
const baseStatus = () => ({
  strategy_id: "AST_FUTURES_V1_DIRECT", strategy_version: "AST_FUTURES_V1_DIRECT", mode: "PAPER", live_trading: false,
  worker: "RUNNING", worker_enabled: true, heartbeat: {}, heartbeat_age_seconds: 10, kill_switch: false, policy_hash: "synthetic",
  fixed_parameters: { atr_period: 22, multiplier: 3.9, regime: "NONE", optimizer: "DISABLED" },
  policy_configuration: { history_sessions: 15, lot_size: 65, lots: 1, opening_range_ist: "09:15:00-09:30:00", session_exit_time_ist: "15:20:00", friction_points_per_round_trip: null },
  trading_date: today, in_session: false, futures_instrument: null, last_observation_at: null, last_completed_bar: null, opening_range: null, ast: null,
  status: null, blockers: null, fill_blocker: null, position: null, position_detail: null, mark: null, position_mark_gross_inr: null,
  trades_today: [], today_gross_points: 0, cumulative_closed_trades: 0, cumulative_gross_points: 0, cumulative_net_research_points: null,
  forced_exit_status: "SCHEDULED_15:20_IST", journal_sequence: 0, journal_head: "GENESIS", journal_verification: "VALID",
  fill_basis: "FUTURES_LTP_PAPER_V1", fill_basis_note: "Synthetic fixture.",
});
const position = { trade_id: "synthetic-1", trading_date: today, state: "OPEN", side: "LONG", future_instrument_id: "SYNTHETIC-NIFTY-FUT",
  future_expiry: "2026-11-26", lot_size: 65, quantity: 65, signal_timestamp: `${today}T10:15:00+05:30`, entry_timestamp: `${today}T10:15:10+05:30`,
  entry_price: 26050, exit_timestamp: null, exit_price: null, exit_reason: null, gross_points: null, gross_inr: null, net_research_points: null,
  rejection_reason: null, unresolved_reason: null, fill_basis: "FUTURES_LTP_PAPER_V1" };
const states = {
  "empty-journal": { status: baseStatus(), expect: ["Waiting for first market observation", "No open paper position"], alert: null },
  "live-position": { status: { ...baseStatus(), in_session: true, last_observation_at: fresh(), status: "OPEN", blockers: ["MAX_OPEN_POSITIONS_REACHED"],
    futures_instrument: { instrument_id: "SYNTHETIC-NIFTY-FUT", expiry: "2026-11-26", observed_lot_size: 65, ltp: 26072.5, timing_ok: true, observation_blocker: null },
    opening_range: { high: 26040, low: 25980, range_start: `${today}T09:15:00+05:30`, range_end: `${today}T09:30:00+05:30` },
    ast: { atr_period: 22, multiplier: 3.9, direction: "BULL", value: 25990, atr: 18, transition: `${today}T09:50:00+05:30` },
    position, position_detail: position, mark: { trade_id: "synthetic-1", at: fresh(), price: 26072.5, points: 22.5, mfe_points: 30, mae_points: -12 },
    position_mark_gross_inr: 1462.5, trades_today: [position], journal_sequence: 40, journal_head: "f".repeat(64) },
    expect: ["CURRENT PAPER POSITION", "SYNTHETIC-NIFTY-FUT", "+22.50"], alert: null, positionFirst: true },
  "stale-worker": { status: { ...baseStatus(), worker: "STOPPED", heartbeat_age_seconds: 900 }, expect: ["UNHEALTHY"], alert: "WORKER STOPPED" },
  "journal-failure": { status: { ...baseStatus(), journal_verification: "INVALID:AST_JOURNAL_CHAIN_INVALID" }, expect: ["INVALID"], alert: "JOURNAL VERIFICATION FAILED" },
};
const emptyPage = { items: [], total: 0, limit: 25, offset: 0, has_more: false };
const performance = { label: "FORWARD_PAPER_PERFORMANCE", basis: "synthetic", drawdown_basis: "CLOSED_TRADE_EQUITY_FROM_ZERO_NOT_MARK_TO_MARKET",
  closed_trades: 0, wins: 0, losses: 0, flat: 0, win_rate: null, gross_points: 0, gross_inr: null, net_research_points: null, average_points: null,
  best_trade: null, worst_trade: null, daily: [], current_peak_points: 0, current_drawdown_points: 0, max_realized_drawdown_points: 0, excluded_unresolved_exposures: 0 };
const widths = [[1440, 900], [390, 844], [360, 800]];

const browser = await chromium.launch();
try {
  // Part A: responsive states.
  for (const [state, spec] of Object.entries(states)) for (const [width, height] of widths) {
    const context = await browser.newContext({ viewport: { width, height }, extraHTTPHeaders: { Authorization: viewer } });
    const page = await context.newPage();
    const errors = [], unexpected = [];
    page.on("pageerror", error => errors.push(String(error)));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") { unexpected.push(`${route.request().method()} ${url.pathname}`); return route.abort(); }
      if (url.pathname.startsWith("/monitor-data/ast-direct/api/ast-direct/")) {
        const leaf = url.pathname.split("/").pop();
        const body = leaf === "status" ? spec.status : leaf === "performance" ? performance :
          leaf === "events" ? { ...emptyPage, scope: "lifecycle", journal_sequence: spec.status.journal_sequence, journal_head: spec.status.journal_head } : emptyPage;
        return route.fulfill({ contentType: "application/json", headers: { "cache-control": "private, no-store" }, body: JSON.stringify(body) });
      }
      // Workspace nav health poll; the isolated host has no ACTS backend, so a neutral stub keeps console noise out.
      if (url.pathname === "/api/health") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ status: "UNKNOWN" }) });
      if (url.origin !== new URL(base).origin) { unexpected.push(url.origin); return route.abort(); }
      return route.continue();
    });
    await page.goto(`${base}/ast-futures`, { waitUntil: "networkidle" });
    await page.getByText("PAPER ONLY · NO BROKER ORDERS").waitFor();
    for (const text of spec.expect) await page.getByText(text, { exact: false }).first().waitFor({ timeout: 10000 });
    const measured = await page.evaluate(() => ({ overflow: document.documentElement.scrollWidth - window.innerWidth,
      buttons: Array.from(document.querySelectorAll("main button, main form, main input")).map(e => e.outerHTML.slice(0, 80)),
      fixed: ["ATR", "22", "3.9", "NONE", "DISABLED", "15:20 IST"].every(t => document.body.innerText.includes(t)),
      positionFirst: (() => { const a = document.querySelector('[aria-label="Current paper position"]'); const b = document.querySelector('[aria-label="NIFTY futures instrument"]');
        return a && b ? Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) : null; })(),
      alert: document.querySelector('main [role="alert"]')?.textContent ?? null }));
    const ok = measured.overflow <= 1 && !errors.length && !unexpected.length && !measured.buttons.length && measured.fixed &&
      (spec.alert ? measured.alert?.includes(spec.alert) : measured.alert === null) && (!spec.positionFirst || measured.positionFirst === true);
    await page.screenshot({ path: path.join(output, "screenshots", `${state}-${width}.png`), fullPage: true });
    results.renders.push({ state, width, ok, ...measured, errors, unexpected });
    if (!ok) results.failures.push(`render:${state}:${width}`);
    await context.close();
  }

  // Part B: live bridge and read API (no interception).
  const bridge = `${base}/monitor-data/ast-direct/api/ast-direct`;
  const get = (url, headers = {}, method = "GET") => fetch(url, { method, headers, redirect: "manual" });
  const ok = await get(`${bridge}/status`, { Authorization: viewer });
  const okText = await ok.text();
  check("authenticated status 200", ok.status === 200, ok.status);
  check("authenticated response private no-store", ok.headers.get("cache-control") === "private, no-store", ok.headers.get("cache-control"));
  check("authenticated response varies by Authorization", (ok.headers.get("vary") ?? "").includes("Authorization"), ok.headers.get("vary"));
  check("no set-cookie", ok.headers.get("set-cookie") === null);
  const live = JSON.parse(okText);
  check("live data is PAPER with live trading disabled", live.mode === "PAPER" && live.live_trading === false);
  check("live seeded position visible through bridge", live.position?.side === "LONG" && live.position?.state === "OPEN", live.position);
  check("no secret in status body", secrets.every(s => !okText.includes(s)));
  for (const [name, headers] of [["missing credentials", {}], ["wrong credentials", { Authorization: `Basic ${Buffer.from("viewer:wrong").toString("base64")}` }],
    ["bearer token as viewer", { Authorization: `Bearer ${secrets[0] ?? "x"}` }], ["malformed", { Authorization: "Basic" }]]) {
    const response = await get(`${bridge}/status`, headers);
    const body = await response.text();
    check(`bypass rejected: ${name}`, response.status === 401 && !body.includes("AST_FUTURES_V1_DIRECT"), response.status);
    check(`error not cacheable: ${name}`, response.headers.get("cache-control") === "private, no-store", response.headers.get("cache-control"));
  }
  const reuse = await get(`${bridge}/status`);
  check("authenticated data not replayed to an unauthenticated request", reuse.status === 401, reuse.status);
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    const response = await get(`${bridge}/status`, { Authorization: viewer }, method);
    check(`bridge rejects ${method}`, response.status === 405, response.status);
  }
  for (const leaf of ["acknowledge", "kill-switch", "orders", "../health", "%2e%2e/status"]) {
    const response = await get(`${bridge}/${leaf}`, { Authorization: viewer });
    check(`bridge rejects path ${leaf}`, [400, 404].includes(response.status), response.status);
  }
  for (const source of ["nifty", "commodity"]) {
    const response = await get(`${base}/monitor-data/${source}/api/ast-direct/status`, { Authorization: viewer });
    check(`AST path not reachable through ${source}`, response.status === 404, response.status);
  }
  // The Next.js server drops an exact "__proto__" query key before route handlers run, so it is never forwarded
  // (verified against a logging upstream); prototype-like keys that do reach the bridge must be rejected.
  for (const query of ["limit=101", "limit=0", "side=BUY", "scope=raw", "url=http://evil.test", "constructor=x", "prototype=x",
    "__proto__[x]=1", "date=today"]) {
    const response = await get(`${bridge}/trades?${query}`, { Authorization: viewer });
    check(`bridge rejects filter ${query}`, response.status === 400, response.status);
  }
  const direct = await get(`${apiOrigin}/api/ast-direct/status`);
  check("read API requires its bearer token", direct.status === 401, direct.status);
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    const response = await get(`${apiOrigin}/api/ast-direct/status`, {}, method);
    check(`read API rejects ${method}`, response.status === 405, response.status);
  }
  check("read API docs disabled", (await get(`${apiOrigin}/openapi.json`)).status === 404);

  // Live browser: every response body seen by the browser is scanned for server-only secrets.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, extraHTTPHeaders: { Authorization: viewer } });
  const page = await context.newPage();
  const leaks = [], requests = [];
  page.on("request", request => requests.push(`${request.method()} ${new URL(request.url()).pathname}`));
  page.on("response", async response => { try { const text = await response.text(); if (secrets.some(s => text.includes(s))) leaks.push(response.url()); } catch { /* redirects/bodiless */ } });
  // Live pages poll continuously, so wait for concrete evidence rather than network idle.
  await page.goto(`${base}/ast-futures`, { waitUntil: "domcontentloaded" });
  await page.getByLabel("Current paper position").waitFor({ timeout: 15000 });
  await page.waitForTimeout(6000);  // at least one further status poll through the bridge
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie, html: document.documentElement.outerHTML }));
  check("live page shows the real paper position", await page.getByLabel("Current paper position").isVisible());
  check("browser received no server secret", !leaks.length && secrets.every(s => !storage.includes(s)), leaks);
  check("browser made only GET requests", requests.every(r => r.startsWith("GET ")), requests.filter(r => !r.startsWith("GET ")));
  check("browser never contacted the read API directly", requests.every(r => !r.includes(":8010")));
  await page.screenshot({ path: path.join(output, "screenshots", "live-position-1440.png"), fullPage: true });
  await context.close();
  const anonymous = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const anonymousPage = await anonymous.newPage();
  const anonymousResponse = await anonymousPage.goto(`${base}/ast-futures`, { waitUntil: "domcontentloaded" });
  // Behind the Basic-auth ingress the page itself is refused; directly, the bridge refuses the data.
  if (anonymousResponse?.status() !== 401) await anonymousPage.getByText(/AST STATUS UNAVAILABLE/).waitFor({ timeout: 10000 });
  check("unauthenticated browser sees no strategy data", !(await anonymousPage.content()).includes("CURRENT PAPER POSITION"),
    anonymousResponse?.status());
  await anonymous.close();
  const shell = await get(`${base}/ast-futures`);
  results.page_shell_cache_control = shell.headers.get("cache-control");
} finally {
  await browser.close();
  fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(results, null, 2));
}
console.log(JSON.stringify({ renders: results.renders.map(r => `${r.state}@${r.width}:${r.ok ? "OK" : "FAIL"}`), security_checks: results.security.length,
  security_failures: results.failures, page_shell_cache_control: results.page_shell_cache_control }, null, 2));
process.exit(results.failures.length ? 1 : 0);
