# flake8: noqa: E501
"""SYNTHETIC deterministic portfolio scenario with hand-computed answers (not market data, not production data).

NOW = 2026-11-24 15:00 IST. All ACTS trades: lot 65, 1 lot, so quantity 65 per leg.
  T1  CLOSED  bull put 25400/25000 PE  entry 100.00/20.00 (credit 80)  exit 90.00/15.00   gross = (100-20-90+15)*65 = +325
  T2  CLOSED  bear call 25600/26000 CE entry  50.00/ 5.00 (credit 45)  exit 62.00/ 3.00   gross = (50-5-62+3)*65   = -910
  T3  OPEN    bear call 25700/26100 CE entry  40.00/ 4.00 (credit 36)  fresh mark debit 38 -> unrealized (36-38)*65 = -130
  T4  OPEN    bear call 25600/26000 CE expiry 2026-11-23, entry 23-Nov 12:24:45 credit 26.55, last mark debit 28.15
              (the 2026-11-23 incident shape): expired, never closed -> flagged, EXCLUDED from totals
  T5  ENTRY_REJECTED (direction reversed before fill)
  T6  CLOSED  expired-unpriced (no exit price) -> excluded from realized totals, counted
MCX (CRUDEOILM lot 10): M1 CLOSED LONG 5600 -> 5615 gross +150 ; M2 OPEN LONG 5600 fresh mark 5610 -> unrealized +100
NATGASMINI (lot 250): M3 CLOSED SHORT 310.0 -> 308.5 gross +375
"""
from __future__ import annotations

from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.db.base import Base
import app.db.models  # noqa: F401
from app.mcx_futures.models import McxFuturesCursor, McxFuturesTrade
from app.scalper.models import ScalperMarketSnapshotRecord as Snap

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 24, 15, 0, 0, tzinfo=IST)
POLICY = {"config": {"hard_daily_loss": 50000, "max_loss_per_trade": 25000, "max_trades_per_day": 6,
                     "stop_credit_multiple": 1.5, "signal_min_score": 80, "forced_exit_time": "15:20:00"},
          "parameters": {"tradeable_score": 55}, "exit_policy": {"version": "MIRROR_EXIT_V1"}}


def t(day: int, h: int, m: int, s: int = 0, month: int = 11) -> str:
    return datetime(2026, month, day, h, m, s, tzinfo=IST).isoformat()


def leg(strike, typ, expiry):
    return {"strike": strike, "option_type": typ, "trading_symbol": f"NIFTY{expiry:%y%b}{int(strike)}{typ}".upper(),
            "expiry": expiry.isoformat()}


def q(fill, bid, ask):
    return {"fill_price": fill, "quote": {"bid": bid, "ask": ask, "bid_quantity": 650, "ask_quantity": 650}}


def acts_doc(*, entry, exit_=None, short, hedge, typ, expiry, decided, entered, exited=None, credit, debit=None,
             direction, reason="OPPOSITE_ACTS_V2_ENTRY_SIGNAL", score=64.0, extra=None, version="ACTS_V3"):
    d = {"strategy_version": version, "direction": direction, "decision_timestamp": decided, "lot_size": 65, "lots": 1,
         "candidate": {"short_leg": leg(short, typ, expiry), "long_leg": leg(hedge, typ, expiry),
                       "spread_width": abs(short - hedge), "executable_credit": credit},
         "decision_executable_credit": credit, "opportunity_score": score, "score_band": "TRADEABLE",
         "score_components": {"momentum": 20, "structure": 14, "positioning": 12}, "cycle_id": "cyc-" + decided[-14:-9],
         "entry_reason": "FIRST_OPPORTUNITY", "policy": POLICY, "policy_hash": "synthetic-policy-hash",
         "decision_entry_quotes": {"short": q(entry[0], entry[0], entry[0] + 0.5), "long": q(entry[1], entry[1] - .5, entry[1])}}
    if entered:
        d.update(entry_timestamp=entered, entry_credit=credit, entry_spot=25500.0,
                 entry_quotes={"short": q(entry[0], entry[0], entry[0] + .5), "long": q(entry[1], entry[1] - .5, entry[1])})
    if exited:
        d.update(exit_timestamp=exited, exit_debit=debit, exit_reason=reason, holding_seconds=(
            datetime.fromisoformat(exited) - datetime.fromisoformat(entered)).total_seconds(),
            exit_quotes={"short": q(exit_[0], exit_[0], exit_[0]), "long": q(exit_[1], exit_[1], exit_[1])},
            final_outcome={"gross_rupees": round(((entry[0] - entry[1]) - (exit_[0] - exit_[1])) * 65, 2)},
            mae=-1.0, mfe=3.0)
    d.update(extra or {})
    return d


