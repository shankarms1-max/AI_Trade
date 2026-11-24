import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { CommodityLiveOverview } from "@/components/commodity-live-overview";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { commodityActsSummary: vi.fn(), commodityPaperSummary: vi.fn() } }));
vi.mock("@/components/mcx-futures", () => ({ McxFuturesOverview: () => <div>ACTS V3 MCX Futures — PAPER</div> }));
vi.mock("@/components/commodity-paper", () => ({ CommodityPaper: () => <div>Legacy paper panels</div> }));
vi.mock("@/components/commodity-acts", () => ({ CommodityOverview: () => <div>Legacy research panels</div>, CommodityJournal: () => <div>Legacy journal</div> }));
beforeEach(() => {
  vi.mocked(api.commodityActsSummary).mockResolvedValue({ instruments: [{ observation_count: 0 }, { observation_count: 0 }] } as never);
  vi.mocked(api.commodityPaperSummary).mockResolvedValue({ instruments: [{ open_positions: 0, unresolved_exposure: 0, closed_trades: 3 }] } as never);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("keeps the unused options engine collapsed with one historical count", async () => {
  render(<CommodityLiveOverview/>);
  expect(screen.getByRole("heading", { name: "ACTS V3 MCX Futures — PAPER" })).toBeInTheDocument();
  expect(await screen.findByText("No worker deployed · no observations · historical records: 3")).toBeInTheDocument();
  expect(screen.queryByText("Legacy paper panels")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Commodity ACTS Options (legacy) — NOT RUNNING"));
  expect(await screen.findByText("Legacy paper panels")).toBeInTheDocument();
});
it("shows legacy evidence when an open option position exists", async () => {
  vi.mocked(api.commodityPaperSummary).mockResolvedValue({ instruments: [{ open_positions: 1, unresolved_exposure: 0, closed_trades: 0 }] } as never);
  render(<CommodityLiveOverview/>);
  await waitFor(() => expect(screen.getByText("Legacy paper panels")).toBeInTheDocument());
});
