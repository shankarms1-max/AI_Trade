"use client";
import { Fragment, useCallback, useState } from "react";
import { api } from "@/lib/api";
import { integer, money, number, timeIST } from "@/lib/format";
import { usePolling } from "@/hooks/use-polling";
import { Badge, Empty, Metric, SectionError } from "@/components/ui";
import type { Phase15Strategy, Phase15Trade } from "@/types";
import { OrbRange } from "@/components/phase15-orb-evidence";
import { Icon } from "@/components/icons";
import { observationState } from "@/lib/monitoring";

const DISPLAY_NAMES: Record<string, string> = {
  ACTS_V2: "ACTS V2", ORB_15: "ORB 15", ORB_30: "ORB 30", ORB_60: "ORB 60",
  EMA_PULLBACK: "EMA Pullback", SUPERTREND_RSI_VOLUME: "Supertrend + RSI + Volume",
};

export function blockerLabel(value: string | null | undefined): string {
  if (!value) return "N/A";
  if (value === "ORB_REENTRY_DISABLED_OR_SAME_BREAKOUT") return "Re-entry disabled / same breakout";
  const words = value.replaceAll("_", " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function amount(value: number | null | undefined) {
  return <span className={value == null ? "muted" : value < 0 ? "negative" : value > 0 ? "positive" : ""}>{money(value)}</span>;
}

function holding(seconds: number | null) {
  if (seconds == null) return "N/A";
  return seconds < 60 ? `${integer(seconds)}s` : `${integer(Math.floor(seconds / 60))}m ${integer(Math.floor(seconds % 60))}s`;
}

function TradeDetails({ strategy }: { strategy: Phase15Strategy }) {
  return <div className="phase15-details">
    <div className="phase15-rejections">Entry rejections today: {integer(strategy.entry_rejections_today)}
      {strategy.latest_rejection_reason && <> · Latest: {blockerLabel(strategy.latest_rejection_reason)}</>}
    </div>
    {strategy.today_trades.length ? <div className="table-wrap"><table aria-label={`${strategy.display_name} today trades`}>
      <thead><tr><th>Time</th><th>Direction</th><th>Spread</th><th>Entry Credit</th><th>Exit / Current Debit</th><th>Gross P&amp;L / MTM</th><th>MFE</th><th>MAE</th><th>Holding</th><th>Entry Reason</th><th>Exit Policy</th><th>Exit Reason</th><th>State</th></tr></thead>
      <tbody>{strategy.today_trades.map((trade: Phase15Trade) => <tr key={trade.trade_id}>
        <td>{timeIST(trade.entry_timestamp ?? trade.decision_timestamp)}</td><td>{trade.direction ?? "N/A"}</td>
        <td>{number(trade.short_strike, 0)} / {number(trade.hedge_strike, 0)}</td>
        <td>{number(trade.entry_credit)}</td><td>{number(trade.state === "CLOSED" ? trade.exit_debit : trade.current_debit)}</td>
        <td>{amount(trade.state === "CLOSED" ? trade.realized_gross_pnl : trade.current_unrealized_gross_pnl)}</td>
        <td>{number(trade.mfe)}</td><td>{number(trade.mae)}</td><td>{holding(trade.holding_seconds)}</td>
        <td>{blockerLabel(trade.entry_reason)}{trade.strategy_features && <OrbRange title="Frozen entry ORB range" evidence={trade.strategy_features} frozen/>}</td><td>{trade.exit_policy_version ?? "UNAVAILABLE"}</td>
        <td title={trade.exit_reason ?? undefined}>{trade.terminal_status ?? blockerLabel(trade.exit_reason)}</td>
        <td><Badge value={trade.state}/></td>
      </tr>)}</tbody>
    </table></div> : <Empty>NO ENTERED TRADES TODAY</Empty>}
  </div>;
}

export function Phase15Monitor({ workspace = false }: { workspace?: boolean } = {}) {
  const loader = useCallback(() => api.phase15Summary(), []);
  const { data, error, loading } = usePolling(loader, 20000);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [selected, setSelected] = useState("ALL");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("source");
  const visible = (data?.strategies ?? []).filter(row => (selected === "ALL" || row.strategy_id === selected) &&
    `${row.display_name} ${row.latest_status ?? ""} ${row.latest_blocker ?? ""}`.toLowerCase().includes(query.toLowerCase()));
  if (sort === "name") visible.sort((a,b) => a.display_name.localeCompare(b.display_name));
  if (sort === "gross") visible.sort((a,b) => a.realized_gross_pnl_today == null ? b.realized_gross_pnl_today == null ? 0 : 1 : b.realized_gross_pnl_today == null ? -1 : b.realized_gross_pnl_today-a.realized_gross_pnl_today);
  return <section className="phase15-monitor" aria-label="Phase 15 paper strategy monitor">
    <div className="phase15-head"><div><span>PHASE 15 · PAPER STRATEGY MONITOR</span>
      <strong>Six strategy paper monitor</strong>
      <small>Net transaction-cost accounting is not configured.</small></div>
      <div className="phase15-badges">{error || (!loading && !data) ? <Badge value="UNAVAILABLE"/> : data ? <><Badge value="PAPER"/><Badge value="GROSS_ONLY"/></> : null}</div>
    </div>
    {error || (!loading && !data) ? <SectionError>PHASE 15 MONITOR UNAVAILABLE</SectionError> : !data ?
      <Empty>LOADING PHASE 15 MONITOR</Empty> : <>
      <div className="phase15-metrics">
        <Metric label="Realized Gross Today" value={amount(data.totals.realized_gross_pnl_today)}/>
        <Metric label="Live Unrealized Gross" value={amount(data.totals.unrealized_gross_pnl)}/>
        <Metric label="Combined Gross" value={amount(data.totals.combined_gross_pnl)}/>
        <Metric label="Open Positions" value={integer(data.totals.open_positions)}/>
        <Metric label="Unresolved Exposures" value={integer(data.totals.unresolved_exposures)}/>
        <Metric label="Expired Unpriced" value={integer(data.totals.expired_unpriced_count)}/>
        <Metric label="Trades Today" value={integer(data.totals.trades_today)}/>
      </div>
      <div className="phase15-asof">As of {timeIST(data.as_of)} · Paper marks and realized gross are shown separately.</div>
      <section aria-label="ORB Range"><h3>ORB Range</h3><div className="phase15-orb-grid">{data.strategies.filter(s=>s.strategy_id.startsWith("ORB_")).map(s=><OrbRange key={s.strategy_id} title={s.display_name} evidence={s.orb_range} observedAt={s.latest_timestamp}/>)}</div></section>
      <div className="strategies-toolbar">
        {workspace && <div className="segmented-control" aria-label="Strategy selection"><button aria-pressed={selected === "ALL"} onClick={() => setSelected("ALL")}>All strategies</button>{data.strategies.map(row => <button key={row.strategy_id} aria-pressed={selected === row.strategy_id} onClick={() => setSelected(row.strategy_id)}>{DISPLAY_NAMES[row.strategy_id] ?? row.display_name}</button>)}</div>}
        <label className="search-field"><Icon name="search" size={15}/><input aria-label="Filter strategies" placeholder="Search signals or blockers…" value={query} onChange={e => setQuery(e.target.value)}/></label>
        <select aria-label="Sort strategies" value={sort} onChange={e => setSort(e.target.value)}><option value="source">Source order</option><option value="name">Strategy name</option><option value="gross">Realized gross ↓</option></select>
      </div>
      {workspace && <div className="strategy-card-grid">{visible.map(strategy => <article className="strategy-tile" id={`strategy-${strategy.strategy_id}`} key={strategy.strategy_id}>
        <header><h3>{DISPLAY_NAMES[strategy.strategy_id] ?? strategy.display_name}</h3><Badge value={strategy.latest_status ?? "UNAVAILABLE"}/></header>
        <div className="strategy-values"><span>Realized gross<strong>{amount(strategy.realized_gross_pnl_today)}</strong></span><span>Open positions<strong>{integer(strategy.open_position_count)}</strong></span></div>
        <p className="signal-line">{blockerLabel(strategy.latest_blocker)}</p><p><Badge value={observationState(strategy.latest_timestamp)}/> · {timeIST(strategy.latest_timestamp)}</p>
        <button className="button-secondary" style={{ marginTop: 12 }} aria-label={`Inspect ${strategy.display_name}`} onClick={() => setExpanded(expanded === strategy.strategy_id ? null : strategy.strategy_id)}>Trade evidence<Icon name="arrow" size={13}/></button>
      </article>)}</div>}
      <div className="table-wrap"><table aria-label="Phase 15 strategies"><thead><tr>
        <th>Strategy</th><th>Trades Today</th><th>W / L</th><th>Rejected</th><th>Open</th><th>Unresolved</th><th>Expired Unpriced</th>
        <th>Realized Gross</th><th>Live MTM</th><th>Latest Status</th><th>Blocker</th>
      </tr></thead><tbody>{visible.map(strategy => <Fragment key={strategy.strategy_id}>
        <tr>
          <td><button className="phase15-expand" aria-expanded={expanded === strategy.strategy_id}
            aria-label={`${expanded === strategy.strategy_id ? "Collapse" : "Expand"} ${DISPLAY_NAMES[strategy.strategy_id] ?? strategy.display_name}`}
            onClick={() => setExpanded(expanded === strategy.strategy_id ? null : strategy.strategy_id)}>
            <span aria-hidden="true">{expanded === strategy.strategy_id ? "▾" : "▸"}</span>
            {DISPLAY_NAMES[strategy.strategy_id] ?? strategy.display_name}</button></td>
          <td>{integer(strategy.trades_today)}</td>
          <td>{integer(strategy.wins_today)} / {integer(strategy.losses_today)}</td>
          <td>{integer(strategy.entry_rejections_today)}</td><td>{integer(strategy.open_position_count)}</td>
          <td>{integer(strategy.unresolved_exposure_count)}</td><td>{integer(strategy.expired_unpriced_count)}</td>
          <td>{amount(strategy.realized_gross_pnl_today)}</td><td>{amount(strategy.unrealized_gross_pnl)}</td>
          <td>{strategy.latest_status ? <Badge value={strategy.latest_status}/> : "N/A"}</td>
          <td title={strategy.latest_blocker ?? undefined}>{blockerLabel(strategy.latest_blocker)}</td>
        </tr>
        {expanded === strategy.strategy_id && <tr>
          <td colSpan={11}><TradeDetails strategy={strategy}/></td></tr>}
      </Fragment>)}</tbody></table></div>
    </>}
  </section>;
}
