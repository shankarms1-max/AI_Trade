import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AlertBanner } from "@/components/terminal/alerts";
import { DataTable } from "@/components/terminal/data-table";
import { DetailDrawer } from "@/components/terminal/drawer";
import { DateRangeSelector } from "@/components/terminal/filters";
import { ChartTooltip } from "@/components/terminal/charts";
import { MetricTile, PnL, StatusBadge, StrategyChip, Tabs, toneFor, Unavailable } from "@/components/terminal/primitives";
import { EmptyState, ErrorState, LoadingState, StaleBar } from "@/components/terminal/states";
import { RequestError } from "@/lib/http";
import { effectiveRange, parseFilters, previousSession, queryString, resolveRange, withGlobalFilters } from "@/lib/filters";
import { duration, istDateTime, num, pctFraction, rupees, signedRupees } from "@/lib/terminal-format";

afterEach(cleanup);

describe("PnL and unknown handling", () => {
  it("colours by sign, uses tabular signed money, and never prints zero for unknown", () => {
    const { container } = render(<><PnL value={125.5}/><PnL value={-30}/><PnL value={0}/><PnL value={null}/><PnL value={undefined} reason="MARK_OLDER_THAN_60s"/></>);
    const cells = Array.from(container.querySelectorAll(".tm-pnl"));
    expect(cells.map(c => c.textContent)).toEqual(["+₹125.50", "−₹30.00", "₹0.00", "—", "—MARK OLDER THAN 60s"]);
    expect(cells[0]).toHaveClass("pnl-pos"); expect(cells[1]).toHaveClass("pnl-neg"); expect(cells[2]).toHaveClass("pnl-zero");
    expect(cells[3]).toHaveClass("pnl-na");
    expect(cells[4]).toHaveAttribute("title", "Unavailable: MARK_OLDER_THAN_60s");
    expect(cells[3].textContent).not.toMatch(/0/);
  });
  it("formats numbers with unknown as an em dash", () => {
    expect([num(null), num(undefined), rupees(null), signedRupees(Number.NaN), pctFraction(null), duration(null), istDateTime(null)]).toEqual(Array(7).fill("—"));
    expect(rupees(-1234.5)).toBe("−₹1,234.50");
    expect(pctFraction(0.4737)).toBe("47.4%");
    expect(duration(95715)).toBe("1d 2h");
  });
  it("renders IST for aware and naive (UTC) timestamps", () => {
    expect(istDateTime("2026-11-24T09:30:00Z")).toBe("24 Nov 15:00:00");
    expect(istDateTime("2026-11-24T09:30:00")).toBe("24 Nov 15:00:00");
    expect(istDateTime("2026-11-24T15:00:00+05:30")).toBe("24 Nov 15:00:00");
  });
});

describe("status badges keep separate dimensions distinct", () => {
  it("enablement, session, feed, readiness, signal and position look different and are labelled", () => {
    render(<>
      <StatusBadge dimension="enablement" label="Execution" value="COLLECT_ONLY"/><StatusBadge dimension="session" label="Session" value="OPEN"/>
      <StatusBadge dimension="feed" label="Feed" value="STALE"/><StatusBadge dimension="readiness" label="Warm-up" value="WARMING_UP"/>
      <StatusBadge dimension="signal" label="Signal" value="NO_SETUP"/><StatusBadge dimension="position" label="Position" value="UNRESOLVED"/>
    </>);
    expect(screen.getByText("COLLECT ONLY").closest(".tm-badge")).toHaveAttribute("data-tone", "warn");
    expect(screen.getByText("OPEN").closest(".tm-badge")).toHaveAttribute("data-tone", "pos");
    expect(screen.getByText("STALE").closest(".tm-badge")).toHaveAttribute("data-tone", "warn");
    expect(screen.getByText("UNRESOLVED").closest(".tm-badge")).toHaveAttribute("data-tone", "crit");
    for (const label of ["Execution", "Session", "Feed", "Warm-up", "Signal", "Position"]) expect(screen.getByText(label)).toBeInTheDocument();
    expect(toneFor("feed", "DATA_STALE")).not.toBe(toneFor("position", "UNRESOLVED"));
    expect(toneFor("enablement", "DISABLED")).toBe("warn");
    expect(toneFor("none", "EXPIRED_NOT_SETTLED")).toBe("crit");
  });
  it("unavailable and chips render", () => {
    render(<><Unavailable reason="no capital configured"/><StrategyChip label="NIFTY ACTS V3" family="ACTS" version="ACTS_V3"/></>);
    expect(screen.getByText("Unavailable")).toBeInTheDocument(); expect(screen.getByText("no capital configured")).toBeInTheDocument();
    expect(screen.getByText("NIFTY ACTS V3").closest(".tm-chip")).toHaveStyle({ "--chip": "var(--tm-chart-1)" });
  });
  it("metric tiles label partial totals next to the value", () => {
    render(<MetricTile primary label="Total net P&L" value={<PnL value={-146.62}/>} warnNote note="PARTIAL · excludes 1 open position"/>);
    const tile = screen.getByRole("group", { name: "Total net P&L" });
    expect(within(tile).getByText("−₹146.62")).toBeInTheDocument();
    expect(within(tile).getByText(/PARTIAL/)).toHaveClass("warn");
  });
});

