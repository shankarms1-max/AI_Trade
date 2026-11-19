import { cleanup,render,screen,within } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import { TradingWorkspace } from "@/components/trading-workspace";
import { api } from "@/lib/api";
import { timeIST } from "@/lib/format";
import fixtures from "./fixtures/monitoring.json";
import type { Phase15Summary } from "@/types";
vi.mock("@/lib/api",()=>({api:{phase15Summary:vi.fn(),health:vi.fn(),phase15Positions:vi.fn(),dashboard:vi.fn()}}));
const data=fixtures.fixtures;
beforeEach(()=>{
 const summary=structuredClone(data['/api/phase15/summary']) as Phase15Summary;
 summary.strategies.push({...structuredClone(summary.strategies[0]),strategy_id:'ACTS_V3',display_name:'ACTS V3',
   realized_gross_pnl_today:0,unrealized_gross_pnl:0,open_position_count:0,trades_today:0,unresolved_exposure_count:0,
   expired_unpriced_count:0,latest_timestamp:null,latest_status:null,today_trades:[]});
 vi.mocked(api.phase15Summary).mockResolvedValue(summary as never);
 vi.mocked(api.health).mockResolvedValue(data['/api/health'] as never);
 vi.mocked(api.phase15Positions).mockResolvedValue(data['/api/phase15/positions'] as never);
 vi.mocked(api.dashboard).mockResolvedValue(data['/api/dashboard/latest'] as never);
});
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.clearAllMocks();});
it("puts ACTS accounting and exposure before ACTS-only evidence",async()=>{
 render(<TradingWorkspace/>);const position=await screen.findByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'});
 const strategies=screen.getByRole('region',{name:'ACTS paper strategy monitor'});
 expect(position.compareDocumentPosition(strategies)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
 expect(screen.getByRole('heading',{name:'ACTS Paper Trading'})).toBeInTheDocument();
 const pulse=screen.getByRole('navigation',{name:'ACTS version state summary'});
 expect(within(pulse).getAllByRole('link')).toHaveLength(2);
 const gross=screen.getByRole('region',{name:"Today's ACTS gross summary"});
 expect(within(gross).getByText('Gross P&L').parentElement).toHaveTextContent('−₹187.50');
 expect(within(gross).queryByText('₹3,900.00')).not.toBeInTheDocument();
 expect(screen.queryByRole('alert')).not.toBeInTheDocument();
 expect(screen.queryByText(/ORB 15|ORB 30|ORB 60|EMA Pullback|Supertrend/)).not.toBeInTheDocument();
});
it("retains independent positions when summary fails",async()=>{
 vi.mocked(api.phase15Summary).mockRejectedValue(Error('private error'));render(<TradingWorkspace/>);
 expect(await screen.findByText(/ACTS SUMMARY UNAVAILABLE/)).toBeInTheDocument();
 expect(await screen.findByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'})).toBeInTheDocument();
 expect(screen.queryByText('private error')).not.toBeInTheDocument();
});
it("uses dashboard futures without spot, premiums or retired ORB levels",async()=>{
 const dashboard=structuredClone(data['/api/dashboard/latest']);dashboard.snapshot.nifty_future=27123.45;
 vi.mocked(api.dashboard).mockResolvedValue(dashboard as never);render(<TradingWorkspace/>);
 const context=screen.getByLabelText('Market and system status');await within(context).findByText('27,123.45');
 expect(context).not.toHaveTextContent('25,701.80');expect(context).not.toHaveTextContent('100.20');
 expect(api.dashboard).toHaveBeenCalledOnce();
});
it("leaves futures N/A if the dashboard fails, without an ORB substitute",async()=>{
 vi.mocked(api.dashboard).mockRejectedValue(Error('private snapshot error'));render(<TradingWorkspace/>);
 await screen.findByRole('article',{name:'ACTS V2 position UI-FIXTURE-ACTS-OPEN'});
 const context=screen.getByLabelText('Market and system status');expect(context).toHaveTextContent('NIFTY FUT N/A');
 expect(context).not.toHaveTextContent('25,701.80');
});
it("prefers V3 observations and orders the pulse V3 then V2",async()=>{
 const summary=structuredClone(data['/api/phase15/summary']) as Phase15Summary;const v2=summary.strategies[0];
 const at='2026-11-19T09:58:00+05:30';v2.latest_timestamp='2026-11-19T09:59:00+05:30';
 summary.strategies.push({...structuredClone(v2),strategy_id:'ACTS_V3',display_name:'ACTS V3',latest_timestamp:at});
 vi.mocked(api.phase15Summary).mockResolvedValue(summary as never);render(<TradingWorkspace/>);
 await screen.findByRole('button',{name:'Expand ACTS V3'});
 expect(within(screen.getByLabelText('Market and system status')).getByText(`Observed ${timeIST(at)}`)).toBeInTheDocument();
 const pulse=screen.getByRole('navigation',{name:'ACTS version state summary'});
 expect(within(pulse).getAllByRole('link').map(link=>link.textContent)).toEqual([
  expect.stringContaining('ACTS V3'),expect.stringContaining('ACTS V2')]);
});
