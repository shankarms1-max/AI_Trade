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

describe("isolated AST Direct read-only source", () => {
  const token = "a".repeat(48);
  const astRequest = (query = "", auth: string | null = viewer) => request(`/monitor-data/ast-direct/api/ast-direct/trades${query}`, auth);
  const segments = ["api", "ast-direct", "trades"];
  beforeEach(async () => {
    const { createHash } = await import("node:crypto");
    const digestFile = join(directory, "viewer-sha256");
    writeFileSync(digestFile, createHash("sha256").update(viewer).digest("hex"), { mode: 0o600 });
    const tokenFile = join(directory, "ast-token");
    writeFileSync(tokenFile, token, { mode: 0o600 });
    vi.stubEnv("AST_DIRECT_VIEWER_AUTH_SHA256_FILE", digestFile);
    vi.stubEnv("AST_DIRECT_API_TOKEN_FILE", tokenFile);
    vi.stubEnv("AST_DIRECT_API_ORIGIN", "http://ast-direct-api:8010");
  });

  it("verifies the viewer locally, never calls the ACTS/NIFTY gateway and keeps the API token server-side", async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ items: [], total: 0 }));
    vi.stubGlobal("fetch", fetcher);
    const result = await readOnlyBridge(astRequest("?limit=50&date=2026-11-23&side=LONG&state=CLOSED"), "ast-direct", segments);
    expect(result.status).toBe(200);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0][0])).toBe("http://ast-direct-api:8010/api/ast-direct/trades?limit=50&date=2026-11-23&side=LONG&state=CLOSED");
    expect(String(fetcher.mock.calls[0][0])).not.toContain("nifty.test");
    expect(fetcher.mock.calls[0][1].headers.Authorization).toBe(`Bearer ${token}`);
    expect(fetcher.mock.calls[0][1].redirect).toBe("error");
    expect(await result.text()).not.toContain(token);
    expect(result.headers.get("cache-control")).toContain("no-store");
  });

  it("rejects missing or wrong viewer credentials before contacting AST", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(astRequest("", null), "ast-direct", segments)).status).toBe(401);
    expect((await readOnlyBridge(astRequest("", "Basic d3Jvbmc6dmlld2Vy"), "ast-direct", segments)).status).toBe(401);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each(["api/ast-direct/acknowledge", "api/ast-direct/kill-switch", "api/commodity-acts/summary", "api/health", "../status"])(
    "allows only the AST monitoring paths, not %s", async path => {
      const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
      expect((await readOnlyBridge(astRequest(), "ast-direct", path.split("/"))).status).toBe(404);
      expect(fetcher).not.toHaveBeenCalled();
    });

  it.each(["?limit=0", "?limit=101", "?offset=100001", "?side=BUY", "?scope=raw", "?date=yesterday", "?state=open",
    "?instrument=CRUDEOILM", "?limit=5&limit=6", "?url=http://evil", "?__proto__=x", "?constructor=x"])("rejects AST filter %s", async query => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(astRequest(query), "ast-direct", segments)).status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects mutations and fails closed on unsafe configuration", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const post = new Request("https://dashboard.test", { method: "POST", headers: { Authorization: viewer } });
    expect((await readOnlyBridge(post, "ast-direct", segments)).status).toBe(405);
    for (const origin of ["http://16.4.18.3:8010", "http://evil.test", "https://user:pw@ast.test", "http://ast-direct-api:8010/x"]) {
      vi.stubEnv("AST_DIRECT_API_ORIGIN", origin);
      expect((await readOnlyBridge(astRequest(), "ast-direct", segments)).status).toBe(503);
    }
    vi.stubEnv("AST_DIRECT_API_ORIGIN", "http://ast-direct-api:8010");
    chmodSync(join(directory, "ast-token"), 0o644);
    expect((await readOnlyBridge(astRequest(), "ast-direct", segments)).status).toBe(503);
    vi.stubEnv("AST_DIRECT_VIEWER_AUTH_SHA256_FILE", "");
    expect((await readOnlyBridge(astRequest(), "ast-direct", segments)).status).toBe(503);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not relax the existing NIFTY and Commodity rules", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    expect((await readOnlyBridge(request("/?side=LONG"), "commodity", ["api", "commodity-acts", "summary"])).status).toBe(400);
    expect((await readOnlyBridge(request("/?scope=all"), "nifty", ["api", "phase15", "summary"])).status).toBe(400);
    expect((await readOnlyBridge(request(), "commodity", ["api", "ast-direct", "status"])).status).toBe(404);
    vi.stubEnv("UNIFIED_NIFTY_ORIGIN", "http://ast-direct-api:8010");
    expect((await readOnlyBridge(request(), "nifty", ["api", "phase15", "summary"])).status).toBe(503);
    expect(fetcher).not.toHaveBeenCalled();
  });
});
