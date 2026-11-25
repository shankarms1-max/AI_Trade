import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPollStore } from "@/lib/poll-store";
import { useResource } from "@/hooks/use-resource";
import { backoffDelay } from "@/hooks/use-polling";
import { RequestError } from "@/lib/http";

const identity = (json: unknown) => json;
const flush = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("shared poll store", () => {
  it("two subscribers of one URL share exactly one request and one timer", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: 1 });
    const store = createPollStore({ fetcher });
    const a = vi.fn(), b = vi.fn();
    const offA = store.subscribe("/x", { interval: 15000, parse: identity }, a);
    const offB = store.subscribe("/x", { interval: 15000, parse: identity }, b);
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot("/x").data).toEqual({ ok: 1 });
    expect(a).toHaveBeenCalled(); expect(b).toHaveBeenCalled();
    expect(store.stats()).toMatchObject({ keys: 1, active: 1 });
    await flush(15000);
    expect(fetcher).toHaveBeenCalledTimes(2); // one interval tick for both subscribers
    offA(); offB();
  });

  it("different URLs poll independently but each only once", async () => {
    const fetcher = vi.fn().mockResolvedValue({});
    const store = createPollStore({ fetcher });
    const offs = [store.subscribe("/a", { interval: 15000, parse: identity }, () => {}), store.subscribe("/b", { interval: 15000, parse: identity }, () => {}), store.subscribe("/a", { interval: 15000, parse: identity }, () => {})];
    await flush();
    expect(fetcher.mock.calls.map(c => c[0]).sort()).toEqual(["/a", "/b"]);
    offs.forEach(o => o());
  });

  it("aborts the in-flight request when the last subscriber leaves and stops polling", async () => {
    let signal: AbortSignal | undefined;
    const fetcher = vi.fn((_url: string, o: { signal: AbortSignal }) => { signal = o.signal; return new Promise(() => {}); });
    const store = createPollStore({ fetcher });
    const off1 = store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    const off2 = store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    await flush();
    expect(signal!.aborted).toBe(false);
    off1();
    expect(signal!.aborted).toBe(false); // another consumer still needs it
    off2();
    expect(signal!.aborted).toBe(true);
    await flush(60000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(store.stats().active).toBe(0);
  });

  it("a strict-mode style subscribe/unsubscribe/subscribe fires no aborted request", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true });
    const store = createPollStore({ fetcher });
    store.subscribe("/x", { interval: 1000, parse: identity }, () => {})();
    store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("backs off exponentially after failures, keeps the last good data and marks it stale", async () => {
    let n = 0;
    const fetcher = vi.fn(async () => { n += 1; if (n === 1) return { v: 1 }; throw new RequestError("network", "down"); });
    const store = createPollStore({ fetcher, maxBackoffMs: 60000 });
    store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    await flush();
    expect(store.getSnapshot("/x")).toMatchObject({ data: { v: 1 }, failure: null, staleSince: null });
    await flush(1000); // second read fails
    const failed = store.getSnapshot<{ v: number }>("/x");
    expect(failed.data).toEqual({ v: 1 });               // previous data kept
    expect(failed.failure?.kind).toBe("network");
    expect(failed.staleSince).not.toBeNull();             // "stale since"
    expect(failed.consecutiveFailures).toBe(1);
    await flush(1000); await flush(2000);                // delays 1s, 2s, 4s ...
    expect(store.getSnapshot("/x").consecutiveFailures).toBe(3);
    expect([1, 2, 3, 4, 9].map(k => backoffDelay(1000, k))).toEqual([1000, 2000, 4000, 8000, 60000]);
    n = 0; // recover on next success
  });

  it("recovers: a later success clears the failure", async () => {
    let fail = true;
    const fetcher = vi.fn(async () => { if (fail) throw new RequestError("http", "HTTP 500", 500); return { ok: true }; });
    const store = createPollStore({ fetcher });
    store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    await flush();
    expect(store.getSnapshot("/x").failure?.status).toBe(500);
    expect(store.getSnapshot("/x").data).toBeNull();
    fail = false;
    await store.refresh("/x");
    expect(store.getSnapshot("/x")).toMatchObject({ data: { ok: true }, failure: null, consecutiveFailures: 0 });
  });

  it("does not poll while the document is hidden", async () => {
    let visible = false;
    const fetcher = vi.fn().mockResolvedValue({});
    const store = createPollStore({ fetcher, visible: () => visible });
    store.subscribe("/x", { interval: 1000, parse: identity }, () => {});
    await flush(5000);
    expect(fetcher).not.toHaveBeenCalled();
    visible = true;
    store.refreshAll();
    await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("a malformed payload becomes a typed failure and keeps previous data", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce({ n: 1 }).mockResolvedValueOnce({ n: "bad" });
    const store = createPollStore({ fetcher });
    const parse = (json: unknown) => { if (typeof (json as { n: unknown }).n !== "number") throw new RequestError("invalid_payload", "bad"); return json; };
    store.subscribe("/x", { interval: 1000, parse }, () => {});
    await flush(); await flush(1000);
    expect(store.getSnapshot("/x").failure?.kind).toBe("invalid_payload");
    expect(store.getSnapshot("/x").data).toEqual({ n: 1 });
  });
});

describe("useResource", () => {
  function Probe({ id }: { id: string }) { const r = useResource(`/probe/${id}`, identity, 1000); return <div data-testid={id}>{r.data ? "data" : r.failure ? "failed" : "loading"}</div>; }
  it("many components using one URL cause a single fetch", async () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL) => new Response(JSON.stringify({ a: 1 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<><Probe id="same"/><Probe id="same"/><Probe id="same"/></>);
    await flush(10);
    expect(fetchMock.mock.calls.filter(c => String(c[0]).includes("/probe/same"))).toHaveLength(1);
    expect(screen.getAllByTestId("same").every(n => n.textContent === "data")).toBe(true);
  });
});
