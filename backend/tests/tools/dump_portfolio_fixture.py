"""Dump the /api/portfolio/* payloads for the SYNTHETIC scenario (tests/portfolio_support.py) to the frontend fixture.
Run from the repo root:  PYTHONPATH=backend python backend/tests/tools/dump_portfolio_fixture.py"""
import json
import time
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.db.session import get_session_factory
from app.main import app
from app.portfolio import service
from app.portfolio.charges import ChargeModel
from tests.portfolio_support import NOW, build

OUT = Path("frontend/tests/fixtures/portfolio-synthetic.json")
S, _ = build()
service._CACHE[id(S)] = (time.monotonic() + 3600, service.load(S, now=NOW.astimezone(), charges=ChargeModel(Decimal(0)),
                                                                use_cache=False))
app.dependency_overrides[get_session_factory] = lambda: S
c = TestClient(app)
get = lambda p: c.get(f"/api/portfolio/{p}").json()  # noqa: E731
pf = service._CACHE[id(S)][1]
t1 = next(p for p in pf.positions if p["trade_id"].startswith("T1"))["position_id"]
t4 = next(p for p in pf.positions if p["trade_id"].startswith("T4"))["position_id"]
m1 = next(p for p in pf.positions if p["trade_id"].startswith("M1"))["position_id"]
payload = {
    "label": "SYNTHETIC portfolio payloads from the real projection code (backend/tests/portfolio_support.py); "
             "not market data, not production data",
    "summary": get("summary?date=2026-11-24"), "positions_open": get("positions?tab=open"),
    "positions_closed": get("positions?tab=closed"), "positions_unresolved": get("positions?tab=unresolved"),
    "positions_all": get("positions?tab=all"), "orders": get("orders"), "fills": get("fills"), "trades": get("trades"),
    "performance": get("performance"), "risk": get("risk"), "equity": get("equity?date=2026-11-24"),
    "strategies": get("strategies"), "alerts": get("alerts"), "header": get("header"), "sources": get("sources"),
    "strategy_nifty_v3": get("strategies/nifty-acts_v3"), "strategy_crude": get("strategies/mcx-crudeoilm"),
    "position_detail_closed_spread": get(f"positions/{t1}"), "position_detail_expired_open": get(f"positions/{t4}"),
    "position_detail_futures": get(f"positions/{m1}"),
}
OUT.write_text(json.dumps(payload, indent=1, default=str))
print("wrote", OUT, OUT.stat().st_size // 1024, "KB", {k: type(v).__name__ for k, v in list(payload.items())[1:4]})
