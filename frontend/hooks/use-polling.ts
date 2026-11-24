"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { describeFailure, REQUEST_TIMEOUT_MS, RequestError, toFailure } from "@/lib/http";

const configured = Number(process.env.NEXT_PUBLIC_POLL_INTERVAL_MS ?? 15000);
const DEFAULT_INTERVAL = Number.isFinite(configured) && configured >= 1000 ? configured : 15000;
export const MAX_BACKOFF_MS = 60_000;

export type PollingOptions = { timeoutMs?: number; maxBackoffMs?: number };
export type PollingState<T> = {
  data: T | null;
  /** Short human label of the current failure (null when the latest read succeeded). */
  error: string | null;
  failure: RequestError | null;
  loading: boolean;
  /** Epoch ms of the last successful read. */
  lastSuccessAt: number | null;
  /** Set while showing last-good data after a failed read: the epoch ms the data dates from. */
  staleSince: number | null;
  consecutiveFailures: number;
  /** Epoch ms of the next scheduled attempt while backing off; null otherwise. */
  nextAttemptAt: number | null;
  refresh: () => Promise<void>;
};

/** Delay after `failures` consecutive failures: interval, 2×, 4× … capped (never below the interval). */
export function backoffDelay(interval: number, failures: number, cap = MAX_BACKOFF_MS): number {
  return Math.max(interval, Math.min(cap, interval * 2 ** Math.max(0, failures - 1)));
}

type Snapshot<T> = Omit<PollingState<T>, "refresh" | "staleSince" | "error">;

/**
 * UI reads only. One timer and at most one request per hook: the next read is scheduled only after
 * the previous one settles. Each read gets its own AbortController, aborted on unmount, loader change
 * or timeout. Failures back off exponentially and keep the last good data (marked stale).
 * Hidden documents pause; foregrounding always refreshes.
 */
export function usePolling<T>(loader: (signal: AbortSignal) => Promise<T>, interval = DEFAULT_INTERVAL, enabled = true, options: PollingOptions = {}): PollingState<T> {
  const { timeoutMs = REQUEST_TIMEOUT_MS, maxBackoffMs = MAX_BACKOFF_MS } = options;
  const [state, setState] = useState<Snapshot<T>>({ data: null, failure: null, loading: true, lastSuccessAt: null, consecutiveFailures: 0, nextAttemptAt: null });
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    let active = true, inFlight = false, queued = false, failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    const clear = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
    const tick = () => { timer = undefined; if (document.visibilityState !== "hidden") void run(); };
    const schedule = (delay: number) => { clear(); timer = setTimeout(tick, delay); };
    const run = async (): Promise<void> => {
      if (!active || !enabled) return;
      if (inFlight) { queued = true; return; }
      clear();
      inFlight = true;
      const started = Date.now();
      const request = new AbortController();
      controller = request;
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; request.abort("REQUEST_TIMEOUT"); }, timeoutMs);
      let succeeded = false;
      try {
        const value = await loader(request.signal);
        if (!active) return;
        succeeded = true;
        failures = 0;
        const at = Date.now();
        setState({ data: value, failure: null, loading: false, lastSuccessAt: at, consecutiveFailures: 0, nextAttemptAt: null });
      } catch (error) {
        if (!active) return;
        failures += 1;
        const failure = timedOut ? new RequestError("timeout", `Request timed out after ${Math.round(timeoutMs / 1000)}s`) : toFailure(error);
        const next = Date.now() + backoffDelay(interval, failures, maxBackoffMs);
        setState(previous => ({ ...previous, failure, loading: false, consecutiveFailures: failures, nextAttemptAt: next }));
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        if (controller === request) controller = null;
      }
      if (!active) return;
      if (queued) { queued = false; return run(); }
      const delay = succeeded ? interval - (Date.now() - started) : backoffDelay(interval, failures, maxBackoffMs);
      if (delay <= 0) return run();
      schedule(delay);
    };
    refreshRef.current = run;
    if (enabled) schedule(0);
    const foreground = () => { if (document.visibilityState === "visible") void run(); };
    document.addEventListener("visibilitychange", foreground);
    return () => {
      active = false;
      clear();
      controller?.abort("UNMOUNTED");
      document.removeEventListener("visibilitychange", foreground);
    };
  }, [loader, interval, enabled, timeoutMs, maxBackoffMs]);
  const refresh = useCallback(() => refreshRef.current(), []);
  return {
    ...state,
    error: state.failure ? describeFailure(state.failure) : null,
    staleSince: state.failure && state.data != null ? state.lastSuccessAt : null,
    refresh,
  };
}
