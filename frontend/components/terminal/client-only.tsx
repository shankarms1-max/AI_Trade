"use client";
import { useSyncExternalStore, type ReactNode } from "react";
import { LoadingState } from "@/components/terminal/states";

const subscribe = () => () => {};
/**
 * Terminal pages are live data views whose content depends on the browser clock (IST session date, "age" labels) and on the
 * URL. They are rendered on the client only, so server HTML can never disagree with the first client render (hydration).
 */
export function ClientOnly({ children }: { children: ReactNode }) {
  const mounted = useSyncExternalStore(subscribe, () => true, () => false);
  return mounted ? <>{children}</> : <LoadingState label="Loading page" rows={3}/>;
}
