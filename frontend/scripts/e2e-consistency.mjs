// Cross-page financial consistency: the SAME realized net / gross / charges / trade count must appear on the API,
// Overview, Tradebook, Analytics, Positions (closed tab) and the CSV export, for several filter windows.
//   BASE=http://127.0.0.1:8080 node scripts/e2e-consistency.mjs     (exit code 1 on any mismatch)
import { chromium } from "playwright";

const BASE = process.env.BASE ?? "http://127.0.0.1:8080";
const WINDOWS = [
  { name: "NIFTY V3, one session", q: "from=2026-11-23&to=2026-11-23&engine=nifty-acts_v3" },
  { name: "all engines, one session", q: "from=2026-11-20&to=2026-11-20" },
  { name: "NIFTY V2, a week", q: "from=2026-11-16&to=2026-11-20&engine=nifty-acts_v2" },
  { name: "MCX crude (synthetic), one session", q: "from=2026-11-24&to=2026-11-24&engine=mcx-crudeoilm" },
  { name: "empty window", q: "from=2026-01-05&to=2026-01-05" },
];
const num = (s) => { const m = String(s ?? "").replace(/[−–]/g, "-").replace(/[₹,+\s]/g, "").match(/-?\d+(\.\d+)?/); return m ? Number(m[0]) : null; };
const eq = (a, b) => a !== null && b !== null && Math.abs(a - b) < 0.015;
const json = async (path) => (await fetch(`${BASE}/api/portfolio/${path}`)).json();
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const lines = async (path, q) => {
  await page.goto(`${BASE}${path}${path.includes("?") ? "&" : "?"}${q}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2200);
  return (await page.evaluate(() => document.querySelector("#workspace-content")?.innerText ?? "")).split("\n").map((s) => s.trim()).filter(Boolean);
};
const after = (ls, label, n = 1) => { const i = ls.findIndex((l) => l.toUpperCase() === label); return i < 0 ? null : ls[i + n] ?? null; };

let failed = 0, checks = 0;
for (const w of WINDOWS) {
  const api = { s: await json(`summary?mode=PAPER&${w.q}`), t: await json(`trades?mode=PAPER&limit=200&${w.q}`), p: await json(`performance?mode=PAPER&${w.q}`) };
  const csv = (await (await fetch(`${BASE}/api/portfolio/trades.csv?mode=PAPER&${w.q}`)).text()).trim().split("\n").slice(1).filter(Boolean);
  const truth = { net: api.s.pnl.realized_net, gross: api.s.pnl.realized_gross, trades: api.s.counts.closed_trades };
  const got = {};
  const ov = await lines("/", `mode=PAPER&${w.q}`);
  got["Overview realized net"] = num(after(ov, "REALIZED NET")); got["Overview realized gross"] = num((after(ov, "REALIZED NET", 2) ?? "").replace(/^Gross/i, ""));
  const tb = await lines("/tradebook", `mode=PAPER&${w.q}`);
  const tot = tb.find((l) => /^Totals/i.test(l)); const cells = tot ? tot.split("\t").filter(Boolean).slice(1) : [];   // the totals cells share the "Totals" line
  const tbNums = cells.map(num).filter((x) => x !== null);
  got["Tradebook net"] = tbNums.length >= 3 ? tbNums[tbNums.length - 1] : (api.t.totals.trades === 0 ? 0 : null);
  got["Tradebook gross"] = tbNums.length >= 3 ? tbNums[tbNums.length - 3] : (api.t.totals.trades === 0 ? 0 : null);
  const an = await lines("/analytics", `mode=PAPER&${w.q}`);
  got["Analytics cumulative net"] = truth.trades === 0 ? 0 : num(after(an, "CUMULATIVE NET P&L"));
  const pos = await page.goto(`${BASE}/positions?tab=closed&mode=PAPER&${w.q}`, { waitUntil: "networkidle" }).then(async () => { await page.waitForTimeout(2200); return page.evaluate(() => {
    const t = [...document.querySelectorAll("table")].find((x) => [...x.querySelectorAll("th")].some((h) => h.textContent.trim().toUpperCase() === "NET")); if (!t) return null;
    const idx = [...t.querySelectorAll("thead th")].findIndex((h) => h.textContent.trim().toUpperCase() === "NET");
    return [...t.querySelectorAll("tbody tr")].map((r) => r.querySelectorAll("td,th")[idx]?.textContent ?? "").filter((x) => /\d/.test(x)); }); });
  got["Positions closed: sum of NET column"] = pos === null ? (truth.trades === 0 ? 0 : null) : Math.round(pos.map(num).reduce((a, b) => a + b, 0) * 100) / 100;
  got["CSV export rows"] = csv.length; got["Tradebook API trade count"] = api.t.totals.trades;
  const rows = [
    ["Overview realized net", truth.net], ["Overview realized gross", truth.gross], ["Tradebook net", truth.net], ["Tradebook gross", truth.gross],
    ["Analytics cumulative net", truth.net], ["Positions closed: sum of NET column", truth.net], ["CSV export rows", truth.trades], ["Tradebook API trade count", truth.trades],
  ];
  console.log(`\n== ${w.name}  [${w.q}]  API truth: net ${truth.net}  gross ${truth.gross}  trades ${truth.trades}`);
  for (const [k, expected] of rows) {
    checks++; const ok = eq(got[k], expected); if (!ok) failed++;
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${k.padEnd(38)} shown=${got[k]}  expected=${expected}`);
  }
}
await browser.close();
console.log(`\n${checks - failed}/${checks} cross-page checks passed`);
process.exit(failed ? 1 : 0);