describe("DataTable", () => {
  type Row = { id: string; name: string; v: number | null; legs?: string };
  const rows: Row[] = [{ id: "a", name: "Alpha", v: 10, legs: "alpha-legs" }, { id: "b", name: "Beta", v: null }];
  const columns = [
    { key: "name", header: "Name", sortKey: "name", render: (r: Row) => r.name },
    { key: "v", header: "Value", align: "right" as const, sortKey: "v", render: (r: Row) => <PnL value={r.v}/> },
  ];
  it("renders a labelled scroll region, sticky first column, right-aligned numbers and a totals row", () => {
    render(<DataTable label="Demo" rows={rows} columns={columns} rowKey={r => r.id} footer={{ v: "₹10.00" }} footerLabel="Totals · all 2"/>);
    expect(screen.getByRole("region", { name: "Demo (scrollable)" })).toHaveClass("tm-table-wrap");
    const table = screen.getByRole("table", { name: "Demo" });
    expect(within(table).getByText("Name").closest("th")).toHaveClass("sticky-first");
    expect(within(table).getByText("Value").closest("th")).toHaveClass("tm-num");
    expect(within(table).getByText("Totals · all 2")).toBeInTheDocument();
    expect(within(table).getAllByText("—").length).toBeGreaterThan(0); // unknown is a dash
  });
  it("sorting is delegated to the API: the header calls onSort and rows are not reordered locally", () => {
    const onSort = vi.fn();
    render(<DataTable label="Demo" rows={rows} columns={columns} rowKey={r => r.id} sort={{ key: "name", desc: false }} onSort={onSort}/>);
    expect(screen.getByRole("columnheader", { name: /Name/ })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: "Sort by Value" }));
    expect(onSort).toHaveBeenCalledWith("v", true);
    fireEvent.click(screen.getByRole("button", { name: "Sort by Name" }));
    expect(onSort).toHaveBeenLastCalledWith("name", true); // toggles the active column to descending
    expect(within(screen.getByRole("table")).getAllByRole("row").slice(1).map(r => r.textContent?.slice(0, 5))).toEqual(["Alpha", "Beta—"].map(s => s.slice(0, 5)));
  });
  it("has no sort buttons unless the caller provides an API sort", () => {
    render(<DataTable label="Demo" rows={rows} columns={columns} rowKey={r => r.id}/>);
    expect(screen.queryByRole("button", { name: /Sort by/ })).toBeNull();
  });
  it("expands rows and keeps the expansion when the rows refresh", () => {
    const view = (data: Row[]) => <DataTable label="Demo" rows={data} columns={columns} rowKey={r => r.id} expand={r => r.legs ? <span>{r.legs}</span> : null}/>;
    const { rerender } = render(view(rows));
    fireEvent.click(screen.getByRole("button", { name: "Expand legs" }));
    expect(screen.getByText("alpha-legs")).toBeInTheDocument();
    rerender(view(rows.map(r => ({ ...r }))));
    expect(screen.getByText("alpha-legs")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse legs" }));
    expect(screen.queryByText("alpha-legs")).toBeNull();
  });
  it("row click and keyboard activate; interactive children do not double-fire", () => {
    const onRowClick = vi.fn();
    render(<DataTable label="Demo" rows={rows} columns={[{ key: "n", header: "N", render: r => <button type="button">{r.name}</button> }]} rowKey={r => r.id} onRowClick={onRowClick}/>);
    const row = screen.getAllByRole("row")[1];
    fireEvent.click(within(row).getByRole("button"));
    expect(onRowClick).not.toHaveBeenCalled();
    fireEvent.click(row);
    fireEvent.keyDown(row, { key: "Enter" });
    expect(onRowClick).toHaveBeenCalledTimes(2);
  });
  it("shows the caller's empty state when there are no rows", () => {
    render(<DataTable label="Demo" rows={[]} columns={columns} rowKey={(r: Row) => r.id} empty={<EmptyState>Nothing here</EmptyState>}/>);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
  });
});

