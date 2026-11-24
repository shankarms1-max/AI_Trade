"use client";
import { useCallback, useRef, useSyncExternalStore } from "react";
import { pollStore, type ResourceState } from "@/lib/poll-store";

/** Default cadences: live pages poll every 15 s; analytics and static reference data poll less often. */
export const LIVE_INTERVAL = 15_000;
export const ANALYTICS_INTERVAL = 60_000;
export const STATIC_INTERVAL = 120_000;

export type Resource<T> = ResourceState<T> & { refresh: () => Promise<void>; error: string | null };
const IDLE = { data: null, failure: null, loading: false, fetching: false, lastSuccessAt: null, staleSince: null, consecutiveFailures: 0, nextAttemptAt: null } as ResourceState<never>;
const LOADING = { ...IDLE, loading: true } as ResourceState<never>;

/**
 * Subscribe to one URL in the shared poll store. Components that use the same URL share one request and one timer.
 * `url === null` disables the subscription (e.g. a closed drawer). The parse function should be module-level.
 */
export function useResource<T>(url: string | null, parse: (json: unknown) => T, interval = LIVE_INTERVAL): Resource<T> {
  const parseRef = useRef(parse);
  parseRef.current = parse;
  const subscribe = useCallback((listener: () => void) => url == null ? () => {} : pollStore.subscribe(url, { interval, parse: json => parseRef.current(json) }, listener), [url, interval]);
  const getSnapshot = useCallback(() => url == null ? IDLE : pollStore.getSnapshot<T>(url), [url]);
  const state = useSyncExternalStore(subscribe, getSnapshot, () => url == null ? IDLE : LOADING) as ResourceState<T>;
  const refresh = useCallback(() => url == null ? Promise.resolve() : pollStore.refresh(url), [url]);
  return { ...state, refresh, error: state.failure ? state.failure.message : null };
}
