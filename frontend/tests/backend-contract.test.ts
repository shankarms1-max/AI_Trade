// Cross-stack contract: payloads produced by the REAL backend projection code (synthetic market input) must pass the
// frontend runtime guards unchanged. Regenerate the fixture whenever backend/app/api/{engines,mcx_futures}.py change.
import { describe, expect, it } from "vitest";
import fixture from "./fixtures/backend-contract-synthetic.json";
import { parseEngineStatus } from "../lib/engines";
import { parseMcxDecisions, parseMcxPerformance, parseMcxStatus, parseMcxTrades } from "../lib/mcx-futures";

describe("backend contract (synthetic payloads from the real projection code)", () => {
  it("is labelled synthetic", () => expect(fixture.label).toMatch(/^SYNTHETIC/));
  it("engines status passes the guard", () => expect(() => parseEngineStatus(fixture.engines, "engines")).not.toThrow());
  it("mcx status passes the guard", () => expect(() => parseMcxStatus(fixture.mcx_status, "status")).not.toThrow());
  it("mcx trades pass the guard", () => expect(() => parseMcxTrades(fixture.mcx_trades, "trades")).not.toThrow());
  it("mcx decisions pass the guard", () => expect(() => parseMcxDecisions(fixture.mcx_decisions, "decisions")).not.toThrow());
  it("mcx performance passes the guard", () => expect(() => parseMcxPerformance(fixture.mcx_performance, "perf")).not.toThrow());
});
