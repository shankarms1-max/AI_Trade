import { backoffDelay, MAX_BACKOFF_MS } from "@/hooks/use-polling";
import { fetchJson, REQUEST_TIMEOUT_MS, RequestError, toFailure } from "@/lib/http";

/**
 * One shared, keyed polling/cache layer for every terminal page. For each distinct URL there is exactly one in-flight
 * request and one timer, shared by all subscribers (cards, tables, header, drawers). The previous payload stays on screen
 * while a refresh runs or fails (so scroll, filters and selection never reset) and is flagged stale. Failures back off
 * exponentially. Hidden documents pause polling and foregrounding refreshes. Everything is read-only GET.
 */
export type ResourceState<T> = {
  data: T | null;
  failure: RequestError | null;
  /** true until the first read settles (success or failure). */
  loading: boolean;
  /** true while a request is in flight (including background refreshes). */
  fetching: boolean;
  lastSuccessAt: number | null;
  /** Epoch ms the shown data dates from, set only while a later refresh is failing. */
  staleSince: number | null;
  consecutiveFailures: number;
  nextAttemptAt: number | null;
};
export type Fetcher = (url: string, options: { signal: AbortSignal; timeoutMs: number }) => Promise<unknown>;
export type Subscription = { interval: number; parse: (json: unknown) => unknown };
const EMPTY: ResourceState<never> = { data: null, failure: null, loading: true, fetching: false, lastSuccessAt: null, staleSince: null, consecutiveFailures: 0, nextAttemptAt: null };

class Resource {
  state: ResourceState<unknown> = EMPTY;
  listeners = new Set<() => void>();
  subs = new Map<symbol, Subscription>();
  timer: ReturnType<typeof setTimeout> | undefined;
  controller: AbortController | null = null;
  failures = 0;
  queued = false;
  constructor(readonly key: string, private env: Env) {}
  interval() { return Math.min(...[...this.subs.values()].map(s => s.interval)); }
  parse(json: unknown) { return [...this.subs.values()][0].parse(json); }
  set(next: Partial<ResourceState<unknown>>) { this.state = { ...this.state, ...next }; this.listeners.forEach(l => l()); }
  clear() { if (this.timer !== undefined) clearTimeout(this.timer); this.timer = undefined; }
  schedule(delay: number) {
    this.clear();
    if (!this.subs.size) return;
    this.timer = setTimeout(() => { this.timer = undefined; if (this.env.visible()) void this.run(); }, Math.max(0, delay));
  }
  async run(): Promise<void> {
    if (!this.subs.size) return;
    if (this.controller) { this.queued = true; return; }
    this.clear();
    const request = new AbortController();
    this.controller = request;
    this.set({ fetching: true });
    const started = this.env.now();
    let ok = false;
    try {
      const json = await this.env.fetcher(this.key, { signal: request.signal, timeoutMs: this.env.timeoutMs });
      if (request.signal.aborted) return;
      const data = this.parse(json);
      ok = true;
      this.failures = 0;
      this.set({ data, failure: null, loading: false, fetching: false, lastSuccessAt: this.env.now(), staleSince: null, consecutiveFailures: 0, nextAttemptAt: null });
    } catch (error) {
      if (request.signal.aborted) return;
      this.failures += 1;
      const failure = toFailure(error);
      const delay = backoffDelay(this.interval(), this.failures, this.env.maxBackoffMs);
      this.set({
        failure, loading: false, fetching: false, consecutiveFailures: this.failures, nextAttemptAt: this.env.now() + delay,
        staleSince: this.state.data != null ? this.state.lastSuccessAt : null,
      });
    } finally {
      if (this.controller === request) this.controller = null;
    }
    if (!this.subs.size) return;
    if (this.queued) { this.queued = false; return this.run(); }
    const delay = ok ? this.interval() - (this.env.now() - started) : backoffDelay(this.interval(), this.failures, this.env.maxBackoffMs);
    if (delay <= 0) return this.run();
    this.schedule(delay);
  }
}
type Env = { fetcher: Fetcher; now: () => number; visible: () => boolean; timeoutMs: number; maxBackoffMs: number };

export function createPollStore(overrides: Partial<Env> = {}) {
  const env: Env = {
    fetcher: (url, options) => fetchJson(url, options), now: () => Date.now(),
    visible: () => typeof document === "undefined" || document.visibilityState !== "hidden",
    timeoutMs: REQUEST_TIMEOUT_MS, maxBackoffMs: MAX_BACKOFF_MS, ...overrides,
  };
  const resources = new Map<string, Resource>();
  const get = (key: string) => { let r = resources.get(key); if (!r) { r = new Resource(key, env); resources.set(key, r); } return r; };
  return {
    /** Subscribe a consumer to `key`. Returns the unsubscribe function; the last unsubscribe aborts the in-flight read. */
    subscribe(key: string, sub: Subscription, listener: () => void): () => void {
      const resource = get(key), id = Symbol(key);
      resource.subs.set(id, sub);
      resource.listeners.add(listener);
      if (resource.subs.size === 1) {
        const age = resource.state.lastSuccessAt == null ? Infinity : env.now() - resource.state.lastSuccessAt;
        if (resource.state.failure || age >= sub.interval) resource.schedule(0); // deferred: React strict-mode mount/unmount/mount must not fire (and abort) a request
        else resource.schedule(sub.interval - age);
      }
      return () => {
        resource.subs.delete(id);
        resource.listeners.delete(listener);
        if (!resource.subs.size) { resource.clear(); resource.queued = false; resource.controller?.abort("UNSUBSCRIBED"); resource.controller = null; resource.set({ fetching: false }); }
      };
    },
    getSnapshot<T>(key: string): ResourceState<T> { return (resources.get(key)?.state ?? EMPTY) as ResourceState<T>; },
    refresh(key: string): Promise<void> { return resources.get(key)?.run() ?? Promise.resolve(); },
    /** Re-run every active resource (used when a hidden tab becomes visible). */
    refreshAll() { resources.forEach(r => { if (r.subs.size) void r.run(); }); },
    stats() { return { keys: resources.size, active: [...resources.values()].filter(r => r.subs.size).length, inflight: [...resources.values()].filter(r => r.controller).length }; },
    reset() { resources.forEach(r => { r.clear(); r.controller?.abort("RESET"); }); resources.clear(); },
  };
}
export const pollStore = createPollStore();
if (typeof document !== "undefined") document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") pollStore.refreshAll(); });
export { RequestError };
