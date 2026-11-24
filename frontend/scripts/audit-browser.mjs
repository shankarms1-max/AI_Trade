// Browser audit: every route x viewport, console/network errors, overflow, N/A density, every button/tab, nav links,
// back/forward. Read-only (the app has no mutating controls). Usage:
//   BASE=http://127.0.0.1:8080 OUT=./audit-out node scripts/audit-browser.mjs [label]
import { chromium } from "playwright";
import { mkdirSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "http://127.0.0.1:8080";
const OUT = process.env.OUT ?? "./audit-out";
const LABEL = process.argv[2] ?? "before";
const ROUTES = (process.env.ROUTES ?? "").split(",").filter(Boolean);
const DEFAULT_ROUTES = ["/", "/positions", "/positions?tab=closed", "/positions?tab=unresolved", "/orders", "/tradebook", "/trades", "/strategies",
  "/strategies/nifty-acts_v3", "/strategies/mcx-natgasmini?view=signals", "/strategies/nifty-acts_v3?view=configuration", "/strategies/does-not-exist",
  "/analytics", "/risk", "/research", "/settings", "/nifty", "/phase15", "/nifty/research", "/engines", "/mcx-futures",
  "/mcx-futures/CRUDEOILM", "/mcx-futures/NATGASMINI", "/mcx-futures/NIFTY", "/ast-futures", "/commodity-acts",
  "/commodity-acts/CRUDEOILM", "/commodity-acts/NATGASMINI", "/history", "/history/1", "/shadow", "/shadow/1", "/system",
  "/does-not-exist"];
const VIEWPORTS = [[1920, 1080], [1440, 900], [1280, 800], [768, 1024], [390, 844]];
mkdirSync(`${OUT}/${LABEL}`, { recursive: true });
const slug = (r) => (r === "/" ? "home" : r.replace(/^\//, "").replace(/[\/\[\]]/g, "_"));

const browser = await chromium.launch();
const results = [];
for (const route of ROUTES.length ? ROUTES : DEFAULT_ROUTES) {
  for (const [w, h] of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h } });
    const page = await ctx.newPage();
    const consoleErrors = [], failed = [], pageErrors = [];
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 240)); });
    page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 240)));
    page.on("response", (r) => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url().replace(BASE, "")}`); });
    page.on("requestfailed", (r) => failed.push(`FAILED ${r.url().replace(BASE, "")} ${r.failure()?.errorText}`));
    let status = null, error = null;
    try {
      const resp = await page.goto(`${BASE}${route}`, { waitUntil: "networkidle", timeout: 30000 });
      status = resp?.status() ?? null;
      await page.waitForTimeout(1200);
    } catch (e) { error = String(e).slice(0, 200); }
    const metrics = await page.evaluate(() => {
      const body = document.body;
      const text = body.innerText || "";
      const wide = [...document.querySelectorAll("*")].filter((el) => {
        const r = el.getBoundingClientRect();
        return r.right > window.innerWidth + 1 && !el.closest("[class*='table-wrap'],[class*='scroll'],table,pre") && r.width > 0;
      }).slice(0, 3).map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 40)}`);
      return { pageOverflowPx: Math.max(0, document.documentElement.scrollWidth - window.innerWidth), overflowing: wide,
        na: (text.match(/\bN\/A\b/g) || []).length, unavailable: (text.match(/UNAVAILABLE|UNCONFIGURED|DISABLED/g) || []).length,
        chars: text.length, h1: document.querySelector("h1")?.textContent?.trim() ?? null,
        buttons: document.querySelectorAll("button").length, tabs: document.querySelectorAll("[role=tab]").length };
    }).catch(() => ({}));
    if ([1440, 390].includes(w)) await page.screenshot({ path: `${OUT}/${LABEL}/${slug(route)}-${w}.png` }).catch(() => {});
    results.push({ route, viewport: `${w}x${h}`, status, error, consoleErrors: [...new Set(consoleErrors)], pageErrors,
      failed: [...new Set(failed)], ...metrics });
    await ctx.close();
  }
}

