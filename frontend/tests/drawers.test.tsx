import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DrawerHost } from "@/components/pages/drawers";
import { fixtureRoutes, fx, nav, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetNav("/positions"); resetStore(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const EXPIRED = "nifty-acts_v3:T4-00000000000000000000";
const CLOSED = "nifty-acts_v3:T1-00000000000000000000";

describe("position detail drawer (deep link ?position=<id>)", () => {
  it("renders nothing without a deep link and never requests detail", () => {
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("opens the expired open position with a red unresolved warning and unknown P&L as dashes", async () => {
    resetNav("/positions", `position=${encodeURIComponent(EXPIRED)}`);
    stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    const dialog = await screen.findByRole("dialog", { name: /NIFTY 25600\/26000 CE bear call spread/ });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/UNRESOLVED/);
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/excluded from P&L totals \(not treated as zero\)/);
    expect(within(dialog).getAllByText("EXPIRED NOT SETTLED").length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText(/EXPIRED CONTRACT NOT SETTLED/).length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/last known 28.15 \(not executable\)/)).toBeInTheDocument();
    expect(within(dialog).getByText("No journal events recorded for this position.")).toBeInTheDocument();
    expect(within(dialog).queryByText("₹0.00")).toBeNull();
  });

  it("shows the timeline, score + components + cycle, marks, orders/fills, P&L explanation and provenance for a closed trade", async () => {
    resetNav("/positions", `position=${encodeURIComponent(CLOSED)}`);
    stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    const dialog = await screen.findByRole("dialog");
    // timeline from journal events
    const timeline = within(dialog).getByRole("list", { name: "Journal events" });
    expect(within(timeline).getAllByRole("listitem").length).toBe(fx.position_detail_closed_spread.timeline.length);
    // score components + cycle id
    expect(within(dialog).getByText("cyc-10:00")).toBeInTheDocument();
    expect(within(dialog).getByText("momentum")).toBeInTheDocument();
    // marks chart is drawn only from real marks
    expect(within(dialog).getByText(/Recorded marks \(2\)/)).toBeInTheDocument();
    expect(within(dialog).getByRole("img", { name: "Recorded marks over time" })).toBeInTheDocument();
    // orders and fills are labelled simulated
    expect(within(dialog).getAllByText("simulated").length).toBeGreaterThan(0);
    // P&L explanation: formula, engine-recorded vs recomputed gross, charge components with status
    expect(within(dialog).getByText(/gross = \(short_entry - hedge_entry - short_exit \+ hedge_exit\) x quantity/)).toBeInTheDocument();
    expect(within(dialog).getByText("Recomputed gross (from fills)")).toBeInTheDocument();
    expect(within(dialog).getByText("Engine-recorded gross")).toBeInTheDocument();
    const charges = within(dialog).getByRole("table", { name: "Charges breakdown" });
    expect(within(charges).getByText("brokerage").closest("tr")).toHaveTextContent(/UNVERIFIED/);
    expect(within(charges).getByText("stt").closest("tr")).toHaveTextContent(/STATUTORY/);
    // provenance
    expect(within(dialog).getAllByText("acts_paper_trades").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("1 → 4 (4 events)")).toBeInTheDocument();
  });

  it("says 'No mark data recorded.' when a position has no marks", async () => {
    const futures = fx.position_detail_futures.position.position_id;
    resetNav("/positions", `position=${encodeURIComponent(futures)}`);
    stubApi({ ...fixtureRoutes(), [`positions/${futures}`]: fx.position_detail_futures });
    renderWithApp(<DrawerHost/>);
    expect(await screen.findByText("No mark data recorded.")).toBeInTheDocument();
  });

  it("closes with Escape by clearing the deep-link params", async () => {
    resetNav("/positions", `position=${encodeURIComponent(CLOSED)}&tab=closed`);
    stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    expect(nav.replaces.at(-1)).toBe("/positions?tab=closed");
  });

  it("a position that does not exist is an error state with retry, not a blank drawer", async () => {
    resetNav("/positions", "position=nope");
    stubApi({ ...fixtureRoutes(), "positions/nope": () => new Response('{"detail":"POSITION_NOT_FOUND"}', { status: 404 }) });
    renderWithApp(<DrawerHost/>);
    expect(await screen.findByText(/Position detail UNAVAILABLE/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 404/)).toBeInTheDocument();
  });
});

describe("order lifecycle drawer (?order=<id>&position=<id>)", () => {
  it("shows the simulated order, 'n/a (simulated)' as broker id, its fills and the position link", async () => {
    const orderId = `${CLOSED}:EXIT:SHORT`;
    resetNav("/orders", `order=${encodeURIComponent(orderId)}&position=${encodeURIComponent(CLOSED)}`);
    stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    const dialog = await screen.findByRole("dialog", { name: orderId });
    expect(within(dialog).getByText("n/a (simulated)")).toBeInTheDocument();
    expect(within(dialog).getByText("Internal order id")).toBeInTheDocument();
    expect(within(dialog).getAllByText("simulated").length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/No broker order exists/)).toBeInTheDocument();
    expect(within(dialog).getByRole("table", { name: "Order fills" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /bull put spread|NIFTY 25400/ }));
    expect(nav.pushes.at(-1)).toContain("position=");
  });

  it("derives the position id from the order id when the link carries only ?order=", async () => {
    const orderId = `${CLOSED}:ENTRY:HEDGE`;
    resetNav("/orders", `order=${encodeURIComponent(orderId)}`);
    const { calls } = stubApi(fixtureRoutes());
    renderWithApp(<DrawerHost/>);
    await screen.findByRole("dialog", { name: orderId });
    expect(decodeURIComponent(calls.find(c => c.includes("/positions/"))!)).toContain(`/positions/${CLOSED}`);
  });
});
