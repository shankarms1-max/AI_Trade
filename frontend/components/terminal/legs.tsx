"use client";
import { num, int, rupees } from "@/lib/terminal-format";
import { PnL, Val } from "@/components/terminal/primitives";
import type { Leg, Position } from "@/types/portfolio";

/**
 * Legs of a multi-leg parent (SHORT/HEDGE of a credit spread) or the single OUTRIGHT leg of a future.
 * Prices are the recorded fills; bid/ask are the latest quote the API recorded. Net credit / est. buyback come
 * from the position, never from adding legs in the browser.
 */
export function ExpandableLegs({ position }: { position: Position }) {
  const spread = position.structure === "CREDIT_SPREAD";
  return <div>
    <table className="tm-legs" aria-label={`Legs of ${position.contract}`}>
      <thead><tr><th>Role</th><th>Contract</th><th>Side</th>{spread && <th>Strike</th>}{spread && <th>Type</th>}<th className="tm-num">Qty</th><th className="tm-num">Entry</th><th className="tm-num">Exit</th><th className="tm-num">Bid</th><th className="tm-num">Ask</th></tr></thead>
      <tbody>{position.legs.map((l: Leg, i) => <tr key={`${l.role}-${i}`}>
        <td>{l.role}</td><td className="tm-mono">{l.symbol ?? "—"}</td><td>{l.side_entry ?? "—"}</td>
        {spread && <td className="tm-num">{num(l.strike, 0)}</td>}{spread && <td>{l.option_type ?? "—"}</td>}
        <td className="tm-num">{int(l.quantity)}</td><td className="tm-num">{num(l.entry_price)}</td>
        <td className="tm-num"><Val>{l.exit_price == null ? null : num(l.exit_price)}</Val></td>
        <td className="tm-num"><Val reason="no quote recorded">{l.bid == null ? null : num(l.bid)}</Val></td>
        <td className="tm-num"><Val reason="no quote recorded">{l.ask == null ? null : num(l.ask)}</Val></td>
      </tr>)}</tbody>
    </table>
    <div className="tm-caption">
      {spread ? <>Net credit / unit <b>{num(position.entry_price)}</b> · {position.status === "CLOSED" ? <>net debit paid <b>{num(position.exit_price)}</b></> : <>est. buyback (executable debit) <b>{position.mark.price == null ? "—" : num(position.mark.price)}</b>{position.mark.price == null && position.mark.last_known_price != null ? <> · last known {num(position.mark.last_known_price)} (not executable)</> : null}</>} · defined max loss <b>{rupees(position.defined_max_loss)}</b></> :
        <>Entry <b>{num(position.entry_price)}</b> · mark <b>{position.mark.price == null ? "—" : num(position.mark.price)}</b> · unrealized <PnL value={position.unrealized_gross} reason={position.mark.reason}/></>}
    </div>
  </div>;
}
