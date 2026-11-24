import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchJson, PayloadError, RequestError, toFailure } from "@/lib/http";
import { parseEngineStatus } from "@/lib/engines";
import { parseMcxStatus, parseMcxTrades } from "@/lib/mcx-futures";
import { dash, dateIST, istDateKey, parseTimestamp, timeIST } from "@/lib/format";
import fixtures from "./fixtures/engines-mcx.json";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("typed request failures", () => {
  it("classifies HTTP errors with status and backend code", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "boom" }, 500)));
    const error = await fetchJson("https://api.example/x").catch(e => e);
    expect(error).toBeInstanceOf(RequestError);
    expect(error).toMatchObject({ kind: "http", status: 500, code: "boom" });
  });
  it("classifies the bridge's 503 SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED as not configured", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ status: "UNAVAILABLE", code: "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED" }, 503)));
    await expect(fetchJson("/monitor-data/ast-direct/api/ast-direct/status")).rejects.toMatchObject({ kind: "not_configured", status: 503, code: "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED" });
  });
  it("keeps a plain 503 as an HTTP error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ code: "SOURCE_UNAVAILABLE" }, 503)));
    await expect(fetchJson("/x")).rejects.toMatchObject({ kind: "http", status: 503 });
  });
  it("classifies network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
    await expect(fetchJson("/x")).rejects.toMatchObject({ kind: "network" });
  });
  it("times out a hung request and aborts it", async () => {
    vi.useFakeTimers();
    let seen: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      seen = init.signal!;
      init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const pending = fetchJson("/x", { timeoutMs: 10_000 }).catch(e => e);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await pending).toMatchObject({ kind: "timeout" });
    expect(seen?.aborted).toBe(true);
  });
  it("reports caller cancellation as aborted, not as an outage", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const controller = new AbortController();
    const pending = fetchJson("/x", { signal: controller.signal }).catch(e => e);
    controller.abort();
    expect(await pending).toMatchObject({ kind: "aborted" });
  });
  it("rejects non-JSON bodies as malformed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>", { status: 200 })));
    await expect(fetchJson("/x")).rejects.toMatchObject({ kind: "invalid_payload" });
  });
  it("normalises unknown loader errors", () => {
    expect(toFailure(new Error("private detail")).kind).toBe("network");
    expect(toFailure(new Error("x"), true).kind).toBe("timeout");
    expect(toFailure(Object.assign(new Error("x"), { status: 404 }))).toMatchObject({ kind: "http", status: 404 });
  });
});

describe("runtime contract guards", () => {
  const engines = fixtures.fixtures["/api/engines/status"];
  it("accepts the engines contract and preserves nulls as unknown", () => {
    const parsed = parseEngineStatus(engines, "");
    expect(parsed.engines).toHaveLength(5);
    const gas = parsed.engines.find(e => e.id === "mcx-futures-natgasmini")!;
    expect(gas.trades_today).toBeNull();
    expect(gas.realized_pnl_today).toBeNull();
    expect(gas.last_decision?.score).toBeNull();
  });
  it.each([
    ["engines not an array", { ...engines, engines: {} }, "engines"],
    ["unknown engine state", { ...engines, engines: [{ ...engines.engines[0], state: "SLEEPING" }] }, "engines[0].state"],
    ["P&L as a string", { ...engines, engines: [{ ...engines.engines[0], realized_pnl_today: "12" }] }, "engines[0].realized_pnl_today"],
    ["non-finite score", { ...engines, engines: [{ ...engines.engines[0], last_decision: { ...engines.engines[0].last_decision, score: Number.NaN } }] }, "engines[0].last_decision.score"],
    ["bad generated_at", { ...engines, generated_at: "yesterday" }, "generated_at"],
  ])("rejects %s with a typed PayloadError", (_name, payload, path) => {
    let error: unknown;
    try { parseEngineStatus(payload, ""); } catch (caught) { error = caught; }
    expect(error).toBeInstanceOf(PayloadError);
    expect(error).toMatchObject({ kind: "invalid_payload", path });
  });
  it("drops external detail links instead of rendering them", () => {
    const parsed = parseEngineStatus({ ...engines, engines: [{ ...engines.engines[0], detail_href: "https://evil.example/" }] }, "");
    expect(parsed.engines[0].detail_href).toBeNull();
  });
  it("validates MCX status, trades and instrument identities", () => {
    expect(parseMcxStatus(fixtures.fixtures["/api/mcx-futures/status"], "").instruments.map(i => i.instrument)).toEqual(["CRUDEOILM", "NATGASMINI"]);
    expect(parseMcxTrades(fixtures.fixtures["/api/mcx-futures/trades"], "").items[1].net_pnl).toBeNull();
    const status = fixtures.fixtures["/api/mcx-futures/status"];
    expect(() => parseMcxStatus({ ...status, instruments: [{ ...status.instruments[0], instrument: "GOLDM" }] }, "")).toThrow(PayloadError);
    expect(() => parseMcxStatus({ ...status, mode: "LIVE" }, "")).toThrow(/mode/);
  });
});

describe("Asia/Kolkata display", () => {
  it("treats timestamps without an offset as UTC, never browser-local", () => {
    expect(parseTimestamp("2026-10-23T06:30:00")).toBe(Date.parse("2026-10-23T06:30:00Z"));
    expect(parseTimestamp("2026-10-23 06:30:00")).toBe(Date.parse("2026-10-23T06:30:00Z"));
    expect(timeIST("2026-10-23T06:30:00")).toBe("12:00:00 IST");
    expect(timeIST("2026-10-23T12:00:00+05:30")).toBe("12:00:00 IST");
    expect(parseTimestamp("not a time")).toBeNull();
    expect(timeIST("not a time")).toBe("N/A");
  });
  it("groups by IST calendar date", () => {
    expect(istDateKey("2026-10-22T18:45:00Z")).toBe("2026-10-23");
    expect(istDateKey("2026-10-22T18:15:00")).toBe("2026-10-22");
    expect(dateIST("2026-10-22T18:45:00Z")).toBe("23 Oct 2026");
  });
  it("renders unknown as an em dash, never zero", () => {
    expect(dash(null)).toBe("—");
    expect(dash(undefined)).toBe("—");
    expect(dash(Number.NaN)).toBe("—");
    expect(dash(0)).toBe("0");
  });
});
