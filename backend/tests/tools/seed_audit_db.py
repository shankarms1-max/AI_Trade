"""Seed a LOCAL sqlite audit database for browser/financial audits.

  PYTHONPATH=backend:worker python backend/tests/tools/seed_audit_db.py OUT.db [EXTRACT.json.gz]

With an EXTRACT (the read-only production extract made by scripts/acts_prod_readonly_extract.py) the NIFTY ACTS journal,
its events and snapshot rows are loaded (REAL data, local only, never commit the output). Always adds SYNTHETIC MCX
futures runs (labelled synthetic) so the MCX pages have rows. Not used by the test suite."""
import gzip
import json
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.base import Base
import app.db.models  # noqa: F401
from app.mcx_futures.models import McxFuturesCursor
from app.scalper.models import ScalperMarketSnapshotRecord as Snap

out = Path(sys.argv[1])
out.unlink(missing_ok=True)
engine = create_engine(f"sqlite:///{out}")
Base.metadata.create_all(engine)
S = sessionmaker(engine, expire_on_commit=False)
with S.begin() as s:
    for inst in ("CRUDEOILM", "NATGASMINI"):
        s.add(McxFuturesCursor(instrument=inst, sequence=0, head_hash="GENESIS", lock_version=0, contract_json="{}",
                               state_json="{}", latest_json="{}"))
if len(sys.argv) > 2:
    d = json.loads(gzip.decompress(Path(sys.argv[2]).read_bytes()))["tables"]
    ts = {}
    with S.begin() as s:
        c = d["acts_paper_cursor"][0]
        s.add(ActsPaperCursor(id=1, snapshot_id=c["snapshot_id"], observed_at=c["observed_at"], sequence=c["sequence"],
                              head_hash=c["head_hash"], lock_version=0, watch_json="{}", latest_json=c["latest_json"]))
        for r in d["acts_paper_trades"]:
            s.add(ActsPaperTrade(id=r["id"], decision_snapshot_id=r["decision_snapshot_id"], state=r["state"],
                                 document=r["document"]))
            ts.setdefault(r["decision_snapshot_id"], json.loads(r["document"])["decision_timestamp"])
        for e in d["trade_events"]:
            ts[e["snapshot_id"]] = json.loads(e["payload"])["market_timestamp"]
            s.add(ActsPaperEvent(sequence=e["sequence"], event_key=f"{e['snapshot_id']}:{e['trade_id']}:{e['event_type']}:{e['sequence']}",
                                 event_type=e["event_type"], snapshot_id=e["snapshot_id"], trade_id=e["trade_id"],
                                 previous_hash="AUDIT_SEED", event_hash="AUDIT_SEED", payload=e["payload"]))
        for sid, t in ts.items():
            at = datetime.fromisoformat(t)
            s.add(Snap(id=sid, capture_key=f"audit-{sid}", captured_at=at, request_started_at=at, response_received_at=at,
                       source_market_timestamp=at, nifty_spot=25000, lot_size=65, atm_strike=25000, expiry=date(2026, 11, 23),
                       source="AUDIT_SEED", feature_json="{}", signal_json="{}", created_at=at))
    print("NIFTY ACTS rows:", len(d["acts_paper_trades"]), "events:", len(d["trade_events"]), "snapshots:", len(ts))
from tests.test_mcx_futures_integration import _run  # noqa: E402
from tests.test_mcx_futures_support import observations_from_bars, trend_bars  # noqa: E402

_run("CRUDEOILM", S, observations_from_bars(trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6, tick=1.0,
                                                       minutes=600, wiggle=2.0), 1.0))
_run("NATGASMINI", S, observations_from_bars(trend_bars(date(2026, 11, 24), start_price=310, slope=-0.03, tick=0.1,
                                                        minutes=600, wiggle=0.12), 0.1))
print("synthetic MCX runs seeded ->", out)
