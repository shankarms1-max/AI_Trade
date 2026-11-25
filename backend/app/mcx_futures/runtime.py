"""Glue between the deterministic McxFuturesEngine and the persistent journal: trade identity, trade projection,
the status record served by /api/mcx-futures/status, and one atomic observation step.

Trade identity: digest(instrument, decided_at) - every entry event carries the decision timestamp, and an open
position keeps it, so ENTRY_PENDING / ENTRY_FILLED / ENTRY_REJECTED / MARK / EXIT of one trade share one id across
restarts without any extra engine state.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.mcx_futures import cycle, store
from app.mcx_futures.bars import Observation
from app.mcx_futures.config import STRATEGY_ID, InstrumentConfig
from app.mcx_futures.costs import CostSchedule
from app.mcx_futures.engine import McxFuturesEngine
from app.mcx_futures.features import warmup_bars
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.spec import ContractSpec
from app.research.manifest import digest

DECISION_STATUSES = {"ENTER", "NO_ENTRY", "EXIT_SIGNAL"}


def split_components(raw: Any) -> tuple[dict[str, float], dict[str, float], dict[str, list[str]]]:
    """Engine components are {name: {points, max, unavailable}}; the API contract exposes points as numbers and the
    maxima / unavailable inputs as separate maps (unavailable inputs score zero, no rescaling)."""
    points, maxima, missing = {}, {}, {}
    for name, value in (raw or {}).items():
        if isinstance(value, dict):
            points[name] = float(value.get("points") or 0.0)
            maxima[name] = float(value.get("max") or 0.0)
            missing[name] = list(value.get("unavailable") or [])
        elif isinstance(value, (int, float)):
            points[name] = float(value)
    return points, maxima, missing


def trade_id(instrument: str, decided_at: str) -> str:
    return digest([STRATEGY_ID, instrument, decided_at])[:32]


def enrich(events: list[dict[str, Any]], instrument: str, open_decided_at: str | None,
           spec: ContractSpec) -> list[dict[str, Any]]:
    """Attach trade_id + a flat trade projection to trade events. Pure; does not alter engine payloads."""
    out = []
    current = open_decided_at
    for e in events:
        e = dict(e)
        p = e.get("payload") or {}
        kind = e["type"]
        proj: dict[str, Any] = {}
        if kind in ("ENTRY_PENDING", "ENTRY_FILLED", "ENTRY_REJECTED"):
            tid = trade_id(instrument, p["decided_at"])
            if kind == "ENTRY_PENDING":
                proj = {"at": e["at"], "direction": p.get("direction"), "score": p.get("score"),
                        "entry_reason": p.get("entry_reason"), "cycle_id": p.get("cycle_id"),
                        "contract": spec.trading_symbol, "version": STRATEGY_ID, "lot_size": spec.lot_size}
            elif kind == "ENTRY_FILLED":
                current = p["decided_at"]
                proj = {"entry_at": e["at"], "entry_price": p.get("price"), "quantity_lots": p.get("lots"),
                        "stop_price": p.get("stop"), "risk_rupees": p.get("risk_rupees")}
            else:
                proj = {"rejection_reason": p.get("reason"), "rejected_at": e["at"]}
        elif kind in ("MARK", "MARK_UNAVAILABLE", "EXIT") and current:
            tid = trade_id(instrument, current)
            if kind == "MARK":
                proj = {"at": e["at"], "mark_price": p.get("price"), "unrealized_pnl": p.get("unrealised_gross"),
                        "stop_price": p.get("stop")}
            elif kind == "MARK_UNAVAILABLE":
                proj = {"at": e["at"], "mark_price": None, "unrealized_pnl": None, "reason": "STALE_QUOTE"}
            else:
                t = p["trade"]
                costs = t.get("costs") or {}
                proj = {"entry_at": t["entry_at"], "entry_price": t["entry_price"], "exit_at": t["exit_at"],
                        "exit_price": t["exit_price"], "exit_reason": t["exit_reason"], "gross_pnl": t["gross_pnl"],
                        "fees": costs.get("total"), "costs": costs,
                        "slippage_cost": costs.get("slippage", 0.0), "net_pnl": t["net_pnl"],
                        "holding_seconds": t.get("holding_seconds"), "mae_rupees": t.get("mae_rupees"),
                        "mfe_rupees": t.get("mfe_rupees"), "quantity_lots": t.get("lots")}
                current = None
        else:
            out.append(e)
            continue
        e["trade_id"], e["projection"] = tid, proj
        out.append(e)
    return out


def status_record(engine: McxFuturesEngine, obs: Observation | None, events: list[dict[str, Any]],
                  previous: dict[str, Any], calendar: SessionCalendar, now: datetime,
                  paper_entries_enabled: bool = True) -> dict[str, Any]:
    """The JSON the API serves for one instrument; carries forward the last decision between bars."""
    snap = engine.snapshot()
    cfg, spec = engine.config, engine.spec
    available = len(snap.get("bars") or [])
    required = warmup_bars(cfg)
    warmup = {"ready": available >= required, "bars_available": available, "bars_required": required,
              "timeframe_minutes": cfg.feature_timeframe_minutes,
              "blocking": [] if available >= required else ["FEATURE_BARS_WARMING_UP"]}
    st = calendar.state(now)
    sess = snap.get("session") or {}
    window = (f"{st.session.open_at.strftime('%H:%M')}-{st.session.close_at.strftime('%H:%M')} IST"
              if st.session else None)
    feed = previous.get("feed") or {}
    if obs is not None:
        spread = (obs.ask - obs.bid) / spec.tick_size if obs.bid and obs.ask else None
        feed = {"last_quote_at": obs.source_ts.isoformat() if obs.source_ts else None,
                "observed_at": obs.at.isoformat(),
                "source_timestamp_available": obs.source_ts is not None,
                "age_seconds": obs.quote_age_seconds(), "stale": (obs.quote_age_seconds() or 0) > cfg.stale_quote_seconds,
                "bid": obs.bid, "ask": obs.ask, "bid_qty": obs.bid_qty, "ask_qty": obs.ask_qty, "ltp": obs.ltp,
                "spread_ticks": round(spread, 2) if spread is not None else None}
    decision = previous.get("last_decision")
    for e in events:
        p = e.get("payload") or {}
        if e["type"] == "DECISION" and p.get("status") in DECISION_STATUSES:
            c = p.get("cycle") or {}
            points, maxima, missing = split_components(p.get("components"))
            decision = {"at": e["at"], "status": p["status"], "direction": p.get("direction"),
                        "score": p.get("score"), "components": points, "component_max": maxima,
                        "unavailable_inputs": missing,
                        "vetoes": list(p.get("vetoes") or []) + list(p.get("risk_blockers") or [])
                        + list(p.get("blockers") or []),
                        "market_state": p.get("market_state"), "cycle_id": c.get("cycle_id"),
                        "cycle_state": c.get("cycle_state") or c.get("state"), "rearm_reason": c.get("rearm_reason"),
                        "warm": p.get("warm")}
    pos = snap.get("position")
    mark = next((e for e in reversed(events) if e["type"] == "MARK"), None)
    position = None
    if pos:
        prev_pos = previous.get("position") or {}
        position = {"direction": pos["direction"], "entry_at": pos["entry_at"], "entry_price": pos["entry_price"],
                    "stop_price": pos["stop"], "quantity_lots": pos["lots"],
                    "mark_price": mark["payload"]["price"] if mark else prev_pos.get("mark_price"),
                    "mark_at": mark["at"] if mark else prev_pos.get("mark_at"),
                    "unrealized_pnl": mark["payload"]["unrealised_gross"] if mark else prev_pos.get("unrealized_pnl")}
    r = snap.get("risk") or {}
    return {"observed_at": obs.at.isoformat() if obs else previous.get("observed_at"),
            "collector_enabled": True, "paper_entries_enabled": paper_entries_enabled,
            "execution_state": "PAPER_ENABLED" if paper_entries_enabled else "COLLECT_ONLY",
            "execution_reason": None if paper_entries_enabled else "MCX_FUTURES_TRADING_ENABLED_FALSE",
            "session": {"status": st.status, "window": window,
                        "next_open": st.next_open.isoformat() if st.next_open else None,
                        "last_entry_time": sess.get("last_entry_at"), "forced_exit_time": sess.get("forced_exit_at"),
                        "reason": None if st.status == "OPEN" else "OUTSIDE_MCX_SESSION_OR_HOLIDAY"},
            "feed": feed, "last_decision": decision, "position": position, "warmup": warmup,
            "risk": {"daily_loss_limit": cfg.daily_loss_limit, "daily_realized": r.get("realised_net"),
                     "max_risk_per_trade": cfg.max_risk_per_trade, "entries_today": r.get("entries"),
                     "max_entries": cfg.max_entries_per_session, "consecutive_losses": r.get("consecutive_losses")},
            "counters": snap.get("counters"), "config_id": cfg.config_id,
            "calibration_status": cfg.calibration_status, "error": None}


def step(sessions: sessionmaker[Session], engine: McxFuturesEngine, obs: Observation, cursor: dict[str, Any],
         calendar: SessionCalendar, now: datetime, *, allow_entries: bool = True
         ) -> tuple[McxFuturesEngine, dict[str, Any], list[dict[str, Any]]]:
    """Process one observation and persist atomically. On a lost lock race nothing is written and the caller
    must reload from the store (the engine in memory is discarded)."""
    pos = engine.position
    open_decided = pos.get("decided_at") if pos else None
    events = engine.on_observation(obs, allow_entries=allow_entries)
    enriched = enrich(events, engine.spec.instrument, open_decided, engine.spec)
    latest = status_record(engine, obs, events, cursor.get("latest") or {}, calendar, now,
                           paper_entries_enabled=allow_entries)
    lock = store.commit_step(sessions, engine.spec.instrument, cursor["lock_version"], enriched,
                             state=engine.snapshot(), latest=latest, observed_at=obs.at.isoformat(),
                             contract=contract_json(engine.spec), engine_version=STRATEGY_ID,
                             config_hash=engine.config.config_hash)
    return engine, {**cursor, "lock_version": lock, "latest": latest}, enriched


def contract_json(spec: ContractSpec) -> dict[str, Any]:
    return {"trading_symbol": spec.trading_symbol, "token": spec.token, "expiry": spec.expiry.isoformat(),
            "lot_size": spec.lot_size, "tick_size": spec.tick_size, "quotation_unit": spec.quotation_unit,
            "source": spec.source}


def open_engine(cfg: InstrumentConfig, spec: ContractSpec, costs: CostSchedule, cursor: dict[str, Any],
                calendar: SessionCalendar) -> McxFuturesEngine:
    """Restore from the persisted snapshot; refuse silently switching contract or config under an open position."""
    state = cursor.get("state") or {}
    if not state:
        return McxFuturesEngine(cfg, spec, costs, calendar=calendar)
    try:
        return McxFuturesEngine.restore(cfg, spec, costs, state, calendar=calendar)
    except ValueError:
        if state.get("position") or state.get("pending_entry"):
            raise RuntimeError("MCX_FUTURES_IDENTITY_CHANGED_WITH_EXPOSURE_REFUSED")
        engine = McxFuturesEngine(cfg, spec, costs, calendar=calendar)
        carry_market_state(state, engine)
        return engine


def _contract_identity(spec: dict[str, Any] | None) -> dict[str, Any]:
    """The tradable contract itself; ``source`` (which daily scrip-master file it was read from) is metadata."""
    return {k: v for k, v in (spec or {}).items() if k != "source"}


def carry_market_state(state: dict[str, Any], engine: McxFuturesEngine) -> int:
    """A FLAT engine whose identity changed (new config / cost version, or simply the next day's scrip master) keeps the
    MARKET DATA it already observed - completed feature bars and same-session risk and cycle counters - as long as the
    bars describe the same contract, timeframe, calendar and state schema. Without this, the daily scrip-master refresh
    discarded every bar each morning and the strategy re-warmed from zero (no historical backfill exists).
    Partial bars, session, position and pending entries are never carried. Returns the number of bars carried."""
    cfg = engine.config
    if (state.get("schema") != engine.s["schema"] or state.get("instrument") != cfg.instrument
            or state.get("calendar_source") != engine.s["calendar_source"]
            or _contract_identity(state.get("spec")) != _contract_identity(engine.s["spec"])):
        return 0
    bars = list(state.get("bars") or [])
    if any(b.get("minutes") != cfg.feature_timeframe_minutes for b in bars):
        return 0                                   # timeframe changed: bars mean something else
    engine.s["bars"] = bars[-cfg.feature_window_bars:]
    engine.s["last_ltp"], engine.s["last_atr"] = state.get("last_ltp"), state.get("last_atr")
    if isinstance(state.get("risk"), dict) and state["risk"].get("session_id"):
        engine.s["risk"] = state["risk"]           # same-session loss/entry budgets are never reset by a redeploy
    if isinstance(state.get("cycle"), dict) and state["cycle"].get("policy") == cycle.POLICY_VERSION:
        engine.s["cycle"] = state["cycle"]         # a consumed opportunity stays consumed
    engine.s["carried_market_state"] = {
        "bars": len(engine.s["bars"]), "previous_config_hash": state.get("config_hash"),
        "previous_cost_schedule": state.get("cost_schedule"), "previous_spec_source": (state.get("spec") or {}).get("source")}
    return len(engine.s["bars"])
