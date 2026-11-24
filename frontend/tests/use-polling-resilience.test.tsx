import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { backoffDelay, usePolling } from "@/hooks/use-polling";
import { RequestError } from "@/lib/http";
import { PollStatus } from "@/components/poll-status";

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-23T06:30:00Z")); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

/** A loader that honours the AbortSignal, like every fetchJson-backed client. */
const hanging = () => vi.fn((signal: AbortSignal) => new Promise<never>((_resolve, reject) => {
  signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
}));

describe("usePolling resilience", () => {
  it("aborts the in-flight request on unmount and never updates afterwards", async () => {
    const loader = hanging();
    const { result, unmount } = renderHook(() => usePolling(loader, 15000));
    await act(() => vi.advanceTimersByTimeAsync(1));
    const signal = loader.mock.calls[0][0];
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(120000));
    expect(loader).toHaveBeenCalledTimes(1);
    expect(result.current.failure).toBeNull();
  });

  it("aborts the superseded request when the loader changes", async () => {
    const first = hanging();
    const second = vi.fn().mockResolvedValue("next");
    const { result, rerender } = renderHook(({ loader }) => usePolling(loader, 15000), { initialProps: { loader: first as (s: AbortSignal) => Promise<string> } });
    await act(() => vi.advanceTimersByTimeAsync(1));
    rerender({ loader: second });
    expect(first.mock.calls[0][0].aborted).toBe(true);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(result.current.data).toBe("next");
    expect(result.current.failure).toBeNull();
  });

  it("times out a hung read with a typed timeout failure", async () => {
    const loader = hanging();
    const { result } = renderHook(() => usePolling(loader, 15000, true, { timeoutMs: 10000 }));
    await act(() => vi.advanceTimersByTimeAsync(9999));
    expect(result.current.failure).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(2));
    expect(loader.mock.calls[0][0].aborted).toBe(true);
    expect(result.current.failure?.kind).toBe("timeout");
    expect(result.current.error).toBe("REQUEST TIMED OUT");
  });

  it("backs off exponentially on consecutive failures, capped at 60 s, and resets on success", async () => {
    expect([1, 2, 3, 4, 5, 6].map(n => backoffDelay(5000, n))).toEqual([5000, 10000, 20000, 40000, 60000, 60000]);
    let fail = true;
    const loader = vi.fn(() => fail ? Promise.reject(new TypeError("fetch failed")) : Promise.resolve("ok"));
    const { result } = renderHook(() => usePolling(loader, 5000));
    const at = async (ms: number, calls: number) => { await act(() => vi.advanceTimersByTimeAsync(ms)); expect(loader).toHaveBeenCalledTimes(calls); };
    await at(1, 1);
    expect(result.current.failure?.kind).toBe("network");
    await at(4998, 1); await at(2, 2);        // +5 s
    await at(9998, 2); await at(2, 3);        // +10 s
    await at(19998, 3); await at(2, 4);       // +20 s
    await at(39998, 4); await at(2, 5);       // +40 s
    expect(result.current.consecutiveFailures).toBe(5);
    await at(59998, 5); await at(2, 6);       // capped at 60 s
    fail = false;
    await at(60000, 7);
    expect(result.current.failure).toBeNull();
    expect(result.current.consecutiveFailures).toBe(0);
    await at(5000, 8);                        // back to the normal cadence
  });

  it("never overlaps requests: manual and foreground refreshes queue behind the in-flight read", async () => {
    let concurrent = 0, peak = 0;
    const resolvers: Array<() => void> = [];
    const loader = vi.fn(() => { concurrent++; peak = Math.max(peak, concurrent); return new Promise<string>(resolve => resolvers.push(() => { concurrent--; resolve("v"); })); });
    const { result } = renderHook(() => usePolling(loader, 15000));
    await act(() => vi.advanceTimersByTimeAsync(1));
    await act(async () => { void result.current.refresh(); void result.current.refresh(); document.dispatchEvent(new Event("visibilitychange")); });
    expect(loader).toHaveBeenCalledTimes(1);
    await act(async () => { resolvers[0](); });
    expect(loader).toHaveBeenCalledTimes(2);
    await act(async () => { resolvers[1](); });
    expect(peak).toBe(1);
    expect(loader).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(1);
  });

  it("keeps the last good data with a visible stale-since indicator and a working retry", async () => {
    const loader = vi.fn<(s: AbortSignal) => Promise<string>>().mockResolvedValueOnce("good")
      .mockRejectedValueOnce(new RequestError("http", "HTTP 502", 502))
      .mockResolvedValueOnce("recovered");
    function Probe() {
      const poll = usePolling(loader, 15000);
      return <><PollStatus poll={poll} label="PROBE"/><span>{poll.data ?? "none"}</span></>;
    }
    render(<Probe/>);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText("good")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(15000));
    expect(screen.getByText("good")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("STALE since 12:00:00 IST");
    expect(screen.getByRole("status")).toHaveTextContent("API ERROR · HTTP 502");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry now" })); });
    expect(loader).toHaveBeenCalledTimes(3);
    expect(screen.getByText("recovered")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("distinguishes failure kinds when nothing was ever loaded", async () => {
    const loader = vi.fn().mockRejectedValue(new RequestError("not_configured", "x", 503, "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED"));
    function Probe() { return <PollStatus poll={usePolling(loader, 15000)} label="PROBE" notConfigured={<p>Not configured here</p>}/>; }
    render(<Probe/>);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("status")).toHaveTextContent("Not configured here");
    cleanup();
    const failing = vi.fn().mockRejectedValue(new TypeError("fetch failed"));
    function Failing() { return <PollStatus poll={usePolling(failing, 15000)} label="PROBE"/>; }
    render(<Failing/>);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("alert")).toHaveTextContent("PROBE UNAVAILABLE");
    expect(screen.getByRole("alert")).toHaveTextContent("API UNAVAILABLE");
  });
});
