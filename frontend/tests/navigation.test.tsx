import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { activeHref, legacy, Navigation, primary } from "@/components/navigation";
import nextConfig from "@/next.config";

const location = vi.hoisted(() => ({ pathname: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => location.pathname }));
afterEach(cleanup);

// The shell was intentionally replaced by the terminal shell: ten primary destinations in a fixed order, the technical and
// legacy pages grouped (collapsed) under "More / Legacy". Every legacy route is still reachable.
describe("navigation", () => {
  it("lists the ten primary destinations in product order", () => {
    location.pathname = "/";
    render(<Navigation/>);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).getAllByRole("link").map(a => a.textContent)).toEqual(["Overview", "Positions", "Orders", "Tradebook", "Strategies", "Analytics", "Risk", "Research", "System Health", "Settings"]);
    expect(primary.map(([href]) => href)).toEqual(["/", "/positions", "/orders", "/tradebook", "/strategies", "/analytics", "/risk", "/research", "/system", "/settings"]);
    expect(screen.getByText(/No broker execution controls/)).toBeInTheDocument();
  });

  it("keeps Engines, MCX, AST, Commodity (legacy), NIFTY monitor, Phase 15, research, history and shadow under More / Legacy", () => {
    location.pathname = "/";
    render(<Navigation/>);
    const more = screen.getByText("More / Legacy").closest("details")!;
    expect(more).not.toHaveAttribute("open");
    const nav = within(more).getByRole("navigation", { name: "Legacy" });
    const hrefs = Object.fromEntries(within(nav).getAllByRole("link").map(a => [a.textContent, a.getAttribute("href")]));
    expect(hrefs).toEqual({
      Engines: "/engines", "MCX Futures": "/mcx-futures", "AST Futures": "/ast-futures", "Commodity (legacy)": "/commodity-acts",
      "NIFTY ACTS monitor": "/nifty", "Phase 15 monitor": "/phase15", "Market research": "/nifty/research",
      "Research history": "/history", "Legacy shadow portfolio": "/shadow",
    });
    expect(legacy).toHaveLength(9);
  });

  it("opens the legacy group and marks the section active for a legacy deep link: /mcx-futures/NATGASMINI", () => {
    location.pathname = "/mcx-futures/NATGASMINI";
    render(<Navigation/>);
    const link = within(screen.getByRole("navigation", { name: "Legacy" })).getByRole("link", { name: "MCX Futures" });
    expect(link).toHaveClass("is-active");
    expect(screen.getByText("More / Legacy").closest("details")).toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "More navigation" })).toHaveClass("is-active");
    expect(screen.getByText("MCX Futures", { selector: "strong" })).toBeInTheDocument();
  });

  it("resolves exact matches before section prefixes and keeps detail routes inside their section", () => {
    expect(activeHref("/engines")).toBe("/engines");
    expect(activeHref("/mcx-futures/CRUDEOILM")).toBe("/mcx-futures");
    expect(activeHref("/commodity-acts/CRUDEOILM")).toBe("/commodity-acts");
    expect(activeHref("/nifty/research")).toBe("/nifty/research");
    expect(activeHref("/nifty")).toBe("/nifty");
    expect(activeHref("/history/12")).toBe("/history");
    expect(activeHref("/strategies/nifty-acts_v3")).toBe("/strategies");
    expect(activeHref("/research")).toBe("/research");
    expect(activeHref("/unknown")).toBeNull();
    expect(activeHref("/mcx-futuresX")).toBeNull();
  });

  it("opens the mobile drawer with every destination and closes it with Escape", () => {
    location.pathname = "/";
    render(<Navigation/>);
    fireEvent.click(screen.getByRole("button", { name: "More navigation" }));
    const dialog = screen.getByRole("dialog", { name: "Navigation" });
    expect(within(dialog).getAllByRole("link")).toHaveLength(primary.length + legacy.length);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Navigation" })).toBeNull();
  });

  it("redirects the superseded /trades page to /tradebook (Next passes the query string through)", async () => {
    expect(await nextConfig.redirects!()).toEqual([{ source: "/trades", destination: "/tradebook", permanent: false }]);
  });
});
