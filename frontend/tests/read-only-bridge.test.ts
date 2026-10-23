// @vitest-environment node
import { mkdtempSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readOnlyBridge } from "@/lib/read-only-bridge";

let directory: string;
const viewer = "Basic dmlld2VyOnRlc3Q=";
const secret = "Basic Y29tbW9kaXR5OnByaXZhdGU=";
const request = (path = "/monitor-data/commodity/api/commodity-acts/paper/summary", auth: string | null = viewer) =>
  new Request("https://dashboard.test" + path, { headers: auth ? { Authorization: auth } : {} });
const json = (data: unknown, status = 200) => Response.json(data, { status });

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "bridge-test-"));
  const file = join(directory, "read-auth");
  writeFileSync(file, secret, { mode: 0o600 });
  chmodSync(file, 0o600);
  vi.stubEnv("UNIFIED_NIFTY_ORIGIN", "https://nifty.test");
  vi.stubEnv("UNIFIED_COMMODITY_ORIGIN", "https://commodity.test");
  vi.stubEnv("UNIFIED_COMMODITY_AUTH_FILE", file);
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); rmSync(directory, { recursive: true }); });

describe("authenticated read-only dashboard bridge", () => {
  it("verifies existing NIFTY authentication then uses server-only Commodity credentials", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ status: "HEALTHY" })).mockResolvedValueOnce(json({ instruments: [] }));
    vi.stubGlobal("fetch", fetcher);
    const result = await readOnlyBridge(request(), "commodity", ["api", "commodity-acts", "paper", "summary"]);
    expect(result.status).toBe(200);
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe(viewer);
    expect(fetcher.mock.calls[1][1].headers.Authorization).toBe(secret);
    expect(String(fetcher.mock.calls[1][0])).toBe("https://commodity.test/api/commodity-acts/paper/summary");
    expect(result.headers.get("cache-control")).toContain("no-store");
    expect(await result.text()).not.toContain(secret);
    expect(result.headers.get("set-cookie")).toBeNull();
  });
  it("rejects missing or invalid viewer auth before contacting Commodity", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(request(undefined, null), "commodity", ["api", "commodity-acts", "summary"])).status).toBe(401);
    expect(fetcher).not.toHaveBeenCalled();
    expect((await readOnlyBridge(request(), "commodity", ["api", "commodity-acts", "summary"])).status).toBe(401);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it.each(["orders/place", "../health", "api/commodity-acts/paper/kill", "https://evil.test"])("does not forward arbitrary path %s", async path => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(request(), "commodity", [path])).status).toBe(404);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects mutations, unknown filters, redirects and invalid JSON", async () => {
    const mutation = new Request("https://dashboard.test", { method: "POST" });
    expect((await readOnlyBridge(mutation, "commodity", ["api", "commodity-acts", "summary"])).status).toBe(405);
    expect((await readOnlyBridge(request("/?url=evil"), "commodity", ["api", "commodity-acts", "summary"])).status).toBe(400);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("private upstream error", { headers: { "content-type": "text/html" } })));
    const response = await readOnlyBridge(request(), "commodity", ["api", "commodity-acts", "summary"]);
    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain("private upstream error");
  });
  it("fails closed without configuration or trusted TLS", async () => {
    vi.stubEnv("UNIFIED_NIFTY_ORIGIN", "http://16.4.18.3");
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(request(), "commodity", ["api", "commodity-acts", "summary"])).status).toBe(503);
    expect(fetcher).not.toHaveBeenCalled();
  });
});