def build():
    """Returns (sessionmaker, truth dict of hand-computed expectations)."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    S = sessionmaker(engine, expire_on_commit=False)
    exp_far, exp_past = date(2026, 12, 1), date(2026, 11, 23)
    docs = {
        "T1": ("CLOSED", acts_doc(entry=(100.0, 20.0), exit_=(90.0, 15.0), short=25400, hedge=25000, typ="PE", expiry=exp_far,
                                  decided=t(24, 10, 0), entered=t(24, 10, 0, 15), exited=t(24, 10, 30, 15), credit=80.0,
                                  debit=75.0, direction="BULL")),
        "T2": ("CLOSED", acts_doc(entry=(50.0, 5.0), exit_=(62.0, 3.0), short=25600, hedge=26000, typ="CE", expiry=exp_far,
                                  decided=t(24, 11, 0), entered=t(24, 11, 0, 15), exited=t(24, 11, 20, 15), credit=45.0,
                                  debit=59.0, direction="BEAR", reason="SPREAD_STOP", score=70.2)),
        "T3": ("OPEN", acts_doc(entry=(40.0, 4.0), short=25700, hedge=26100, typ="CE", expiry=exp_far,
                                decided=t(24, 14, 50), entered=t(24, 14, 50, 15), credit=36.0, direction="BEAR",
                                extra={"current_debit": 38.0, "gross_mark_rupees": -130.0, "last_snapshot_id": 3030})),
        "T4": ("OPEN", acts_doc(entry=(63.95, 37.4), short=25600, hedge=26000, typ="CE", expiry=exp_past,
                                decided=t(23, 12, 24, 30), entered=t(23, 12, 24, 45), credit=26.55, direction="BEAR",
                                extra={"current_debit": 28.15, "gross_mark_rupees": -104.0, "last_snapshot_id": 4040})),
        "T5": ("ENTRY_REJECTED", acts_doc(entry=(30.0, 3.0), short=25800, hedge=26200, typ="CE", expiry=exp_far,
                                          decided=t(24, 13, 0, 15), entered=None, credit=27.0, direction="BEAR",
                                          extra={"rejection_reason": "PENDING_ACTS_V2_DIRECTION_REVERSED"})),
        "T6": ("CLOSED", acts_doc(entry=(30.0, 3.0), short=25700, hedge=26100, typ="CE", expiry=date(2026, 11, 20),
                                  decided=t(19, 13, 0, 15), entered=t(19, 13, 0, 30), credit=27.0, direction="BEAR",
                                  extra={"expiry_terminalization": True, "exit_reason": "CONTRACT_EXPIRED_UNPRICED"})),
    }
    sid = 1000
    snap_ids: dict[str, int] = {}
    with S.begin() as s:
        for name, (state, doc) in docs.items():
            sid += 10
            snap_ids[name] = sid
            s.add(ActsPaperTrade(id=f"{name}-" + "0" * 20, decision_snapshot_id=sid, state=state, document=json.dumps(doc)))
            at = datetime.fromisoformat(doc["decision_timestamp"])
            s.add(Snap(id=sid, capture_key=f"k{sid}", captured_at=at, request_started_at=at, response_received_at=at,
                       nifty_spot=25500, lot_size=65, atm_strike=25500, expiry=exp_far, source="SYNTH", feature_json="{}",
                       signal_json="{}", created_at=at))
        s.add(ActsPaperCursor(id=1, observed_at=t(24, 14, 59, 30), sequence=1, head_hash="x", lock_version=0, watch_json="{}",
                              latest_json=json.dumps({"status": "NO_SETUP", "strategy_version": "ACTS_V3", "direction": None,
                                                      "opportunity_score": 41.2, "primary_blocker": "MAX_OPEN_POSITIONS_REACHED",
                                                      "hard_blockers": ["MAX_OPEN_POSITIONS_REACHED"]})))
        seq = 0

        def event(trade, kind, at_s, sn, payload):
            nonlocal seq
            seq += 1
            at = datetime.fromisoformat(at_s)
            if not s.get(Snap, sn):
                s.add(Snap(id=sn, capture_key=f"k{sn}", captured_at=at, request_started_at=at, response_received_at=at,
                           nifty_spot=25500, lot_size=65, atm_strike=25500, expiry=exp_far, source="SYNTH",
                           feature_json="{}", signal_json="{}", created_at=at))
            s.add(ActsPaperEvent(sequence=seq, event_key=f"e{seq}", event_type=kind, snapshot_id=sn, trade_id=trade,
                                 previous_hash="p", event_hash="h", payload=json.dumps({"market_timestamp": at_s, **payload})))


        def T(n):
            return f"{n}-" + "0" * 20

        event(T("T1"), "ACTS_ENTRY", t(24, 10, 0, 15), 5001, {})
        event(T("T1"), "ACTS_MARK", t(24, 10, 10), 5002, {"available": True, "trade_document": {"gross_mark_rupees": 130.0, "current_debit": 78.0}})
        event(T("T1"), "ACTS_MARK", t(24, 10, 20), 5003, {"available": True, "trade_document": {"gross_mark_rupees": 260.0, "current_debit": 76.0}})
        event(T("T1"), "ACTS_EXIT", t(24, 10, 30, 15), 5004, {})
        event(T("T2"), "ACTS_ENTRY", t(24, 11, 0, 15), 5005, {})
        event(T("T2"), "ACTS_MARK", t(24, 11, 10), 5006, {"available": True, "trade_document": {"gross_mark_rupees": -390.0, "current_debit": 51.0}})
        event(T("T2"), "ACTS_MARK", t(24, 11, 15), 5007, {"available": False, "reason": "STALE", "trade_document": {}})
        event(T("T2"), "ACTS_EXIT", t(24, 11, 20, 15), 5008, {})
        event(T("T3"), "ACTS_ENTRY", t(24, 14, 50, 15), 5009, {})
        event(T("T3"), "ACTS_MARK", t(24, 14, 59, 30), 5010, {"available": True, "quotes": {"short": {"fill_price": 39.0}, "long": {"fill_price": 1.0}},
                                                              "trade_document": {"gross_mark_rupees": -130.0, "current_debit": 38.0}})
        event(T("T4"), "ACTS_MARK", t(23, 12, 46), 5011, {"available": True, "quotes": {"short": {"fill_price": 44.0}, "long": {"fill_price": 15.85}},
                                                          "trade_document": {"gross_mark_rupees": -104.0, "current_debit": 28.15}})
        event(T("T5"), "ACTS_ENTRY_REJECTED", t(24, 13, 0, 30), 5012, {"reason": "PENDING_ACTS_V2_DIRECTION_REVERSED"})
        for inst in ("CRUDEOILM", "NATGASMINI"):
            s.add(McxFuturesCursor(instrument=inst, sequence=0, head_hash="GENESIS", lock_version=0, contract_json="{}",
                                   state_json="{}", latest_json="{}"))

        def mcx(trade_id, inst, state, **doc):
            base = {"trade_id": trade_id, "instrument": inst, "mode": "PAPER", "version": "ACTS_V3_MCX_FUTURES",
                    "contract": f"{inst}18DEC26FUT", "lot_size": 10 if inst == "CRUDEOILM" else 250, "quantity_lots": 1,
                    "at": doc.get("entry_at"), "journal_sequence": 1, "cycle_id": "mcx-cyc", "score": 88.0,
                    "entry_reason": "FIRST_OPPORTUNITY"}
            base.update(doc)
            s.add(McxFuturesTrade(trade_id=trade_id, instrument=inst, trading_date=date(2026, 11, 24), state=state,
                                  document=json.dumps(base)))
        mcx("M1", "CRUDEOILM", "CLOSED", direction="LONG", entry_at=t(24, 10, 0), entry_price=5600.0, exit_at=t(24, 12, 0),
            exit_price=5615.0, exit_reason="MAX_HOLDING_TIME", gross_pnl=150.0, fees=9.65, net_pnl=140.35, stop_price=5592.0,
            holding_seconds=7200, mae_rupees=20.0, mfe_rupees=160.0)
        mcx("M2", "CRUDEOILM", "OPEN", direction="LONG", entry_at=t(24, 14, 55), entry_price=5600.0, stop_price=5590.0,
            last_mark={"at": t(24, 14, 59, 50), "mark_price": 5610.0, "unrealized_pnl": 100.0, "stop_price": 5590.0})
        mcx("M3", "NATGASMINI", "CLOSED", direction="SHORT", entry_at=t(24, 12, 0), entry_price=310.0, exit_at=t(24, 14, 0),
            exit_price=308.5, exit_reason="ATR_TRAIL_STOP", gross_pnl=375.0, stop_price=312.0, holding_seconds=7200)
    truth = {"T1_gross": 325.0, "T2_gross": -910.0, "T3_unrealized": -130.0, "M1_gross": 150.0, "M2_unrealized": 100.0,
             "M3_gross": 375.0, "snap": snap_ids}
    return S, truth
