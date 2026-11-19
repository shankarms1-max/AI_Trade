import type { Phase15Strategy, Phase15Summary } from "@/types";
import { completeSum } from "@/lib/monitoring";

export const ACTS_VERSIONS = ["ACTS_V3", "ACTS_V2"] as const;

export function isActs(row: { strategy_id: string }): boolean {
  return ACTS_VERSIONS.some(version => version === row.strategy_id);
}

export function actsStrategies(rows: readonly Phase15Strategy[]): Phase15Strategy[] {
  return ACTS_VERSIONS.flatMap(version => {
    const row = rows.find(item => item.strategy_id === version);
    return row ? [row] : [];
  });
}

/** Isolate even cached full-research responses; never infer missing accounting. */
export function actsSummary(data: Phase15Summary | null | undefined): Phase15Summary | null {
  if (!data) return null;
  const strategies = actsStrategies(data.strategies);
  const versions = ACTS_VERSIONS.map(version => strategies.find(row => row.strategy_id === version));
  const realized = completeSum(versions.map(row => row?.realized_gross_pnl_today));
  const unrealized = completeSum(versions.map(row => row?.unrealized_gross_pnl));
  return { ...data, strategies, totals: {
    realized_gross_pnl_today: realized, unrealized_gross_pnl: unrealized,
    combined_gross_pnl: completeSum([realized, unrealized]),
    open_positions: completeSum(versions.map(row => row?.open_position_count)),
    unresolved_exposures: completeSum(versions.map(row => row?.unresolved_exposure_count)),
    expired_unpriced_count: completeSum(versions.map(row => row?.expired_unpriced_count)),
    trades_today: completeSum(versions.map(row => row?.trades_today)),
  } };
}
