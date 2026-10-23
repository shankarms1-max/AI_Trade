import { cleanup,render,screen,within } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import { TradingWorkspace } from "@/components/trading-workspace";
import { api } from "@/lib/api";
import fixtures from "./fixtures/monitoring.json";
vi.mock("@/lib/api",()=>({api:{phase15Summary:vi.fn(),health:vi.fn(),phase15Positions:vi.fn()}}));
const data=fixtures.fixtures;
beforeEach(()=>{vi.mocked(api.phase15Summary).mockResolvedValue(structuredClone(data['/api/phase15/summary']) as never);vi.mocked(api.health).mockResolvedValue(data['/api/health'] as never);vi.mocked(api.phase15Positions).mockResolvedValue(data['/api/phase15/positions'] as never);});
afterEach(()=>{cleanup();vi.clearAllMocks();});
it("puts gross accounting and active exposure before detailed strategy evidence",async()=>{
 render(<TradingWorkspace/>);const position=await screen.findByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'});
 const strategies=screen.getByRole('region',{name:'Phase 15 paper strategy monitor'});
 expect(position.compareDocumentPosition(strategies)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
 expect(screen.getByRole('navigation',{name:'Six strategy state summary'}).children).toHaveLength(6);
 const gross=screen.getByRole('region',{name:"Today's NIFTY gross summary"});expect(within(gross).getByText('Gross P&L').parentElement).toHaveTextContent('N/A');expect(within(gross).getByText('₹3,900.00')).toBeInTheDocument();
 expect(screen.getByRole('alert')).toHaveTextContent('1 unresolved exposure');expect(screen.getByText('−₹520.00')).toBeInTheDocument();
});
it("retains independent position evidence when summary is unavailable",async()=>{
 vi.mocked(api.phase15Summary).mockRejectedValue(Error('private error'));render(<TradingWorkspace/>);
 expect(await screen.findByText(/NIFTY SUMMARY UNAVAILABLE/)).toBeInTheDocument();expect(await screen.findByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'})).toBeInTheDocument();expect(screen.queryByText('private error')).not.toBeInTheDocument();
});
it("uses the exact backend future price, not option premium or spot",async()=>{
 render(<TradingWorkspace/>);const context=screen.getByLabelText('Market and system status');await screen.findByText('25,701.80',{selector:'.market-context strong'});expect(context).toHaveTextContent('25,701.80');expect(context).not.toHaveTextContent('100.20');
});