// Interaction audit at 1440: click every button/tab on each route, flag new errors; verify nav links; back/forward.
const interactions = [];
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errs = [];
page.on("console", (m) => { if (m.type() === "error") errs.push(m.text().slice(0, 200)); });
page.on("pageerror", (e) => errs.push(String(e).slice(0, 200)));
for (const route of ["/", "/positions", "/orders", "/tradebook", "/strategies", "/analytics", "/risk", "/nifty", "/engines", "/mcx-futures", "/mcx-futures/CRUDEOILM", "/commodity-acts", "/system"]) {
  await page.goto(`${BASE}${route}`, { waitUntil: "networkidle" }).catch(() => {});
  const sel = "#workspace-content button, #workspace-content [role=tab]";
  const n = await page.locator(sel).count();
  let clicked = 0, broke = 0;
  for (let i = 0; i < Math.min(n, 25); i++) {
    const el = page.locator(sel).nth(i);
    if (!(await el.isVisible().catch(() => false)) || !(await el.isEnabled().catch(() => false))) continue;
    const before = errs.length;
    await el.click({ timeout: 2000 }).catch(() => {});
    clicked++;
    await page.waitForTimeout(150);
    if (errs.length > before) broke++;
    if (!page.url().startsWith(`${BASE}${route}`)) { await page.goto(`${BASE}${route}`, { waitUntil: "networkidle" }).catch(() => {}); }
  }
  interactions.push({ route, controls: n, clicked, clicksThatRaisedErrors: broke });
}
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
const navTargets = await page.locator("aside nav a").evaluateAll((as) => as.map((a) => ({ text: a.textContent.trim(), href: a.getAttribute("href") })));
const navResults = [];
for (const t of navTargets) {
  await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.querySelector("details.tm-more")?.setAttribute("open", "")); // legacy group is collapsed by default
  await page.locator(`aside nav a[href="${t.href}"]`).click();
  await page.waitForURL((u) => u.pathname === t.href, { timeout: 8000 }).catch(() => {});
  await page.waitForLoadState("networkidle").catch(() => {});
  navResults.push({ ...t, landed: new URL(page.url()).pathname, ok: new URL(page.url()).pathname === t.href });
}
const history = { expected: ["/positions", "/", "/positions", "/positions"] };
try {
  const hp = await ctx.newPage();
  const at = () => new URL(hp.url()).pathname;
  await hp.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await hp.goto(`${BASE}/positions`, { waitUntil: "networkidle" });
  await hp.goto(`${BASE}/trades`, { waitUntil: "networkidle" });
  await hp.goBack({ waitUntil: "networkidle" }); history.back1 = at();
  await hp.goBack({ waitUntil: "networkidle" }); history.back2 = at();
  await hp.goForward({ waitUntil: "networkidle" }); history.fwd = at();
  await hp.reload({ waitUntil: "networkidle" }); history.afterReload = at();
} catch (e) { history.error = String(e).slice(0, 160); }
await browser.close();

const summary = {
  label: LABEL, base: BASE, routes: [...new Set(results.map((r) => r.route))].length, runs: results.length,
  consoleErrorRuns: results.filter((r) => r.consoleErrors.length || r.pageErrors.length).length,
  failedRequestRuns: results.filter((r) => r.failed.length).length,
  overflowRuns: results.filter((r) => r.pageOverflowPx > 1).length,
};
writeFileSync(`${OUT}/${LABEL}/audit.json`, JSON.stringify({ summary, results, interactions, navResults, history }, null, 1));
console.log(JSON.stringify(summary), "\nnav:", navResults.map((n) => `${n.text}${n.ok ? "" : " !!" + n.landed}`).join(" | "), "\nhistory:", JSON.stringify(history));
