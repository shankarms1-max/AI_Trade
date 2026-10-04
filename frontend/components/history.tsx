"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { dateIST, number, percent, timeIST } from "@/lib/format";
import { Badge, Empty, Panel, SectionError } from "@/components/ui";
import type { MarketRegime, MarketSnapshot, PipelineRun, RiskEvaluationSet, StrategyCandidateSet } from "@/types";

type HistoryData = {
  snapshots: MarketSnapshot[];
  regimes: MarketRegime[];
  pipelines: PipelineRun[];
  candidates: Array<Pick<StrategyCandidateSet, "snapshot_id" | "candidate_count">>;
  risks: RiskEvaluationSet[];
};

export function HistoryTable() {
  const router = useRouter();
  const [data, setData] = useState<HistoryData | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    Promise.all([api.snapshots(), api.regimes(), api.pipelines(), api.candidatesList(), api.riskList()])
      .then(([snapshots, regimes, pipelines, candidates, risks]) => setData({ snapshots, regimes, pipelines, candidates, risks }))
      .catch(() => setError(true));
  }, []);
  return <main><div className="page-head"><div><h1>Research History</h1><p>Persisted snapshot audit trail</p></div></div><Panel title="Snapshots">
    {error ? <SectionError>RESEARCH HISTORY UNAVAILABLE</SectionError> : !data ? <div className="loading">LOADING HISTORY</div> : data.snapshots.length === 0 ? <Empty>NO SNAPSHOTS</Empty> : <div className="table-wrap"><table><thead><tr><th>Date</th><th>Time</th><th>Spot</th><th>VIX</th><th>Regime</th><th>Confidence</th><th>Evidence</th><th>Candidates</th><th>Approved</th><th>Shadow action</th><th>Pipeline</th></tr></thead><tbody>
      {data.snapshots.map(snapshot => {
        const regime = data.regimes.find(item => item.snapshot_id === snapshot.id);
        const pipeline = data.pipelines.find(item => item.market_snapshot_id === snapshot.id);
        const candidates = data.candidates.find(item => item.snapshot_id === snapshot.id);
        const risk = data.risks.find(item => item.snapshot_id === snapshot.id);
        return <tr className="row-link" key={snapshot.id} onClick={() => router.push(`/history/${snapshot.id}`)}><td>{dateIST(snapshot.timestamp_ist)}</td><td>{timeIST(snapshot.timestamp_ist)}</td><td>{number(snapshot.nifty_spot)}</td><td>{number(snapshot.india_vix)}</td><td>{regime ? <Badge value={regime.regime}/> : "N/A"}</td><td>{percent(regime?.confidence)}</td><td>{regime?.evidence_quality ?? "N/A"}</td><td>{candidates?.candidate_count ?? 0}</td><td>{risk?.approved_count ?? 0}</td><td>{pipeline?.shadow_trade_id ? `ENTRY ${pipeline.shadow_trade_id}` : "NONE"}</td><td>{pipeline ? <Badge value={pipeline.status}/> : "N/A"}</td></tr>;
      })}
    </tbody></table></div>}
  </Panel></main>;
}
