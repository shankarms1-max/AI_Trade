// Cross-stack contract: payloads produced by the REAL backend portfolio code (backend/tests/tools/dump_portfolio_fixture.py,
// synthetic market input) must pass the frontend runtime guards unchanged. Regenerate the fixture whenever
// backend/app/api/portfolio.py or backend/app/portfolio/* change.
import { describe, expect, it } from "vitest";
import fixture from "./fixtures/portfolio-synthetic.json";
import {
  parseAlerts, parseEquity, parseFills, parseHeader, parseOrders, parsePerformance, parsePositionDetail, parsePositions,
  parseRisk, parseSources, parseStrategies, parseStrategyDetail, parseSummary, parseTrades,
} from "@/lib/portfolio-guards";
import { PayloadError } from "@/lib/http";

const f = fixture as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

describe("portfolio contract (synthetic payloads from the real backend code)", () => {
  it("is labelled synthetic", () => expect(f.label).toMatch(/^SYNTHETIC/));
  it.each([
    ["summary", parseSummary, "summary"], ["positions (open)", parsePositions, "positions_open"], ["positions (closed)", parsePositions, "positions_closed"],
    ["positions (unresolved)", parsePositions, "positions_unresolved"], ["positions (all)", parsePositions, "positions_all"],
    ["orders", parseOrders, "orders"], ["fills", parseFills, "fills"], ["trades", parseTrades, "trades"], ["performance", parsePerformance, "performance"],
    ["risk", parseRisk, "risk"], ["equity", parseEquity, "equity"], ["strategies", parseStrategies, "strategies"], ["alerts", parseAlerts, "alerts"],
    ["header", parseHeader, "header"], ["sources", parseSources, "sources"], ["strategy detail (NIFTY)", parseStrategyDetail, "strategy_nifty_v3"],
    ["strategy detail (MCX)", parseStrategyDetail, "strategy_crude"], ["position detail (closed spread)", parsePositionDetail, "position_detail_closed_spread"],
    ["position detail (expired open)", parsePositionDetail, "position_detail_expired_open"], ["position detail (futures)", parsePositionDetail, "position_detail_futures"],
  ])("%s passes its guard", (_name, guard, key) => {
    expect(() => (guard as (v: unknown, p: string) => unknown)(f[key], "")).not.toThrow();
  });

  it("keeps the expired open position's unknowns as null, never zero", () => {
    const p = parsePositions(f.positions_open, "").items.find(x => x.flags.includes("EXPIRED_NOT_SETTLED"))!;
    expect(p.status).toBe("OPEN");
    expect(p.unrealized_gross).toBeNull();
    expect(p.est_net_liquidation).toBeNull();
    expect(p.mark.price).toBeNull();
    expect(p.mark.reason).toBe("EXPIRED_CONTRACT_NOT_SETTLED");
    expect(p.mark.basis).toBe("STALE");
  });

  it("carries the partial-total flags the UI must label", () => {
    const s = parseSummary(f.summary, "");
    expect(s.pnl.partial).toBe(true);
    expect(s.pnl.excluded_open_positions).toBeGreaterThan(0);
    expect(s.pnl.note).toMatch(/PARTIAL/);
    expect(s.attention[0].flags).toContain("EXPIRED_NOT_SETTLED");
  });

  it("paper orders are simulated and have no broker order id", () => {
    const o = parseOrders(f.orders, "");
    expect(o.items.length).toBeGreaterThan(0);
    for (const order of o.items) { expect(order.simulated).toBe(true); expect(order.broker_order_id).toBeNull(); expect(order.mode).toBe("PAPER"); }
  });

  it("tolerates optional keys that some engines omit (live MCX rows carry no first_sequence, engine_recorded_*)", () => {
    const p = structuredClone(f.positions_open.items[0]);
    delete p.journal.first_sequence; delete p.journal.events; delete p.decision_credit; delete p.rearm_reason; delete p.cycle_id;
    expect(() => parsePositions({ ...f.positions_open, items: [p] }, "")).not.toThrow();
  });

  it("rejects a malformed payload with a typed PayloadError instead of guessing", () => {
    const bad = structuredClone(f.summary);
    bad.pnl.total_net_pnl = "12";
    expect(() => parseSummary(bad, "")).toThrow(PayloadError);
    const noCounts = structuredClone(f.positions_open); delete noCounts.tab_counts;
    expect(() => parsePositions(noCounts, "")).toThrow(/tab_counts/);
    expect(() => parseOrders({ items: "x" }, "")).toThrow(PayloadError);
  });

  it("MCX strategy cards expose readiness and signal component maxima; NIFTY does not claim them", () => {
    const crude = parseStrategies(f.strategies, "").items;
    expect(crude.map(c => c.engine_id)).toContain("mcx-crudeoilm");
    const detail = parseStrategyDetail(f.strategy_crude, "");
    expect(detail.config.calibration_status).toBe("PRE_REGISTERED_UNVALIDATED");
    expect(detail.config.matches_running_worker).toBe(false);
    const nifty = parseStrategyDetail(f.strategy_nifty_v3, "");
    expect(nifty.config.policy_hash).toBe("synthetic-policy-hash");
    expect(nifty.dimensions.readiness).toBeNull();
  });
});
