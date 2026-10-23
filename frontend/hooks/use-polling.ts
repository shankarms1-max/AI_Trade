"use client";
import { useCallback, useEffect, useRef, useState } from "react";

/** UI reads only. Visible cadence is unchanged; foregrounding always refreshes. */
export function usePolling<T>(loader: () => Promise<T>, interval = Number(process.env.NEXT_PUBLIC_POLL_INTERVAL_MS ?? 15000), enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  useEffect(() => {
    let active = true, inFlight = false, refreshQueued = false;
    const refresh = async () => {
      if (!active || !enabled || document.visibilityState === "hidden") return;
      if (inFlight) { refreshQueued = true; return; }
      inFlight = true;
      try { const value = await loader(); if (active) { setData(value); setError(null); } }
      catch { if (active) setError("API UNAVAILABLE"); }
      finally {
        inFlight = false;
        if (active) setLoading(false);
        if (active && refreshQueued) { refreshQueued = false; void refresh(); }
      }
    };
    refreshRef.current = refresh;
    const initial = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, interval);
    const foreground = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", foreground);
    return () => { active = false; window.clearTimeout(initial); window.clearInterval(timer); document.removeEventListener("visibilitychange", foreground); };
  }, [loader, interval, enabled]);
  const refresh = useCallback(() => refreshRef.current(), []);
  return { data, error, loading, refresh };
}
