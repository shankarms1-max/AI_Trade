"use client";
import { useEffect, useSyncExternalStore } from "react";
import { Icon } from "@/components/icons";
export const THEME_KEY = "ai-trade-theme";
let temporaryTheme: "light" | "dark" | null = null;
function subscribe(listener: () => void) {
  window.addEventListener("storage", listener);
  window.addEventListener("ai-trade-theme", listener);
  return () => { window.removeEventListener("storage", listener); window.removeEventListener("ai-trade-theme", listener); };
}
function readTheme() {
  if (temporaryTheme) return temporaryTheme;
  try { return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark"; }
  catch { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
}
export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, readTheme, () => "dark");
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const next = theme === "dark" ? "light" : "dark";
  return <button className="icon-button theme-toggle" aria-label={`Switch to ${next} theme`} title={`Switch to ${next} theme`} onClick={() => {
    try { localStorage.setItem(THEME_KEY, next); temporaryTheme = null; } catch { temporaryTheme = next; }
    document.documentElement.dataset.theme = next;
    window.dispatchEvent(new Event("ai-trade-theme"));
  }}><Icon name={theme === "dark" ? "sun" : "moon"}/></button>;
}