describe("DetailDrawer", () => {
  function Host() {
    const [open, setOpen] = useState(false);
    return <><button onClick={() => setOpen(true)}>open it</button><DetailDrawer open={open} title="Position detail" subtitle="sub" onClose={() => setOpen(false)}><button>inside</button></DetailDrawer></>;
  }
  it("is an accessible modal dialog: labelled, Esc closes, focus returns to the opener", () => {
    render(<Host/>);
    const opener = screen.getByText("open it");
    opener.focus(); fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Position detail" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close details" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
  it("traps Tab inside the drawer", () => {
    render(<Host/>);
    fireEvent.click(screen.getByText("open it"));
    const inside = screen.getByText("inside");
    inside.focus();
    fireEvent.keyDown(inside, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Close details" })).toHaveFocus();
  });
});

describe("states", () => {
  it("loading, empty, error and disconnected are distinct and error states offer Retry", () => {
    const retry = vi.fn();
    render(<>
      <LoadingState label="Loading orders"/><EmptyState hint="hint">No orders</EmptyState>
      <ErrorState failure={new RequestError("http", "HTTP 500", 500)} label="Orders" onRetry={retry}/>
      <ErrorState failure={new RequestError("network", "down")} label="Positions" onRetry={retry}/>
    </>);
    expect(screen.getByText("Loading orders")).toBeInTheDocument();
    expect(screen.getByText("No orders")).toBeInTheDocument();
    expect(screen.getByText("Orders UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/DISCONNECTED · Positions/)).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Retry" });
    fireEvent.click(buttons[0]);
    expect(retry).toHaveBeenCalledOnce();
  });
  it("StaleBar says stale since when a refresh failed", () => {
    render(<StaleBar resource={{ failure: new RequestError("timeout", "t"), staleSince: Date.parse("2026-11-24T09:30:00Z"), nextAttemptAt: null, consecutiveFailures: 2, refresh: vi.fn() }} label="Summary"/>);
    expect(screen.getByText(/STALE since 15:00:00 IST/)).toBeInTheDocument();
    expect(screen.getByText(/2 failures/)).toBeInTheDocument();
  });
});

describe("alerts and tooltip", () => {
  it("AlertBanner is a prominent role=alert listing code, engine, message, since and action", () => {
    render(<AlertBanner items={[{ engine_id: "e", engine: "NIFTY ACTS V3", position_id: "e:T4", severity: "CRITICAL", code: "EXPIRED_POSITION_NOT_SETTLED", message: "expired", since: "2026-11-23", action: "Do not edit the journal" }, { engine_id: null, engine: null, position_id: null, severity: "WARNING", code: "FEED_STALE", message: "w", since: null, action: null }]}/>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-tone", "crit");
    expect(within(alert).getByText("EXPIRED_POSITION_NOT_SETTLED")).toBeInTheDocument();
    expect(within(alert).getByText(/since 23 Nov 2026/)).toBeInTheDocument();
    expect(within(alert).getByText(/Do not edit the journal/)).toBeInTheDocument();
    expect(within(alert).queryByText("FEED_STALE")).toBeNull(); // only CRITICAL in the banner
    expect(within(alert).getByRole("link", { name: "View position" })).toHaveAttribute("href", "/positions?tab=unresolved&position=e%3AT4");
  });
  it("ChartTooltip prints an em dash for unknown values", () => {
    render(<ChartTooltip active label="10:00" payload={[{ name: "Combined net", value: 12.5, dataKey: "a" }, { name: "Unrealized", value: null, dataKey: "b" }]}/>);
    expect(screen.getByText("+₹12.50")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});

describe("tabs and date range selector", () => {
  it("tabs expose counts and arrow-key navigation", () => {
    const onChange = vi.fn();
    render(<Tabs label="T" value="a" onChange={onChange} tabs={[{ id: "a", label: "Open", count: 3 }, { id: "b", label: "Closed", count: 5 }]}/>);
    expect(screen.getByRole("tab", { name: /Open/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getByRole("tab", { name: /Open/ }), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("b");
  });
  it("DateRangeSelector offers Today, Previous session, 7D, 30D, MTD, Custom, All and shows date inputs for Custom", () => {
    const onChange = vi.fn();
    const { rerender } = render(<DateRangeSelector value="7d" from={null} to={null} onChange={onChange}/>);
    const group = screen.getByRole("group", { name: "Date range" });
    expect(within(group).getAllByRole("button").map(b => b.textContent)).toEqual(["Today", "Previous session", "7D", "30D", "MTD", "Custom", "All"]);
    expect(within(group).getByRole("button", { name: "7D" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(group).getByRole("button", { name: "MTD" }));
    expect(onChange).toHaveBeenCalledWith("mtd", null, null);
    rerender(<DateRangeSelector value="custom" from="2026-11-01" to="2026-11-10" onChange={onChange}/>);
    fireEvent.change(screen.getByLabelText("To date"), { target: { value: "2026-11-12" } });
    expect(onChange).toHaveBeenLastCalledWith("custom", "2026-11-01", "2026-11-12");
  });
});

describe("filters library", () => {
  const now = new Date("2026-11-24T10:00:00Z"); // Tuesday 15:30 IST
  it("resolves every range in IST calendar dates", () => {
    expect(resolveRange("today", null, null, now)).toEqual({ from: "2026-11-24", to: "2026-11-24" });
    expect(resolveRange("prev", null, null, now)).toEqual({ from: "2026-11-23", to: "2026-11-23" });
    expect(resolveRange("7d", null, null, now)).toEqual({ from: "2026-11-18", to: "2026-11-24" });
    expect(resolveRange("30d", null, null, now)).toEqual({ from: "2026-10-26", to: "2026-11-24" });
    expect(resolveRange("mtd", null, null, now)).toEqual({ from: "2026-11-01", to: "2026-11-24" });
    expect(resolveRange("custom", "2026-11-02", null, now)).toEqual({ from: "2026-11-02", to: null });
    expect(resolveRange("all", null, null, now)).toEqual({ from: null, to: null });
    expect(previousSession("2026-11-23")).toBe("2026-11-20"); // Monday -> Friday
  });
  it("uses the IST date even when UTC is still the previous day", () => {
    expect(resolveRange("today", null, null, new Date("2026-11-24T20:00:00Z"))).toEqual({ from: "2026-11-25", to: "2026-11-25" });
  });
  it("parses filters from the URL, defaulting to PAPER and ignoring junk dates", () => {
    expect(parseFilters(new URLSearchParams(""))).toMatchObject({ mode: "PAPER", engine: null, range: null });
    expect(parseFilters(new URLSearchParams("mode=live&engine=x&from=2026-11-01&to=bad"))).toMatchObject({ mode: "LIVE", engine: "x", from: "2026-11-01", to: null, range: "custom" });
    expect(effectiveRange(parseFilters(new URLSearchParams("")), "today")).toBe("today");
  });
  it("builds stable sorted query strings (the URL is the cache key) and skips empty values", () => {
    expect(queryString({ b: 2, a: "x", c: null, d: "", e: undefined, f: false })).toBe("?a=x&b=2&f=false");
    expect(queryString({})).toBe("");
  });
  it("carries only the global filter keys across navigation", () => {
    expect(withGlobalFilters("/positions", "engine=nifty-acts_v3&range=7d&tab=closed&offset=50&position=x")).toBe("/positions?engine=nifty-acts_v3&range=7d");
    expect(withGlobalFilters("/orders?status=FILLED", "mode=LIVE")).toBe("/orders?status=FILLED&mode=LIVE");
    expect(withGlobalFilters("/risk", "")).toBe("/risk");
  });
});
