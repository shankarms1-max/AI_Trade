import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Navigation } from "@/components/navigation";
import { renderWithApp, resetNav, resetStore } from "./helpers/terminal";
import { nav } from "./helpers/nav-mock";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => { resetStore(); resetNav("/"); });
afterEach(cleanup);

const page = (route: string) => `app${route === "/" ? "" : route}/page.tsx`;

describe("routes", () => {
  it.each(["/", "/nifty", "/phase15", "/nifty/research", "/engines", "/mcx-futures", "/mcx-futures/[instrument]", "/ast-futures", "/commodity-acts", "/commodity-acts/[instrument]",
    "/history", "/history/[snapshotId]", "/shadow", "/shadow/[tradeId]", "/system"])("existing route %s is still served", route => {
    expect(existsSync(page(route))).toBe(true);
  });
  it.each(["/positions", "/orders", "/tradebook", "/strategies", "/strategies/[engineId]", "/analytics", "/risk", "/research", "/settings"])("new route %s exists", route => {
    expect(existsSync(page(route))).toBe(true);
  });
  it("/trades has no page of its own: it is redirected to /tradebook (see next.config.ts)", () => {
    expect(existsSync("app/trades/page.tsx")).toBe(false);
    expect(readFileSync("next.config.ts", "utf8")).toContain('source: "/trades", destination: "/tradebook"');
  });
  it("/positions is the new positions page, not the legacy Phase 15 view", () => {
    const src = readFileSync(page("/positions"), "utf8");
    expect(src).toContain("components/pages/positions");
    expect(src).not.toContain("phase15-paper-trades");
  });
  it("legacy components stay available to their own routes", () => {
    expect(existsSync("components/phase15-paper-trades.tsx")).toBe(true);
    expect(readFileSync("components/phase15-paper-trades.tsx", "utf8")).toContain("Phase15Positions");
    expect(readFileSync("app/phase15/page.tsx", "utf8")).toContain("ACTS Paper Monitor");
  });
});

describe("navigation keeps global filters", () => {
  it("carries mode, engine, instrument, version and date range onto every link but not page-local params", () => {
    resetNav("/positions", "engine=nifty-acts_v3&instrument=NIFTY&range=7d&mode=PAPER&tab=closed&offset=50&position=x");
    renderWithApp(<Navigation/>);
    const orders = within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "Orders" });
    const q = new URL(orders.getAttribute("href")!, "http://x").searchParams;
    expect(Object.fromEntries(q)).toEqual({ engine: "nifty-acts_v3", instrument: "NIFTY", range: "7d", mode: "PAPER" });
    const legacy = within(screen.getByRole("navigation", { name: "Legacy" })).getByRole("link", { name: "Engines" });
    expect(legacy.getAttribute("href")).toContain("engine=nifty-acts_v3");
  });
  it("marks the current page with aria-current and exposes a skip link", () => {
    resetNav("/orders");
    renderWithApp(<Navigation/>);
    expect(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "Orders" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#workspace-content");
    expect(screen.getByRole("complementary", { name: "Workspace navigation" })).toBeInTheDocument();
  });
  it("mobile drawer is a labelled modal dialog reachable by keyboard", () => {
    renderWithApp(<Navigation/>);
    const more = screen.getByRole("button", { name: "More navigation" });
    expect(more).toHaveAttribute("aria-haspopup", "dialog");
    fireEvent.click(more);
    expect(screen.getByRole("dialog", { name: "Navigation" })).toHaveAttribute("aria-modal", "true");
    expect(nav.pushes).toHaveLength(0);
  });
});
