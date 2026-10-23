import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DataTable } from "@/components/data-table";
import { ThemeToggle, THEME_KEY } from "@/components/theme-toggle";
import { awareTimestamp, commodityTrade, niftyTrade, realizedPath, type PortfolioTrade } from "@/lib/portfolio";
import { api } from "@/lib/api";
import type { Phase15PaperTrade } from "@/types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); localStorage.clear(); });
const trade = (id: string, realized: number | null, exitTimestamp: string | null): PortfolioTrade => ({
  id, source: "NIFTY", strategy: "ACTS V2", state: "CLOSED", realized, exitTimestamp,
  timestamp: null, contract: null, unrealized: null, reason: null,
});
describe("portfolio evidence", () => {
  it("sorts only observed priced exits deterministically without synthetic capital", () => {
    const rows = [trade("b", -30, "2026-10-22T05:00:00Z"), trade("a", 100, "2026-10-21T05:00:00Z")];
    const original = structuredClone(rows);
    expect(realizedPath(rows)).toMatchObject({ status: "AVAILABLE", excluded: 0, points: [{ id: "a", gross: 100 }, { id: "b", gross: 70 }] });
    expect(rows).toEqual(original);
  });
  it("labels unpriced closed evidence as excluded, not zero P&L", () => {
    expect(realizedPath([trade("a", 100, "2026-10-21T05:00:00Z"), trade("b", null, null)])).toMatchObject({ status: "PARTIAL", excluded: 1, points: [{ gross: 100 }] });
    expect(realizedPath([trade("b", null, null)])).toEqual({ status: "INCOMPLETE", points: [], excluded: 1 });
    expect(realizedPath([])).toEqual({ status: "EMPTY", points: [], excluded: 0 });
  });
  it("rejects nonfinite values and naive/invalid timestamps rather than inventing marks", () => {
    expect(awareTimestamp("2026-10-21T10:00:00")).toBeNull();
    expect(awareTimestamp("invalidZ")).toBeNull();
    expect(awareTimestamp("2026-10-21T10:00:00+05:30")).toBe("2026-10-21T10:00:00+05:30");
    expect(commodityTrade("CRUDEOILM", { state: "CLOSED", gross_realized_pnl: NaN }, 0).realized).toBeNull();
    expect(niftyTrade({ state: "OPEN", mark_available: false, current_unrealized_gross_pnl: 400 } as Phase15PaperTrade).unrealized).toBeNull();
    expect(commodityTrade("NATGASMINI", { state: "OPEN", unrealized_gross: null }, 0).unrealized).toBeNull();
    expect(commodityTrade("NATGASMINI", { state: "PENDING_PAPER_ENTRY", decision_timestamp: "2026-10-21T05:00:00Z" }, 0).timestamp).toBeNull();
    expect(niftyTrade({ state: "PENDING_PAPER_ENTRY", decision_timestamp: "2026-10-21T05:00:00Z" } as Phase15PaperTrade).timestamp).toBeNull();
  });
});
describe("shared table and themes", () => {
  it("filters and sorts loaded records with unavailable values last, without mutating source", () => {
    const rows = [{ id: "unknown", value: null }, { id: "second", value: 2 }, { id: "first", value: 1 }];
    render(<DataTable label="Evidence" rows={rows} rowKey={r => r.id} searchText={r => r.id} columns={[
      { key: "id", label: "Identity", value: r => r.id, render: r => r.id },
      { key: "value", label: "Gross", value: r => r.value, render: r => r.value ?? "Unavailable" },
    ]}/>);
    fireEvent.click(screen.getByRole("button", { name: /Gross/ }));
    const body = screen.getByRole("table").querySelector("tbody")!;
    expect(within(body).getAllByRole("row").map(r => r.textContent)).toEqual(["first1", "second2", "unknownUnavailable"]);
    fireEvent.click(screen.getByRole("button", { name: /Gross/ }));
    expect(within(body).getAllByRole("row").map(r => r.textContent)).toEqual(["second2", "first1", "unknownUnavailable"]);
    fireEvent.change(screen.getByRole("textbox", { name: "Search Evidence" }), { target: { value: " FIRST " } });
    expect(within(body).getAllByRole("row")).toHaveLength(1);
    expect(rows.map(r => r.id)).toEqual(["unknown", "second", "first"]);
  });
  it("persists light/dark preference and updates the root theme", () => {
    localStorage.setItem(THEME_KEY, "dark");
    render(<ThemeToggle/>);
    fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    fireEvent.click(screen.getByRole("button", { name: "Switch to dark theme" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
describe("production journal date compatibility", () => {
  it("omits the all label and resolves today to the current IST calendar date", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-22T20:00:00Z"));
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
    vi.stubGlobal("fetch", fetch);
    await api.phase15Journal({ date: "all", limit: 100 });
    expect(new URL(fetch.mock.calls[0][0], "http://localhost").searchParams.has("date")).toBe(false);
    await api.phase15Journal({ date: "today", offset: 0 });
    expect(new URL(fetch.mock.calls[1][0], "http://localhost").searchParams.get("date")).toBe("2026-10-23");
    await api.phase15Journal({ date: "2026-10-21" });
    expect(new URL(fetch.mock.calls[2][0], "http://localhost").searchParams.get("date")).toBe("2026-10-21");
  });
});
