/**
 * Shared read-only HTTP layer: every GET has a timeout, honours a caller AbortSignal and
 * fails with a typed RequestError so the UI can tell HTTP / timeout / network / not-configured /
 * malformed-payload apart. No credentials or secrets live here (NEXT_PUBLIC_* holds only URLs).
 */
export const REQUEST_TIMEOUT_MS = 10_000;
/** Public URL only (same-origin "" in production behind Caddy); NEXT_PUBLIC_* must never carry credentials. */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
export type FailureKind = "http" | "timeout" | "network" | "not_configured" | "invalid_payload" | "aborted";

export class RequestError extends Error {
  constructor(public kind: FailureKind, message: string, public status: number | null = null, public code: string | null = null) {
    super(message);
    this.name = "RequestError";
  }
}

/** Raised by runtime guards when a payload does not match the contract. */
export class PayloadError extends RequestError {
  constructor(public path: string, detail: string) { super("invalid_payload", `Malformed response at ${path || "root"}: ${detail}`); }
}

const TIMEOUT_REASON = "REQUEST_TIMEOUT";

/** Combine a caller signal with a timeout; `dispose` must be called when the request settles. */
export function withTimeout(signal: AbortSignal | undefined, ms: number): { signal: AbortSignal; dispose: () => void } {
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal?.reason);
  if (signal?.aborted) controller.abort(signal.reason);
  else signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => controller.abort(TIMEOUT_REASON), ms);
  return { signal: controller.signal, dispose: () => { clearTimeout(timer); signal?.removeEventListener("abort", onAbort); } };
}

const NOT_CONFIGURED = /NOT_CONFIGURED/;

async function errorCode(response: Response): Promise<string | null> {
  try {
    const body = await response.json() as { code?: unknown; detail?: unknown };
    return typeof body?.code === "string" ? body.code : typeof body?.detail === "string" ? body.detail : null;
  } catch { return null; }
}

export async function fetchJson(url: string, options: { signal?: AbortSignal; timeoutMs?: number; credentials?: RequestCredentials } = {}): Promise<unknown> {
  const { signal, dispose } = withTimeout(options.signal, options.timeoutMs ?? REQUEST_TIMEOUT_MS);
  try {
    let response: Response;
    try {
      response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal,
        ...(options.credentials ? { credentials: options.credentials } : {}) });
    } catch {
      if (signal.aborted) throw signal.reason === TIMEOUT_REASON
        ? new RequestError("timeout", `Request timed out after ${Math.round((options.timeoutMs ?? REQUEST_TIMEOUT_MS) / 1000)}s`)
        : new RequestError("aborted", "Request cancelled");
      throw new RequestError("network", "Network unreachable");
    }
    if (!response.ok) {
      const code = await errorCode(response);
      if (response.status === 503 && code && NOT_CONFIGURED.test(code)) throw new RequestError("not_configured", "Source not configured in this deployment", 503, code);
      throw new RequestError("http", `HTTP ${response.status}`, response.status, code);
    }
    try { return await response.json(); }
    catch {
      if (signal.aborted) throw new RequestError(signal.reason === TIMEOUT_REASON ? "timeout" : "aborted", "Request interrupted");
      throw new RequestError("invalid_payload", "Response was not JSON", response.status);
    }
  } finally { dispose(); }
}

/** Normalise anything a loader can throw into a typed failure. */
export function toFailure(error: unknown, timedOut = false): RequestError {
  if (error instanceof RequestError) return error;
  if (timedOut) return new RequestError("timeout", "Request timed out");
  if (error instanceof Error && error.name === "AbortError") return new RequestError("aborted", "Request cancelled");
  if (error instanceof TypeError) return new RequestError("network", "Network unreachable");
  const status = typeof (error as { status?: unknown })?.status === "number" ? (error as { status: number }).status : null;
  return status != null ? new RequestError("http", `HTTP ${status}`, status) : new RequestError("network", "Source unreachable");
}

export const FAILURE_LABEL: Record<FailureKind, string> = {
  http: "API ERROR",
  timeout: "REQUEST TIMED OUT",
  network: "API UNAVAILABLE",
  not_configured: "NOT CONFIGURED",
  invalid_payload: "MALFORMED RESPONSE",
  aborted: "REQUEST CANCELLED",
};
export function describeFailure(failure: RequestError): string {
  const label = FAILURE_LABEL[failure.kind];
  if (failure.kind === "http") return `${label} · HTTP ${failure.status ?? "?"}${failure.code ? ` · ${failure.code}` : ""}`;
  if (failure.kind === "not_configured") return `${label} · ${failure.code ?? "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED"}`;
  return label;
}
