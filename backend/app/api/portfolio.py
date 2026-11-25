"""/api/portfolio/* - unified, read-only financial read model. GET only. Money is computed here, never in the browser."""
from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session, sessionmaker

from app.api import engines as engines_api
from app.api.mcx_futures import build_status as mcx_build_status
from app.db.session import get_session_factory
from app.mcx_futures.session import SessionCalendar
from app.portfolio import analytics, service
from app.portfolio.normalize import ENGINES, iso

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
MODES = ("PAPER", "LIVE")


def _d(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{name.upper()}_MUST_BE_YYYY_MM_DD") from None


def _filters(mode: str | None, strategy: str | None, engine: str | None, instrument: str | None,
             version: str | None) -> dict[str, Any]:
    if mode and mode.upper() not in MODES:
        raise HTTPException(status_code=422, detail="MODE_MUST_BE_PAPER_OR_LIVE")
    if instrument and instrument.upper() not in {e["instrument"] for e in ENGINES.values()}:
        raise HTTPException(status_code=422, detail="UNKNOWN_INSTRUMENT")
    return {"mode": mode, "strategy": strategy, "engine": engine, "instrument": instrument.upper() if instrument
            else None, "version": version}


def _pf(sessions):
    return service.load(sessions)


@router.get("/summary")
def summary(date_: str | None = Query(None, alias="date"), date_from: str | None = Query(None, alias="from"),
            date_to: str | None = Query(None, alias="to"), mode: str | None = None, strategy: str | None = None,
            engine: str | None = None, instrument: str | None = None, version: str | None = None,
            sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    start = _d(date_from or date_, "from")
    end = _d(date_to or date_, "to") or start
    return service.summary(_pf(sessions), start=start, end=end, **_filters(mode, strategy, engine, instrument,
                                                                           version))


@router.get("/positions")
def positions(tab: str = Query("open", pattern="^(open|closed|unresolved|all)$"), limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0, le=100000), date_from: str | None = Query(None, alias="from"),
              date_to: str | None = Query(None, alias="to"), mode: str | None = None, strategy: str | None = None,
              engine: str | None = None, instrument: str | None = None, version: str | None = None,
              direction: str | None = None,
              sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    f = _filters(mode, strategy, engine, instrument, version)
    return service.positions_list(_pf(sessions), tab=tab, limit=limit, offset=offset, start=_d(date_from, "from"),
                                  end=_d(date_to, "to"), direction=direction, **f)


@router.get("/positions/{position_id}")
def position(position_id: str, sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    detail = analytics.position_detail(sessions, _pf(sessions), position_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="POSITION_NOT_FOUND")
    return detail


@router.get("/orders")
def orders(status: str | None = None, intent: str | None = None, limit: int = Query(50, ge=1, le=200),
           offset: int = Query(0, ge=0, le=100000), date_from: str | None = Query(None, alias="from"),
           date_to: str | None = Query(None, alias="to"), mode: str | None = None, strategy: str | None = None,
           engine: str | None = None, instrument: str | None = None, version: str | None = None,
           sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    f = _filters(mode, strategy, engine, instrument, version)
    return service.orders_list(_pf(sessions), status=status, intent=intent, limit=limit, offset=offset,
                               start=_d(date_from, "from"), end=_d(date_to, "to"), **f)


@router.get("/fills")
def fills(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
          sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    items = sorted(pf.fills, key=lambda f: f["at"] or "", reverse=True)
    return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset: offset + limit]}


def _trade_args(date_from, date_to, result, exit_reason, mode, strategy, engine, instrument, version, direction):
    if result not in (None, "win", "loss"):
        raise HTTPException(status_code=422, detail="RESULT_MUST_BE_WIN_OR_LOSS")
    return dict(start=_d(date_from, "from"), end=_d(date_to, "to"), result=result, exit_reason=exit_reason,
                direction=direction, **_filters(mode, strategy, engine, instrument, version))


@router.get("/trades")
def trades(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
           result: str | None = None, exit_reason: str | None = None, mode: str | None = None,
           strategy: str | None = None, engine: str | None = None, instrument: str | None = None,
           version: str | None = None, direction: str | None = None, sort: str = "exit_at", desc: bool = True,
           limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000),
           sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    args = _trade_args(date_from, date_to, result, exit_reason, mode, strategy, engine, instrument, version, direction)
    return service.trades_page(_pf(sessions), sort=sort, desc=desc, limit=limit, offset=offset, **args)


@router.get("/trades.csv")
def trades_csv(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
               result: str | None = None, exit_reason: str | None = None, mode: str | None = None,
               strategy: str | None = None, engine: str | None = None, instrument: str | None = None,
               version: str | None = None, direction: str | None = None,
               sessions: sessionmaker[Session] = Depends(get_session_factory)) -> Response:
    args = _trade_args(date_from, date_to, result, exit_reason, mode, strategy, engine, instrument, version, direction)
    return Response(service.trades_csv(_pf(sessions), **args), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="tradebook.csv"'})


@router.get("/performance")
def performance(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                mode: str | None = None, strategy: str | None = None, engine: str | None = None,
                instrument: str | None = None, version: str | None = None,
                sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    return analytics.performance(_pf(sessions), start=_d(date_from, "from"), end=_d(date_to, "to"),
                                 **_filters(mode, strategy, engine, instrument, version))


@router.get("/risk")
def risk(mode: str | None = None, strategy: str | None = None, engine: str | None = None,
         instrument: str | None = None, version: str | None = None,
         sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    return analytics.risk(_pf(sessions), **_filters(mode, strategy, engine, instrument, version))


@router.get("/equity")
def equity(date_: str | None = Query(None, alias="date"), mode: str | None = None, strategy: str | None = None,
           engine: str | None = None, instrument: str | None = None, version: str | None = None,
           sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    day = _d(date_, "date") or pf.now.astimezone(IST).date()
    return analytics.equity(sessions, pf, day, **_filters(mode, strategy, engine, instrument, version))


def _engine_rows(sessions) -> list[dict[str, Any]]:
    return engines_api.build(sessions)["engines"]


@router.get("/strategies")
def strategies(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    return {"as_of": iso(pf.now), "items": analytics.strategies(pf, _engine_rows(sessions),
                                                                 mcx_build_status(sessions, pf.now))}


@router.get("/strategies/{engine_id}")
def strategy(engine_id: str, sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    cards = analytics.strategies(pf, _engine_rows(sessions), mcx_build_status(sessions, pf.now))
    card = next((c for c in cards if c["engine_id"] == engine_id), None)
    if card is None:
        raise HTTPException(status_code=404, detail="STRATEGY_NOT_FOUND")
    return {**card, "config": analytics.strategy_config(pf, engine_id),
            "performance": analytics.performance(pf, engine=engine_id)}


@router.get("/alerts")
def alerts(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    items = service.alerts(pf, _engine_rows(sessions))
    return {"as_of": iso(pf.now), "critical": sum(1 for a in items if a["severity"] == "CRITICAL"),
            "warning": sum(1 for a in items if a["severity"] == "WARNING"), "items": items}


@router.get("/header")
def header(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    """Global terminal header: clock, sessions, inferred feed connectivity, freshness, critical alerts."""
    pf = _pf(sessions)
    now = pf.now
    rows = _engine_rows(sessions)
    mcx = SessionCalendar().state(now)
    nse_open = engines_api.nifty_session_open(now)
    feeds = [r for r in rows if r["family"] in ("ACTS", "MCX_FUTURES")]
    in_session = nse_open or mcx.status == "OPEN"
    # Every configured, ACTIVE engine is accounted for exactly once: fresh, stale, unavailable or market closed.
    # DISABLED / NOT_CONFIGURED engines are not active feeds (counted separately, never as "fresh").
    klass = {"RUNNING": "fresh", "DATA_STALE": "stale", "MARKET_CLOSED": "market_closed"}
    active, inactive = [], []
    for r in feeds:
        if r["state"] in ("DISABLED", "NOT_CONFIGURED"):
            inactive.append(r)
        else:
            active.append({**r, "classification": klass.get(r["state"], "unavailable")})
    count = lambda k: sum(1 for r in active if r["classification"] == k)  # noqa: E731
    fresh, stale, unavailable, closed = count("fresh"), count("stale"), count("unavailable"), count("market_closed")
    live = fresh + stale + unavailable          # feeds that should be producing observations right now
    connectivity = ("NOT_APPLICABLE_MARKETS_CLOSED" if live == 0 and not in_session else
                    "UNKNOWN" if live == 0 else
                    "CONNECTED_INFERRED" if fresh == live else
                    "PARTIAL_INFERRED" if fresh > 0 else "DISCONNECTED_INFERRED")
    items = service.alerts(pf, rows)
    return {"as_of": iso(now), "now_ist": now.astimezone(IST).isoformat(), "timezone": "Asia/Kolkata",
            "nse": {"status": "OPEN" if nse_open else "CLOSED"},
            "mcx": {"status": mcx.status, "window": (f"{mcx.session.open_at:%H:%M}-{mcx.session.close_at:%H:%M} IST"
                                                    if mcx.session else None),
                    "next_open": iso(mcx.next_open)},
            "broker_connectivity": {"state": connectivity, "basis": "inferred from fresh market observations of "
                                    "every active in-session feed; the broker session itself is not probed (no extra "
                                    "broker traffic)"},
            "data_freshness": {"fresh_feeds": fresh, "stale_feeds": stale, "unavailable_feeds": unavailable,
                               "market_closed_feeds": closed, "feeds": len(active), "inactive_feeds": len(inactive),
                               "stale": [r["label"] for r in active if r["classification"] in ("stale", "unavailable")],
                               "by_feed": [{"id": r["id"], "label": r["label"], "state": r["state"],
                                            "classification": r["classification"], "reason": r.get("state_reason")}
                                           for r in active]},
            "critical_alerts": sum(1 for a in items if a["severity"] == "CRITICAL"),
            "warning_alerts": sum(1 for a in items if a["severity"] == "WARNING"),
            "default_mode": "PAPER", "live_execution": "NONE"}


@router.get("/sources")
def sources(sessions: sessionmaker[Session] = Depends(get_session_factory)) -> dict[str, Any]:
    pf = _pf(sessions)
    return {"as_of": iso(pf.now), "sources": pf.sources, "charge_model": pf.charges.status(),
            "brokerage_per_order": float(pf.charges.brokerage_per_order),
            "definitions": service.__doc__}
