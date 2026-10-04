import type { AIResearch, CompactSystemHealth, DashboardData, DetailedSystemHealth, MarketFeatures, MarketRegime, MarketSnapshot, NotificationDelivery, OperationalEvent, PipelineRun, RiskEvaluationSet, ShadowMark, ShadowPerformance, ShadowTrade, StrategyCandidateSet, SystemMetrics } from "@/types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, `Request failed (${response.status})`);
  return response.json() as Promise<T>;
}
const optional = async <T>(path: string): Promise<T | null> => { try { return await request<T>(path); } catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; } };
export const api = {
  dashboard: () => request<DashboardData>("/api/dashboard/latest"),
  snapshots: (limit = 50) => request<MarketSnapshot[]>(`/api/snapshots?limit=${limit}`),
  snapshot: (id: number) => optional<MarketSnapshot>(`/api/snapshots/${id}`),
  regimes: (limit = 50) => request<MarketRegime[]>(`/api/regime?limit=${limit}`),
  pipelines: (limit = 50) => request<PipelineRun[]>(`/api/pipeline?limit=${limit}`),
  candidatesList: (limit = 50) => request<Array<Pick<StrategyCandidateSet, "snapshot_id" | "candidate_count">>>(`/api/strategy-candidates?limit=${limit}`),
  riskList: (limit = 50) => request<RiskEvaluationSet[]>(`/api/risk?limit=${limit}`),
  features: (id: number) => optional<MarketFeatures>(`/api/features/${id}`),
  regime: (id: number) => optional<MarketRegime>(`/api/regime/${id}`),
  ai: (id: number) => optional<AIResearch>(`/api/ai-research/${id}`),
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
