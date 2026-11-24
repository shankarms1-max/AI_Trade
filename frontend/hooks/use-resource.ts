"use client";
import { useCallback, useState, useSyncExternalStore } from "react";
import { pollStore, type ResourceState } from "@/lib/poll-store";

/** Default cadences: live pages poll every 15 s; analytics and static reference data poll less often. */
export const LIVE_INTERVAL = 15_000;
export const ANALYTICS_INTERVAL = 60_000;
export const STATIC_INTERVAL = 120_000;

export type Resource<T> = ResourceState<T> & { refresh: () => Promise<void>; error: string | null; /** true when `data` is the previous URL's payload (same keepPrevious group) shown while the new URL loads. */ placeholder: boolean };
const IDLE = { data: null, failure: null, loading: false, fetching: false, lastSuccessAt: null, staleSince: null, consecutiveFailures: 0, nextAttemptAt: null } as ResourceState<never>;
const LOADING = { ...IDLE, loading: true } as ResourceState<never>;

/**
 * Subscribe to one URL in the shared poll store. Components that use the same URL share one request and one timer.
 * `url === null` disables the subscription (e.g. a closed drawer). The parse function should be module-level.
 */
export function useResource<T>(url: string | null, parse: (json: unknown) => T, interval = LIVE_INTERVAL, options: { keepPrevious?: string } = {}): Resource<T> {
  const [previous, setPrevious] = useState<{ group: string; data: T } | null>(null);
  const subscribe = useCallback((listener: () => void) => url == null ? () => {} : pollStore.subscribe(url, { interval, parse }, listener), [url, interval, parse]);
  const getSnapshot = useCallback(() => url == null ? IDLE : pollStore.getSnapshot<T>(url), [url]);
  const state = useSyncExternalStore(subscribe, getSnapshot, () => url == null ? IDLE : LOADING) as ResourceState<T>;
  const refresh = useCallback(() => url == null ? Promise.resolve() : pollStore.refresh(url), [url]);
  const group = options.keepPrevious;
  if (group && state.data != null && previous?.data !== state.data) setPrevious({ group, data: state.data });
  const placeholder = !!group && state.data == null && previous?.group === group && url != null;
  return { ...state, data: placeholder ? previous!.data : state.data, placeholder, refresh, error: state.failure ? state.failure.message : null };
}
