"use client";
import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { GLOBAL_KEYS, parseFilters, type Filters, type RangeKey } from "@/lib/filters";

/** Pages whose data honours the global date range, and the range they use when the URL does not say. */
const DATED: Record<string, RangeKey> = { "/": "today", "/positions": "all", "/orders": "all", "/tradebook": "all", "/analytics": "all" };
export const defaultRangeFor = (pathname: string): RangeKey => DATED[pathname] ?? "all";
export const isDatedPath = (pathname: string) => pathname in DATED;

type UrlState = {
  filters: Filters;
  /** Current query string (no leading ?). */
  search: string;
  pathname: string;
  get: (key: string) => string | null;
  /** Merge query updates (null/"" removes a key). Global-filter changes also drop pagination. */
  set: (updates: Record<string, string | null | undefined>, options?: { push?: boolean }) => void;
};
const empty = new URLSearchParams();
const Ctx = createContext<UrlState>({ filters: parseFilters(empty), search: "", pathname: "/", get: () => null, set: () => {} });

/** URL-backed state: global filters + page-local params (tab, page, sort, ?position=…). Held in the URL so navigation never resets it. */
export function FiltersProvider({ children }: { children: ReactNode }) {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const search = params?.toString() ?? "";
  const set = useCallback((updates: Record<string, string | null | undefined>, options?: { push?: boolean }) => {
    const next = new URLSearchParams(search);
    let global = false;
    for (const [key, value] of Object.entries(updates)) {
      if (value == null || value === "") next.delete(key); else next.set(key, value);
      if ((GLOBAL_KEYS as readonly string[]).includes(key)) global = true;
    }
    if (global && !("offset" in updates)) next.delete("offset");
    const text = next.toString();
    const href = text ? `${pathname}?${text}` : pathname;
    if (options?.push) router.push(href, { scroll: false }); else router.replace(href, { scroll: false });
  }, [search, pathname, router]);
  const value = useMemo<UrlState>(() => ({ filters: parseFilters(new URLSearchParams(search)), search, pathname, get: key => new URLSearchParams(search).get(key), set }), [search, pathname, set]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
export const useUrlState = () => useContext(Ctx);
export const useFilters = () => useContext(Ctx).filters;
