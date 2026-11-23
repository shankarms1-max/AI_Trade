// Node-only module: secrets and upstream destinations must never enter a client bundle.
import { readFileSync, lstatSync } from "node:fs";
import { createHash, timingSafeEqual } from "node:crypto";

const paths: Record<string, Set<string>> = {
  nifty: new Set(["api/health", "api/dashboard/latest", "api/phase15/summary", "api/phase15/positions", "api/phase15/journal"]),
  commodity: new Set(["api/commodity-acts/summary", "api/commodity-acts/decisions", "api/commodity-acts/paper/summary",
    "api/commodity-acts/paper/positions", "api/commodity-acts/paper/orders", "api/commodity-acts/paper/fills", "api/commodity-acts/paper/events"]),
  "ast-direct": new Set(["api/ast-direct/status", "api/ast-direct/trades", "api/ast-direct/events", "api/ast-direct/performance"]),
};
const defaultKeys = ["instrument", "limit", "offset", "date", "strategy", "state"];
// AST Direct is a separate isolated stack: its own keys and value rules, never the NIFTY/Commodity origins.
const astRules: Record<string, RegExp> = { limit: /^\d+$/, offset: /^\d+$/, date: /^\d{4}-\d{2}-\d{2}$/,
  state: /^[A-Z_]{1,32}$/, side: /^(LONG|SHORT)$/, scope: /^(all|lifecycle)$/ };
let active = 0;
const headers = { "Cache-Control": "private, no-store", "Vary": "Authorization", "X-Content-Type-Options": "nosniff" };
const failure = (status: number, code: string) => Response.json({ status: "UNAVAILABLE", code }, { status, headers });

function origin(value: string | undefined): URL {
  if (!value) throw new Error("NOT_CONFIGURED");
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/" ||
      (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) {
    throw new Error("INVALID_ORIGIN");
  }
  return url;
}

function secret(file: string | undefined): string {
  if (!file) throw new Error("NOT_CONFIGURED");
  const info = lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 4096 || (process.platform !== "win32" && (info.mode & 0o077))) throw new Error("UNSAFE_SECRET");
  return readFileSync(file, "utf8").trim();
}

function commodityAuthorization(): string {
  const auth = secret(process.env.UNIFIED_COMMODITY_AUTH_FILE);
  if (!/^(Basic|Bearer) [A-Za-z0-9+/=_-]+$/.test(auth)) throw new Error("INVALID_SECRET");
  return auth;
}

/** Internal Docker service name or loopback over HTTP; anything else must be HTTPS. Never NIFTY/ACTS origins. */
function astOrigin(): URL {
  const value = process.env.AST_DIRECT_API_ORIGIN;
  if (!value) throw new Error("NOT_CONFIGURED");
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/" ||
      (url.protocol !== "https:" && !(url.protocol === "http:" && ["ast-direct-api", "localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) {
    throw new Error("INVALID_ORIGIN");
  }
  return url;
}

/** The viewer's ingress Basic credential, checked against a server-only SHA-256 digest; no ACTS gateway call. */
function astViewerAuthorized(viewer: string): boolean {
  const expected = secret(process.env.AST_DIRECT_VIEWER_AUTH_SHA256_FILE).toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(expected)) throw new Error("INVALID_SECRET");
  return timingSafeEqual(createHash("sha256").update(viewer).digest(), Buffer.from(expected, "hex"));
}

function astAuthorization(): string {
  const token = secret(process.env.AST_DIRECT_API_TOKEN_FILE);
  if (!/^[A-Za-z0-9._~+-]{32,512}$/.test(token)) throw new Error("INVALID_SECRET");
  return `Bearer ${token}`;
}

async function jsonBody(response: Response): Promise<unknown> {
  if (!response.headers.get("content-type")?.includes("application/json") || !response.body) throw new Error("INVALID_RESPONSE");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 2 * 1024 * 1024) throw new Error("RESPONSE_TOO_LARGE");
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  const data = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (data == null || typeof data !== "object") throw new Error("INVALID_RESPONSE");
  return data;
}

export async function readOnlyBridge(request: Request, source: string, segments: string[]): Promise<Response> {
  if (request.method !== "GET") return failure(405, "READ_ONLY");
  const path = segments.join("/");
  if (!paths[source]?.has(path)) return failure(404, "READ_PATH_NOT_ALLOWED");
  const query = new URL(request.url).searchParams;
  const seen = new Set<string>();
  const ast = source === "ast-direct";
  for (const [key, value] of query) {
    if (ast && (seen.has(key) || !Object.hasOwn(astRules, key) || !astRules[key].test(value) ||
        (key === "limit" && (+value < 1 || +value > 100)) || (key === "offset" && +value > 100000))) return failure(400, "INVALID_FILTER");
    if (ast) { seen.add(key); continue; }
    if (seen.has(key) || !defaultKeys.includes(key) ||
        value.length > 80 || !/^[A-Za-z0-9_-]+$/.test(value) ||
        (key === "instrument" && !["CRUDEOILM", "NATGASMINI"].includes(value)) ||
        (key === "limit" && (!/^\d+$/.test(value) || +value < 1 || +value > 100)) ||
        (key === "offset" && (!/^\d+$/.test(value) || +value > 100000))) return failure(400, "INVALID_FILTER");
    seen.add(key);
  }
  const viewer = request.headers.get("authorization");
  if (!viewer || viewer.length > 4096 || !/^Basic [A-Za-z0-9+/=]+$/.test(viewer)) return failure(401, "AUTHENTICATION_REQUIRED");
  if (active >= 8) return failure(429, "READ_CAPACITY_BUSY");
  active++;
  try {
    if (ast) {
      if (!astViewerAuthorized(viewer)) return failure(401, "AUTHENTICATION_REQUIRED");
      const url = new URL("/" + path, astOrigin());
      url.search = query.toString();
      const response = await fetch(url, { headers: { Accept: "application/json", Authorization: astAuthorization() },
        redirect: "error", cache: "no-store", signal: AbortSignal.timeout(5000) });
      if (!response.ok) return failure(503, "SOURCE_UNAVAILABLE");
      return Response.json(await jsonBody(response), { headers });
    }
    // Reuse the existing NIFTY gateway's authentication, never trust a spoofable
    // browser header or expose an unauthenticated aggregation endpoint.
    const nifty = origin(process.env.UNIFIED_NIFTY_ORIGIN);
    const auth = await fetch(new URL("/api/health", nifty), { headers: { Accept: "application/json", Authorization: viewer },
      redirect: "error", cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (auth.status === 401 || auth.status === 403) return failure(401, "AUTHENTICATION_REQUIRED");
    if (!auth.ok) return failure(503, "AUTH_VERIFIER_UNAVAILABLE");
    await jsonBody(auth);
    const upstream = source === "nifty" ? nifty : origin(process.env.UNIFIED_COMMODITY_ORIGIN);
    const url = new URL("/" + path, upstream);
    url.search = query.toString();
    const response = await fetch(url, { headers: { Accept: "application/json", Authorization: source === "nifty" ? viewer : commodityAuthorization() },
      redirect: "error", cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok) return failure(503, "SOURCE_UNAVAILABLE");
    return Response.json(await jsonBody(response), { headers });
  } catch { return failure(503, "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED"); }
  finally { active--; }
}
