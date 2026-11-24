import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EnginesPage from "@/app/engines/page";
import { EnginesOverview } from "@/components/engines";
import { ENGINE_STATES, type Engine } from "@/types/engines";
import fixtures from "./fixtures/engines-mcx.json";

/** SYNTHETIC fixture payloads only (tests/fixtures/engines-mcx.json). */
const base = fixtures.fixtures["/api/engines/status"];
let payload: unknown = base;
let status = 200;
beforeEach(() => {
  payload = base; status = 200;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    expect(new URL(url, "http://localhost").pathname).toBe("/api/engines/status");
    return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const engine = (overrides: Partial<Engine>): Engine => ({ ...(base.engines[0] as Engine), ...overrides });

describe("engines overview", () => {
  it("renders one card per engine with state, mode, instrument and the latest block reason", async () => {
    render(<EnginesOverview/>);
    const acts = await screen.findByRole("article", { name: "ACTS V3 engine" });
    expect(within(acts).getByText("RUNNING")).toBeInTheDocument();
    expect(within(acts).getByText("PAPER")).toBeInTheDocument();
    expect(within(acts).getByText("NIFTY")).toBeInTheDocument();
    const toggle = within(acts).getByText("Decision & position");
    const details = toggle.closest("details");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(toggle);
    expect(details).toHaveAttribute("open");
    expect(within(acts).getByText("SCORE_BELOW_ENTRY_THRESHOLD")).toBeInTheDocument();
    expect(within(acts).getByText(/SHORT_PUT_SPREAD SYNTH-NIFTY-25600PE · qty 75 @ 42.50/)).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(5);
    expect(screen.getByRole("link", { name: "All engines" })).toHaveAttribute("href", "/engines");
  });

  it("keeps realized and unrealized P&L separate and never shows 0 for unknown", async () => {
    render(<EnginesOverview/>);
    const acts = await screen.findByRole("article", { name: "ACTS V3 engine" });
    expect(within(acts).getByText("Realized today").nextSibling).toHaveTextContent("−₹120.00");
    expect(within(acts).getByText("Unrealized (open)").nextSibling).toHaveTextContent("₹337.50");
    const gas = screen.getByRole("article", { name: "MCX Futures · NATGASMINI engine" });
    for (const label of ["Realized today", "Unrealized (open)", "Trades today"]) {
      expect(within(gas).getByText(label).nextSibling).toHaveTextContent(/^—$/);
    }
    expect(within(gas).queryByText(/₹0\.00/)).not.toBeInTheDocument();
    const crude = screen.getByRole("article", { name: "MCX Futures · CRUDEOILM engine" });
    expect(within(crude).getByText("Realized today").nextSibling).toHaveTextContent("₹0.00");
    expect(within(crude).getByText("Unrealized (open)").nextSibling).toHaveTextContent(/^—$/);
    expect(within(crude).getByText("Last decision").nextSibling).toHaveTextContent(/^—$/);
  });

  it.each(ENGINE_STATES)("renders a distinct, labelled %s badge", async state => {
    payload = { ...base, engines: [engine({ id: state, label: `E-${state}`, state })] };
    render(<EnginesOverview/>);
    const card = await screen.findByRole("article", { name: `E-${state} engine` });
    const badge = card.querySelector(`.engine-state[data-state="${state}"] .badge`) as HTMLElement;
    expect(badge).toHaveTextContent(state.replaceAll("_", " "));
    const expected = { RUNNING: "good", ERROR: "bad", DATA_STALE: "warn", DATA_UNAVAILABLE: "warn", MARKET_CLOSED: "accent" }[state as string] ?? "neutral";
    expect(badge).toHaveAttribute("data-tone", expected);
  });

  it("renders a prominent LIVE warning", async () => {
    payload = { ...base, engines: [engine({ id: "live", label: "Live engine", mode: "LIVE" })] };
    render(<EnginesOverview/>);
    const card = await screen.findByRole("article", { name: "Live engine engine" });
    expect(card).toHaveClass("is-live");
    expect(within(card).getByRole("alert", { name: "LIVE mode warning" })).toHaveTextContent("LIVE MODE · real broker orders possible");
  });

  it("shows a typed malformed-payload error instead of guessed values", async () => {
    payload = { ...base, engines: [{ ...base.engines[0], state: "SLEEPING" }] };
    render(<EnginesOverview/>);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("ENGINE STATUS UNAVAILABLE");
    expect(alert).toHaveTextContent("MALFORMED RESPONSE");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry now" })).toBeInTheDocument();
  });

  it("explains a not-configured endpoint without an error alert", async () => {
    payload = { status: "UNAVAILABLE", code: "SOURCE_UNAVAILABLE_OR_NOT_CONFIGURED" }; status = 503;
    render(<EnginesOverview/>);
    expect(await screen.findByText("ENGINE STATUS NOT CONFIGURED")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("/engines page", () => {
  it("renders a read-only engine table with a row header per engine and no controls", async () => {
    payload = { ...base, engines: [...base.engines, engine({ id: "live", label: "Live engine", mode: "LIVE" })] };
    render(<EnginesPage/>);
    const table = await screen.findByRole("table", { name: "Engine status" });
    expect(table).toHaveAttribute("data-layout", "scroll");
    expect(table.parentElement).toHaveClass("table-wrap", "sticky-first");
    expect(within(table).getAllByRole("rowheader")).toHaveLength(6);
    expect(screen.getByText(/1 engine report LIVE mode: Live engine/)).toBeInTheDocument();
    const row = within(table).getByRole("rowheader", { name: /Replay research worker/ }).closest("tr") as HTMLElement;
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByRole("button", { name: /start|stop|kill|enable|disable/i })).not.toBeInTheDocument();
    expect(screen.getByText(/No start, stop or kill actions/)).toBeInTheDocument();
  });
});
