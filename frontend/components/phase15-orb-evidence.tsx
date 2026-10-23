import { money, number, timeIST } from "@/lib/format";
import type { Phase15OrbRange, Phase15PaperTrade } from "@/types";

const clock = (value: string | null | undefined) => value ? new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
}).format(new Date(value)) : "N/A";
const signed = (value: number) => `${value >= 0 ? "+" : "−"}${number(Math.abs(value))}`;

export function OrbRange({ title, evidence, frozen = false, observedAt }: {
  title: string; evidence?: Phase15OrbRange | null; frozen?: boolean; observedAt?: string | null;
}) {
  const price = evidence?.current_future_price;
  const high = evidence?.opening_high, low = evidence?.opening_low;
  const location = evidence?.complete === false ? "RANGE FORMING" : evidence?.complete !== true ||
    price == null || high == null || low == null ? "N/A" :
    price > high ? "ABOVE HIGH" : price < low ? "BELOW LOW" : "INSIDE RANGE";
  const distance = location === "ABOVE HIGH" ? price! - high! : location === "BELOW LOW" ? price! - low! : null;
  const breakout = evidence?.status === "WATCH" ? "BREAKOUT WATCH" :
    evidence?.decision_reason === "CONFIRMED_OPENING_RANGE_BREAKOUT" ? "BREAKOUT CONFIRMED" : null;
  return <article className="phase15-orb-card" aria-label={title}>
    <header><h3>{title}</h3><span>{clock(evidence?.range_start)}–{clock(evidence?.range_end)} IST</span></header>
    <small>NIFTY FUTURES · {frozen ? "Frozen trade decision range" : "Latest stored decision range"}</small>
    <dl>
      <div className="phase15-orb-boundary"><dt>High</dt><dd>{number(high)}</dd></div>
      {!frozen && <div><dt>Current future</dt><dd>{number(price)} {distance != null &&
        <span className={distance > 0 ? "positive" : "negative"}>{distance > 0 ? "↑" : "↓"} {signed(distance)} pts</span>}</dd></div>}
      <div className="phase15-orb-boundary"><dt>Low</dt><dd>{number(low)}</dd></div>
      {!frozen && <><div><dt>Status</dt><dd>{location}</dd></div>
        {breakout && <div><dt>Breakout</dt><dd>{breakout}</dd></div>}
        <div><dt>Direction</dt><dd>{evidence?.breakout_direction ?? "N/A"}</dd></div>
        <div><dt>Distance (bps)</dt><dd>{number(evidence?.breakout_distance_bps)}</dd></div>
      </>}
      <div><dt>Confirmation</dt><dd>{evidence?.confirmation_seconds == null ? "N/A" : `${number(evidence.confirmation_seconds, 0)} sec`}</dd></div>
      <div><dt>Breakout at</dt><dd>{timeIST(evidence?.breakout_timestamp)}</dd></div>
    </dl>
    {!frozen && <><p className="muted">Reason / blocker: {evidence?.blocker ?? evidence?.reason ?? evidence?.decision_reason ?? "N/A"}</p>
      <small>Decision observation: {timeIST(observedAt)}</small></>}
  </article>;
}

export function OptionExecution({ trade }: { trade: Phase15PaperTrade }) {
  const shortEntry = trade.entry_quotes.short, hedgeEntry = trade.entry_quotes.long;
  const shortAsk = trade.mark_available ? trade.latest_mark_quotes.short?.ask : null;
  const hedgeBid = trade.mark_available ? trade.latest_mark_quotes.long?.bid : null;
  const change = shortAsk != null && shortEntry?.fill_price != null ? shortAsk - shortEntry.fill_price : null;
  return <section className="phase15-option-execution" aria-label="Option execution">
    <h4>Option execution · exact contracts</h4>
    <div className="table-wrap"><table aria-label="Option leg execution">
      <thead><tr><th>Leg</th><th>Entry fill</th><th>Current exact quote</th><th>Change</th></tr></thead>
      <tbody><tr><th>Short {trade.short_option_type ?? shortEntry?.option_type ?? "N/A"} {number(trade.short_strike, 0)}</th>
        <td>{money(shortEntry?.fill_price)}</td><td>ASK {money(shortAsk)}</td>
        <td>{change == null ? "N/A" : `${change >= 0 ? "+" : "−"}${money(Math.abs(change))}`}</td></tr>
      <tr><th>Hedge {trade.hedge_option_type ?? hedgeEntry?.option_type ?? "N/A"} {number(trade.hedge_strike, 0)}</th>
        <td>{money(hedgeEntry?.fill_price)}</td><td>BID {money(hedgeBid)}</td><td>—</td></tr></tbody>
    </table></div>
    <p>Entry spread credit {money(trade.entry_credit)} · Current spread debit {money(trade.mark_available ? trade.current_debit : null)}</p>
  </section>;
}
