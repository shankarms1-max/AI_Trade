import { cleanup, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GlobalHeader } from "@/components/global-header";
import { Navigation } from "@/components/navigation";
import { Analytics } from "@/components/pages/analytics";
import { DrawerHost } from "@/components/pages/drawers";
import { Orders } from "@/components/pages/orders";
import { Overview } from "@/components/pages/overview";
import { Positions } from "@/components/pages/positions";
import { Research } from "@/components/pages/research";
import { Risk } from "@/components/pages/risk";
import { Settings } from "@/components/pages/settings";
import { Strategies, StrategyDetail } from "@/components/pages/strategies";
import { SystemSections } from "@/components/pages/system-sections";
import { Tradebook } from "@/components/pages/tradebook";
import { fixtureRoutes, renderWithApp, resetNav, resetStore, stubApi } from "./helpers/terminal";

vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
beforeEach(() => resetStore());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

// Anything that could place, change, cancel, enable or square off an order. The terminal is strictly read-only.
const MUTATING = /\b(buy|sell|square[- ]?off|cancel|place order|submit order|execute|exit position|close position|flatten|enable|disable|activate|kill|start|stop engine|arm|deploy)\b/i;
const PAGES: Array<[string, string, string, () => ReactElement]> = [
  ["overview", "/", "", () => <Overview/>], ["positions", "/positions", "", () => <Positions/>], ["positions closed", "/positions", "tab=closed", () => <Positions/>],
  ["orders", "/orders", "", () => <Orders/>], ["tradebook", "/tradebook", "", () => <Tradebook/>], ["strategies", "/strategies", "", () => <Strategies/>],
  ["strategy overview", "/strategies/nifty-acts_v3", "", () => <StrategyDetail engineId="nifty-acts_v3"/>],
  ["strategy config", "/strategies/mcx-crudeoilm", "view=configuration", () => <StrategyDetail engineId="mcx-crudeoilm"/>],
  ["analytics", "/analytics", "", () => <Analytics/>], ["risk", "/risk", "", () => <Risk/>], ["research", "/research", "", () => <Research/>],
  ["settings", "/settings", "", () => <Settings/>], ["system", "/system", "", () => <SystemSections/>],
  ["header + nav", "/", "", () => <><Navigation/><GlobalHeader/></>],
  ["position drawer", "/positions", "position=nifty-acts_v3%3AT4-00000000000000000000", () => <DrawerHost/>],
  ["order drawer", "/orders", "order=nifty-acts_v3%3AT1-00000000000000000000%3AEXIT%3ASHORT", () => <DrawerHost/>],
];

describe("read-only terminal", () => {
  it.each(PAGES)("%s: only GET requests and no order/enable/square-off control", async (_name, path, search, make) => {
    resetNav(path, search);
    const { methods, calls } = stubApi(fixtureRoutes());
    renderWithApp(make());
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    await new Promise(r => setTimeout(r, 60));
    expect(new Set(methods)).toEqual(new Set(["GET"]));
    const controls = [...screen.queryAllByRole("button"), ...screen.queryAllByRole("link"), ...screen.queryAllByRole("tab"), ...screen.queryAllByRole("switch"), ...screen.queryAllByRole("checkbox")];
    for (const c of controls) {
      const name = c.getAttribute("aria-label") ?? c.textContent ?? "";
      expect(name, `control "${name}"`).not.toMatch(MUTATING);
    }
    expect(document.querySelector("form")).toBeNull();
    expect(document.querySelector("input[type=submit], button[type=submit]")).toBeNull();
  });
});
