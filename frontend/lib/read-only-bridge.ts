// Node-only module: secrets and upstream destinations must never enter a client bundle.
import { readFileSync, lstatSync } from "node:fs";

const paths: Record<string, Set<string>> = {
  nifty: new Set(["api/health", "api/dashboard/latest", "api/phase15/summary", "api/phase15/positions", "api/phase15/journal"]),
  commodity: new Set(["api/commodity-acts/summary", "api/commodity-acts/decisions", "api/commodity-acts/paper/summary",
    "api/commodity-acts/paper/positions", "api/commodity-acts/paper/orders", "api/commodity-acts/paper/fills", "api/commodity-acts/paper/events"]),
};
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

function commodityAuthorization(): string {
  const file = process.env.UNIFIED_COMMODITY_AUTH_FILE;
  if (!file) throw new Error("NOT_CONFIGURED");
  const info = lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 4096 || (process.platform !== "win32" && (info.mode & 0o077))) throw new Error("UNSAFE_SECRET");
  const auth = readFileSync(file, "utf8").trim();
  if (!/^(Basic|Bearer) [A-Za-z0-9+/=_-]+$/.test(auth)) throw new Error("INVALID_SECRET");
  return auth;
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
  for (const [key, value] of query) {
    if (seen.has(key) || !["instrument", "limit", "offset", "date", "strategy", "state"].includes(key) ||
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
