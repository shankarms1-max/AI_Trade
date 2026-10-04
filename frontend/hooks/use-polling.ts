"use client";
import { useCallback, useEffect, useState } from "react";

export function usePolling<T>(loader: () => Promise<T>, interval = Number(process.env.NEXT_PUBLIC_POLL_INTERVAL_MS ?? 15000)) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    try { const value = await loader(); setData(value); setError(null); }
    catch { setError("API UNAVAILABLE"); }
    finally { setLoading(false); }
  }, [loader]);
  useEffect(() => { const initial = window.setTimeout(refresh, 0); const timer = window.setInterval(refresh, interval); return () => { window.clearTimeout(initial); window.clearInterval(timer); }; }, [refresh, interval]);
  return { data, error, loading, refresh };
}
