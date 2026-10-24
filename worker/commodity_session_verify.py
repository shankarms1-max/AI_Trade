"""Read-only next-session observations; local API GETs only, never broker calls."""

import argparse
from datetime import datetime, time
import json
from urllib.request import urlopen
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def inspect_observations(research, paper, now):
    errors, evidence = [], []
    active_session = now.weekday() < 5 and time(9, 2) <= now.time().replace(tzinfo=None) <= time(23, 30)
    rows = {row["instrument_id"]: row for row in paper["instruments"]}
    for row in research["instruments"]:
        instrument = row["instrument_id"]
        p = rows[instrument]
        # Current API uses the configured MCX calendar. An explicit closed
        # session (including a holiday) must not become a false stale-feed alarm.
        # Older exports without this field retain the original monitor window.
        inspect_active = active_session and p.get("market_open") is not False
        latest = row.get("latest") or {}
        alpha = latest.get("alpha") or {}
        decision = (p.get("latest") or {}).get("decision") or {}
        at = datetime.fromisoformat(latest["observed_at"]) if latest.get("observed_at") else None
        age = (now - at).total_seconds() if at else None
        if inspect_active and (age is None or not 0 <= age <= 90):
            errors.append(instrument + ":OBSERVATIONS_STALE_OR_UNAVAILABLE")
        paper_at = (p.get("latest") or {}).get("observed_at")
        if inspect_active and (
            not paper_at or not 0 <= (now - datetime.fromisoformat(paper_at)).total_seconds() <= 90
        ):
            errors.append(instrument + ":PAPER_PROCESSING_STALE_OR_UNAVAILABLE")
        if inspect_active and not p.get("paper_enabled"):
            errors.append(instrument + ":PAPER_WORKER_DISABLED")
        if inspect_active and (p.get("latest") or {}).get("contract_renewal_status") != "VERIFIED":
            errors.append(instrument + ":CONTRACT_RENEWAL_UNAVAILABLE")
        contract = p.get("execution_evidence")
        if inspect_active and contract and at and age is not None and 0 <= age <= 90:
            if (contract.get("future_token"), contract.get("future_expiry"), contract.get("option_expiry")) != (
                latest.get("future_token"),
                latest.get("future_expiry"),
                latest.get("option_expiry"),
            ):
                errors.append(instrument + ":CONTRACT_IDENTITY_MISMATCH")
        if now.time().replace(tzinfo=None) >= time(22, 32) and p.get("open_positions"):
            errors.append(instrument + ":FORCED_EXIT_NOT_FILLED")
        if p.get("unresolved_exposure"):
            errors.append(instrument + ":UNRESOLVED_EXPOSURE")
        evidence.append(
            {
                "instrument": instrument,
                "observations": row["observation_count"],
                "decisions": row["decision_count"],
                "observed_at": at.isoformat() if at else None,
                "source_timestamps": latest.get("timestamp_provenance"),
                "data_provenance": latest.get("data_provenance"),
                "alpha_readiness": row["readiness"],
                "alpha": alpha,
                "contract_evidence": contract,
                "contract_renewal_status": (p.get("latest") or {}).get("contract_renewal_status"),
                "paper_decision": decision,
                "risk_blocks": (p.get("latest") or {}).get("risk_blocks"),
                "pending_entries": p["pending_entries"],
                "open_positions": p["open_positions"],
                "closed_trades": p["closed_trades"],
                "realized_gross": p["realized_gross"],
                "realized_net": p["realized_net"],
            }
        )
    return {"at_ist": now.isoformat(), "errors": errors, "instruments": evidence, "broker_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    now = datetime.now(IST)
    try:

        def read(path):
            with urlopen("http://127.0.0.1:8000/api/commodity-acts/" + path, timeout=10) as response:
                return json.load(response)

        result = inspect_observations(read("status"), read("paper/summary"), now)
        result["fills"] = {
            instrument: read("paper/fills?instrument=" + instrument + "&limit=100")["items"]
            for instrument in ("CRUDEOILM", "NATGASMINI")
        }
    except Exception:
        result = {"at_ist": now.isoformat(), "errors": ["COMMODITY_READ_ONLY_API_UNAVAILABLE"], "broker_requests": 0}
    print(json.dumps(result, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
