"""Read frozen exported paper documents. No database, broker or P&L fitting.

Usage: python scripts/diagnose_commodity_single_short.py --trades-file export.json
Old documents lacking causal inputs are NOT retrospectively qualified.
"""

import argparse
import json
from pathlib import Path


def diagnostic(trade):
    decision = trade.get("decision") or {}
    candidate = trade.get("candidate") or {}
    qualification = decision.get("entry_qualification") or candidate.get("qualification")
    execution = trade.get("entry_execution") or {}
    quote = (execution.get("short") or {}).get("quote") or {}
    return {
        "trade_id": trade.get("id"),
        "instrument": trade.get("instrument_id"),
        "underlying_direction": trade.get("direction"),
        "market_state": decision.get("market_state"),
        "selected_option": candidate.get("short_leg"),
        "otm_distance_points": candidate.get("short_distance_points"),
        "observed_entry_quote": {
            key: quote.get(key)
            for key in (
                "bid",
                "ask",
                "bid_quantity",
                "ask_quantity",
                "source_market_timestamp",
                "open_interest",
                "volume",
            )
        },
        "acts_directional_evidence": {
            key: decision.get(key) for key in ("alpha", "positioning", "directional_score", "directional_components")
        },
        "old_compatibility_score": decision.get("opportunity_score"),
        "commodity_native_qualification": qualification,
        "qualification_reconstruction": (
            "STORED_CAUSAL_EVIDENCE" if qualification else "NOT_RECONSTRUCTED_MISSING_CAUSAL_INPUTS"
        ),
        "entry_reason": decision.get("entry_reason"),
        "exit_reason": trade.get("exit_reason"),
        "gross_pnl": trade.get("gross_realized_pnl"),
        "net_pnl": trade.get("net_realized_pnl"),
        "source": "EXPORTED_FROZEN_PAPER_DOCUMENT_NO_RECOMPUTATION",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades-file", type=Path, required=True)
    args = parser.parse_args()
    documents = json.loads(args.trades_file.read_text())
    if isinstance(documents, dict):
        documents = documents.get("items")
    if not isinstance(documents, list) or any(not isinstance(row, dict) for row in documents):
        parser.error("expected an exported JSON list of frozen trade documents, or an items list")
    print(json.dumps({"read_only": True, "trades": [diagnostic(row) for row in documents]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
