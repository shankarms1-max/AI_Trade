import { Badge } from "@/components/ui";
import { money, number, percent, pretty } from "@/lib/format";
import type { StrategyCandidate } from "@/types";

export function CandidateTable({ candidates }: { candidates: StrategyCandidate[] }) {
  const economics = candidates.some(candidate => candidate.strategy_logic_version === "phase14_2_v1");
  return <div className="table-wrap"><table>
    <thead><tr><th>Rank</th><th>Strategy / Family</th><th>Spread Width</th><th>Net credit</th>
      {economics && <><th>Survival / Carry</th><th>Expected Move Distance</th><th>DTE</th><th>Max Loss / lot</th><th>Theta/Gamma Balance</th></>}
      <th>Pricing</th><th>Score</th></tr></thead>
    <tbody>{candidates.map((item, index) => <tr key={item.candidate_id}>
      <td>#{index + 1}</td><td><details><summary>{pretty(item.strategy_type)}
        {item.strategy_family && <div className="metric-detail">{pretty(item.strategy_family)} · {pretty(item.directional_strength ?? "N/A")}</div>}
      </summary><dl className="kv-grid">
        <div className="kv"><dt>Short / Long</dt><dd>{number(item.short_leg.strike, 0)} / {number(item.long_leg.strike, 0)} {item.short_leg.option_type}</dd></div>
        <div className="kv"><dt>Credit / width</dt><dd>{percent(item.credit_to_width_ratio * 100)}</dd></div>
        <div className="kv"><dt>Structure reference</dt><dd>{number(item.support_or_resistance_reference, 0)}</dd></div>
        {item.strategy_logic_version && <>
          <div className="kv"><dt>Bias / Directional Strength</dt><dd>{pretty(item.market_bias ?? "N/A")} / {pretty(item.directional_strength ?? "N/A")}</dd></div>
          <div className="kv"><dt>Expected move proxy</dt><dd>{number(item.expected_move_points)} pts · {item.expected_move_source}</dd></div>
          <div className="kv"><dt>Buffer / beyond structure</dt><dd>{number(item.required_short_strike_buffer)} / {number(item.distance_beyond_structure_points)} pts</dd></div>
          <div className="kv"><dt>Cost estimate / after cost</dt><dd>{number(item.estimated_cost)} / {number(item.net_credit_after_cost)} pts {item.cost_estimate_complete ? "" : "(incomplete costs)"}</dd></div>
          <div className="kv"><dt>DTE bucket / Gamma risk</dt><dd>{pretty(item.dte_bucket ?? "N/A")} / {pretty(item.gamma_risk_state ?? "N/A")}</dd></div>
          <div className="kv"><dt>Logic version</dt><dd>{item.strategy_logic_version}</dd></div>
        </>}
      </dl>{item.strategy_logic_version && <details><summary>Score components and penalties</summary><pre>{JSON.stringify({ survival: item.survival_components, carry: item.carry_components, ranking: item.ranking_components, penalties: item.ranking_penalties }, null, 2)}</pre></details>}
      {item.warnings.length > 0 && <p className="warning">{item.warnings.map(pretty).join(" · ")}</p>}
      </details></td><td>{number(item.spread_width, 0)}</td><td>{number(item.net_credit)}</td>
      {economics && <><td>{number(item.survival_score, 0)} / {number(item.carry_score, 0)}</td>
        <td>{item.distance_in_expected_move_units == null ? "N/A" : `${number(item.distance_in_expected_move_units)}×`}</td>
        <td>{item.days_to_expiry ?? "N/A"}</td><td>{money(item.max_loss_per_lot)}</td><td>{pretty(item.theta_gamma_balance_state ?? "N/A")}</td></>}
      <td><Badge value={item.pricing_basis}/></td><td>{number(item.selection_score, 0)}</td>
    </tr>)}</tbody>
  </table>{economics && <p className="metric-detail">Survival and carry are research scores. Expected move and theta/gamma context are proxies.</p>}</div>;
}
