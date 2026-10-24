"""Finite next-session verifier. Docker exec/local read-only API; no broker calls."""

import argparse
from datetime import date, datetime, time as wall
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if socket.gethostname() != "ip-172-26-5-227":
        raise SystemExit("HOST_IDENTITY_REFUSED")
    tz = ZoneInfo("Asia/Kolkata")
    root = Path("/srv/commodity-acts/monitoring")
    root.mkdir(mode=0o750, exist_ok=True)
    path = root / ("session-" + args.session.isoformat() + ".jsonl")
    failures = root / ("failures-" + args.session.isoformat() + ".jsonl")
    seen_errors, seen_fills = set(), {}
    alpha_valid = {"CRUDEOILM": False, "NATGASMINI": False}
    latest = None
    while True:
        now = datetime.now(tz)
        if now.date() != args.session or now.time().replace(tzinfo=None) > wall(23, 36):
            break
        if now.time().replace(tzinfo=None) < wall(9, 5):
            time.sleep(min(60, (datetime.combine(args.session, wall(9, 5), tz) - now).total_seconds()))
            continue
        command = [
            "docker",
            "exec",
            "commodity_acts-backend-1",
            "python",
            "-B",
            "/app/worker/commodity_session_verify.py",
        ]
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=45)
            latest = json.loads(p.stdout)
            if p.returncode not in (0, 1) or not isinstance(latest.get("errors"), list):
                raise ValueError("invalid monitor result")
        except Exception:
            latest = {
                "at_ist": now.isoformat(),
                "errors": ["VERIFIER_OR_COMMODITY_API_UNAVAILABLE"],
                "broker_requests": 0,
            }
        for row in latest.get("instruments", []):
            if (row.get("alpha") or {}).get("validity_state") == "VALID":
                alpha_valid[row["instrument"]] = True
        for fills in latest.get("fills", {}).values():
            for fill in fills:
                if fill.get("observed_at", "").startswith(args.session.isoformat()):
                    seen_fills[fill["id"]] = fill
        with path.open("a") as stream:
            stream.write(json.dumps(latest, sort_keys=True) + "\n")
        errors = set(latest["errors"])
        new = errors - seen_errors
        if new:
            alert = {"at_ist": now.isoformat(), "operational_failures": sorted(new), "evidence_file": str(path)}
            with failures.open("a") as stream:
                stream.write(json.dumps(alert, sort_keys=True) + "\n")
            print(json.dumps(alert, sort_keys=True), flush=True)
        seen_errors = errors
        end = datetime.combine(args.session, wall(23, 35), tz)
        if now >= end:
            break
        time.sleep(min(300, max(1, (end - datetime.now(tz)).total_seconds())))
    summary = {
        "session": args.session.isoformat(),
        "alpha_ever_valid": alpha_valid,
        "genuine_entry_fills": sum(f.get("kind") == "ENTRY" for f in seen_fills.values()),
        "genuine_exit_fills": sum(f.get("kind") == "EXIT" for f in seen_fills.values()),
        "last_errors": latest.get("errors", []) if latest else [],
        "broker_requests": 0,
    }
    (root / ("summary-" + args.session.isoformat() + ".json")).write_text(json.dumps(summary, indent=2) + "\n")
    return 1 if summary["last_errors"] else 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
