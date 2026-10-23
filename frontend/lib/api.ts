import type { AIResearch, AlphaFeature, CompactSystemHealth, DashboardData, DetailedSystemHealth, MarketFeatures, MarketRegime, MarketSnapshot, NotificationDelivery, OperationalEvent, Phase15Summary, Phase15TradePage, Phase15JournalFilters, PipelineRun, RiskEvaluationSet, ShadowMark, ShadowPerformance, ShadowTrade, StrategyCandidateSet, SystemMetrics } from "@/types";
import type { CommodityDecisionPage, CommodityInstrument, CommoditySummary } from "@/types/commodity-acts";
import type { PaperKind, PaperPage, PaperSummary } from "@/types/commodity-paper";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
const COMMODITY_BASE = process.env.NEXT_PUBLIC_COMMODITY_API_BASE ?? API_BASE;
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
async function request<T>(path: string): Promise<T> {
  const base = path.startsWith("/api/commodity-acts/") ? COMMODITY_BASE : API_BASE;
  const response = await fetch(`${base}${path}`, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, `Request failed (${response.status})`);
  return response.json() as Promise<T>;
}
const optional = async <T>(path: string): Promise<T | null> => { try { return await request<T>(path); } catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; } };
export const api = {
  commodityPaperSummary: () => request<PaperSummary>("/api/commodity-acts/paper/summary"),
  commodityPaperPage: (instrument: CommodityInstrument, kind: PaperKind, offset = 0) =>
    request<PaperPage>(`/api/commodity-acts/paper/${kind}?instrument=${encodeURIComponent(instrument)}&limit=50&offset=${offset}`),
  commodityActsSummary: () => request<CommoditySummary>("/api/commodity-acts/summary"),
  commodityActsDecisions: (instrument: CommodityInstrument, offset = 0) =>
    request<CommodityDecisionPage>(`/api/commodity-acts/decisions?instrument=${encodeURIComponent(instrument)}&limit=50&offset=${offset}`),
  dashboard: () => request<DashboardData>("/api/dashboard/latest"),
  phase15Summary: () => request<Phase15Summary>("/api/phase15/summary"),
  phase15Positions: (offset = 0) => request<Phase15TradePage>(`/api/phase15/positions?limit=100&offset=${offset}`),
  phase15Journal: (filters: Phase15JournalFilters = {}) => {
    const params = new URLSearchParams();
    // The deployed API accepts ISO dates; UI labels "today"/"all" are not dates.
    const date = filters.date === "all" ? undefined : filters.date === "today" ?
      new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date()) : filters.date;
    for (const [key, value] of Object.entries({ limit: 100, ...filters, date })) {
      if (value != null && value !== "") params.set(key, String(value));
    }
    return request<Phase15TradePage>(`/api/phase15/journal?${params}`);
  },
  snapshots: (limit = 50) => request<MarketSnapshot[]>(`/api/snapshots?limit=${limit}`),
  snapshot: (id: number) => optional<MarketSnapshot>(`/api/snapshots/${id}`),
  regimes: (limit = 50) => request<MarketRegime[]>(`/api/regime?limit=${limit}`),
  pipelines: (limit = 50) => request<PipelineRun[]>(`/api/pipeline?limit=${limit}`),
  candidatesList: (limit = 50) => request<Array<Pick<StrategyCandidateSet, "snapshot_id" | "candidate_count" | "eligible" | "strategy_type" | "strategy_version" | "created_at">>>(`/api/strategy-candidates?limit=${limit}`),
  riskList: (limit = 50) => request<RiskEvaluationSet[]>(`/api/risk?limit=${limit}`),
  features: (id: number) => optional<MarketFeatures>(`/api/features/${id}`),
  regime: (id: number) => optional<MarketRegime>(`/api/regime/${id}`),
  ai: (id: number) => optional<AIResearch>(`/api/ai-research/${id}`),
  alpha: (id: number) => optional<AlphaFeature>(`/api/alpha/${id}`),
  alphaLatest: () => optional<AlphaFeature>("/api/alpha/latest"),
  alphaHistory: (date: string) => request<AlphaFeature[]>(`/api/alpha/history?date=${date}`),
  candidates: (id: number) => optional<StrategyCandidateSet>(`/api/strategy-candidates/${id}`),
  risk: (id: number) => optional<RiskEvaluationSet>(`/api/risk/${id}`),
  pipeline: (id: number) => optional<PipelineRun>(`/api/pipeline/${id}`),
  shadows: (limit = 100) => request<ShadowTrade[]>(`/api/shadow/trades?limit=${limit}`),
  shadow: (id: number) => optional<ShadowTrade>(`/api/shadow/trades/${id}`),
  marks: (id: number) => request<ShadowMark[]>(`/api/shadow/trades/${id}/marks`),
  performance: () => request<ShadowPerformance>("/api/shadow/performance"),
  health: () => request<CompactSystemHealth>("/api/health"),
  systemHealth: () => request<DetailedSystemHealth>("/api/system/health"),
  systemEvents: (limit = 100) => request<OperationalEvent[]>(`/api/system/events?limit=${limit}`),
  systemMetrics: () => request<SystemMetrics>("/api/system/metrics"),
  notifications: (limit = 20) => request<NotificationDelivery[]>(`/api/notifications?limit=${limit}`),
};
