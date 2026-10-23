import type { CSSProperties, ReactNode } from "react";
export type IconName = "grid" | "chart" | "layers" | "flame" | "gas" | "history" | "pulse" | "shield" | "sun" | "moon" | "menu" | "close" | "arrow" | "chevron" | "clock" | "search" | "refresh" | "wallet";
const paths: Record<IconName, ReactNode> = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/></>,
  chart: <><path d="M3 3v18h18M7 15l4-5 4 3 6-8"/><path d="M17 5h4v4"/></>,
  layers: <><path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5"/></>,
  flame: <path d="M12 3c1 5 6 5 6 11a6 6 0 0 1-12 0c0-3 2-5 3-6 0 3 1 4 2 4 2-3 2-6 1-9Z"/>,
  gas: <><path d="M12 3S5 10 5 15a7 7 0 0 0 14 0c0-5-7-12-7-12Z"/><path d="M9 16c0 2 1 3 3 3"/></>,
  history: <path d="M3 11a9 9 0 1 1 3 8M3 4v7h7M12 7v5l3 2"/>,
  pulse: <path d="M2 12h5l3-8 4 16 3-8h5"/>,
  shield: <><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6l-8-3Z"/><path d="m8 12 3 3 5-6"/></>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/></>,
  moon: <path d="M20 14A8 8 0 0 1 10 4a8.5 8.5 0 1 0 10 10Z"/>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>, close: <path d="m6 6 12 12M6 18 18 6"/>,
  arrow: <path d="M5 12h14m-5-5 5 5-5 5"/>, chevron: <path d="m9 5 7 7-7 7"/>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/></>,
  wallet: <><rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 8V6l13-3v2M21 11h-6v5h6M17 13.5h.01"/></>,
};
export function Icon({ name, size = 20, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>{paths[name]}</svg>;
}
