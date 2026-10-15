import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Page from "@/app/page";
import Phase15Page from "@/app/phase15/page";
import ErrorPage from "@/app/error";
import GlobalError from "@/app/global-error";

const failures = vi.hoisted(() => ({ dashboard: false, monitor: false }));
vi.mock("@/components/dashboard", () => ({ Dashboard: () => {
  if (failures.dashboard) throw new Error("private dashboard detail");
  return <div>Legacy dashboard works</div>;
} }));
vi.mock("@/components/phase15-monitor", () => ({ Phase15Monitor: () => {
  if (failures.monitor) throw new Error("private monitor detail");
  return <div>Phase 15 monitor works</div>;
} }));

beforeEach(() => { failures.dashboard = false; failures.monitor = false; });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("frontend route isolation", () => {
  it("renders the monitor exactly once alongside the dashboard", () => {
    render(<Page/>);
    expect(screen.getAllByText("Phase 15 monitor works")).toHaveLength(1);
    expect(screen.getByText("Legacy dashboard works")).toBeInTheDocument();
  });

  it("keeps Phase 15 visible if the legacy dashboard throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    failures.dashboard = true;
    render(<Page/>);
    expect(screen.getByText("Phase 15 monitor works")).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "Research dashboard unavailable" })).toBeInTheDocument();
    expect(screen.queryByText("private dashboard detail")).not.toBeInTheDocument();
  });

  it("keeps the legacy dashboard visible if Phase 15 throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    failures.monitor = true;
    render(<Page/>);
    expect(screen.getByText("Legacy dashboard works")).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "Phase 15 monitor unavailable" })).toBeInTheDocument();
    expect(screen.queryByText("private monitor detail")).not.toBeInTheDocument();
  });

  it("serves a dedicated monitor page without the dashboard", () => {
    render(<Phase15Page/>);
    expect(screen.getByRole("heading", { name: "Phase 15 Strategy Monitor" })).toBeInTheDocument();
    expect(screen.getAllByText("Phase 15 monitor works")).toHaveLength(1);
    expect(screen.queryByText("Legacy dashboard works")).not.toBeInTheDocument();
  });

  it("keeps route errors generic and calls the reset callback", () => {
    const reset = vi.fn();
    render(<ErrorPage error={new Error("secret token")} reset={reset}/>);
    expect(screen.getByText("PAGE UNAVAILABLE")).toBeInTheDocument();
    expect(screen.queryByText("secret token")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry page" }));
    expect(reset).toHaveBeenCalledOnce();
  });

  it("keeps root errors generic, renders its own shell, and calls reset", () => {
    const reset = vi.fn();
    const html = renderToStaticMarkup(<GlobalError error={new Error("secret token")} reset={reset}/>);
    expect(html).toContain("<html");
    expect(html).toContain("TERMINAL UNAVAILABLE");
    expect(html).not.toContain("secret token");
    render(<GlobalError error={new Error("secret token")} reset={reset}/>);
    fireEvent.click(screen.getByRole("button", { name: "Retry application" }));
    expect(reset).toHaveBeenCalledOnce();
  });
});
