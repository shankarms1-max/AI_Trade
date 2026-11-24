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
    expect(link).not.toHaveClass("is-active");
    expect(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("link", { name: "Natural Gas Mini" })).toHaveClass("is-active");
    expect(screen.getByText("Natural Gas Mini", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More navigation" })).toHaveClass("is-active");
  });

  it("resolves exact matches before section prefixes", () => {
    expect(activeHref("/engines")).toBe("/engines");
    expect(activeHref("/mcx-futures/CRUDEOILM")).toBe("/mcx-futures/CRUDEOILM");
    expect(activeHref("/commodity-acts/CRUDEOILM")).toBe("/commodity-acts");
    expect(activeHref("/nifty/research")).toBe("/nifty/research");
    expect(activeHref("/history/12")).toBe("/history");
    expect(activeHref("/unknown")).toBeNull();
    expect(activeHref("/mcx-futuresX")).toBeNull();
  });
});

it("routes commodity instruments to the running futures pages", () => {
  location.pathname = "/";
  render(<Navigation/>);
  const nav = screen.getByRole("navigation", { name: "Primary" });
  expect(within(nav).getByRole("link", { name: "Commodity (MCX)" })).toHaveAttribute("href", "/commodity-acts");
  expect(within(nav).getByRole("link", { name: "Crude Oil Mini" })).toHaveAttribute("href", "/mcx-futures/CRUDEOILM");
  expect(within(nav).getByRole("link", { name: "Natural Gas Mini" })).toHaveAttribute("href", "/mcx-futures/NATGASMINI");
});
