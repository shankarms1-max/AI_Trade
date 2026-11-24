import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { activeHref, Navigation } from "@/components/navigation";

const location = vi.hoisted(() => ({ pathname: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => location.pathname }));
vi.mock("@/lib/api", () => ({ api: { health: vi.fn().mockResolvedValue({ status: "HEALTHY" }) } }));
afterEach(cleanup);

describe("navigation", () => {
  it("lists Engines and MCX Futures destinations alongside AST Futures", () => {
    location.pathname = "/";
    render(<Navigation/>);
    const sidebar = screen.getByRole("navigation", { name: "Primary" });
    expect(within(sidebar).getByRole("link", { name: "Engines" })).toHaveAttribute("href", "/engines");
    expect(within(sidebar).getByRole("link", { name: "MCX Futures" })).toHaveAttribute("href", "/mcx-futures");
    expect(within(sidebar).getByRole("link", { name: "AST Futures" })).toHaveAttribute("href", "/ast-futures");
    expect(screen.getByText(/No broker execution controls/)).toBeInTheDocument();
  });

  it("keeps a deep link's section active: /mcx-futures/NATGASMINI", () => {
    location.pathname = "/mcx-futures/NATGASMINI";
    render(<Navigation/>);
    const link = within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "MCX Futures" });
    expect(link).toHaveClass("is-active");
    expect(link).toHaveAttribute("aria-current", "location");
    expect(screen.getByText("MCX Futures", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More navigation" })).toHaveClass("is-active");
  });

  it("resolves exact matches before section prefixes", () => {
    expect(activeHref("/engines")).toBe("/engines");
    expect(activeHref("/mcx-futures/CRUDEOILM")).toBe("/mcx-futures");
    expect(activeHref("/commodity-acts/CRUDEOILM")).toBe("/commodity-acts/CRUDEOILM");
    expect(activeHref("/nifty/research")).toBe("/nifty/research");
    expect(activeHref("/history/12")).toBe("/history");
    expect(activeHref("/unknown")).toBeNull();
    expect(activeHref("/mcx-futuresX")).toBeNull();
  });
});
