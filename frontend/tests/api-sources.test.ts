import {afterEach,expect,it,vi} from "vitest";
afterEach(()=>{vi.unstubAllGlobals();vi.unstubAllEnvs();vi.resetModules();});
it("retains relative NIFTY URLs and an independent Commodity read-only source",async()=>{
 vi.stubEnv('NEXT_PUBLIC_API_BASE_URL','');vi.stubEnv('NEXT_PUBLIC_COMMODITY_API_BASE','https://commodity.example');vi.resetModules();
 const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({})});vi.stubGlobal('fetch',fetch);
 const{api}=await import('@/lib/api');await api.phase15Summary();await api.commodityPaperSummary();
 expect(fetch.mock.calls[0][0]).toBe('/api/phase15/summary');expect(fetch.mock.calls[1][0]).toBe('https://commodity.example/api/commodity-acts/paper/summary');
 for(const call of fetch.mock.calls)expect(call[1]).not.toHaveProperty('method');
});
