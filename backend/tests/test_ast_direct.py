"""AST_FUTURES_V1_DIRECT: frozen 22/3.9 semantics, lifecycle, recovery, evidence and historical parity."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import random

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.adaptive_supertrend.bars import FutureBar
from app.adaptive_supertrend.capture import save_capture
from app.adaptive_supertrend.direct import CAPTURE_MODE, VERSION
from app.adaptive_supertrend.direct import engine as direct_engine
from app.adaptive_supertrend.direct.engine import ACTIVE, BarEval, Observation, evaluate, step
from app.adaptive_supertrend.direct.models import DirectCursor, DirectEvent, DirectTrade
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.direct.regression import compare_ledger, compare_summary, load_ledger, replay
from app.adaptive_supertrend.direct.report import report
from app.adaptive_supertrend.direct.service import JOURNAL, DirectPaperService
from app.adaptive_supertrend.indicators import supertrend
from app.adaptive_supertrend.research import load_bars
from app.db.session import build_session_factory
from app.scalper.repository import IST
from tests.test_scalper import snapshot

POLICY = DirectPolicy(lot_size=65)
DAY = date(2026, 11, 16)  # Monday
EXP = date(2026, 11, 26)


def t(hour, minute, second=0, day=DAY):
    return datetime.combine(day, time(hour, minute, second), IST)


def fbar(index, close, *, high=None, low=None, valid=True, fid="FUT", day=DAY):
    start = t(9, 15, day=day) + timedelta(minutes=5 * index)
    return FutureBar(
        start, start + timedelta(minutes=5), close, high if high is not None else close + 1,
        low if low is not None else close - 1, close, None, valid, None if valid else "PRICE_SEQUENCE_GAP", None,
        fid, EXP,
    )


def session(spec, *, or_valid=True):
    """Opening range high 25005 / low 24995, then (close, direction, transition) for each later bar."""
    opening = [fbar(0, 25000, high=25005), fbar(1, 25002, valid=or_valid), fbar(2, 24998, low=24995)]
    rows = [BarEval(bar, "BULL", 24900.0, 10.0, "T0") for bar in opening]
    for offset, (close, direction, transition) in enumerate(spec, 3):
        rows.append(BarEval(fbar(offset, close), direction, 24900.0, 10.0, transition))
    return rows


class Sim:
    def __init__(self, evals, policy=POLICY):
        self.evals, self.policy = evals, policy
        self.state: dict = {}
        self.active: dict | None = None
        self.finished: list[dict] = []
        self.sid = 0

    def tick(self, when, price=25010, *, kill=False, fid="FUT", expiry=EXP, lot=65, timing=True):
        self.sid += 1
        obs = Observation(self.sid, when, None if price is None else Decimal(str(price)), fid, expiry, lot, timing)
        result = step(self.policy, self.state, self.active, obs, [e for e in self.evals if e.bar.end <= when],
                      kill_switch=kill)
        self.active = None
        for doc in (result.active, result.created):
            if doc is not None:
                if doc["state"] in ACTIVE:
                    self.active = doc
                else:
                    self.finished.append(doc)
        return result


# 1-3: opening range


def test_opening_range_is_exactly_first_three_bars_and_frozen():
    sim = Sim(session([(25020, "BULL", "T0"), (25100, "BULL", "T0")]))
    sim.tick(t(9, 29, 59))
    assert sim.state["opening_range"] is None
    sim.tick(t(9, 30, 5))
    orb = sim.state["opening_range"]
    assert (orb["high"], orb["low"]) == (25005, 24995)
    assert [b["start"] for b in orb["source_bars"]] == [t(9, m).isoformat() for m in (15, 20, 25)]
    assert (orb["range_start"], orb["range_end"]) == (t(9, 15).isoformat(), t(9, 30).isoformat())
    sim.tick(t(9, 45, 5))
    assert sim.state["opening_range"] == orb  # Later, higher bars never widen the frozen range.


def test_opening_range_cannot_be_completed_from_later_bars():
    sim = Sim(session([(25020, "BULL", "T0")] * 10, or_valid=False))
    for minute in (30, 35, 40, 55):
        result = sim.tick(t(9, minute, 5))
    assert sim.state["opening_range"] is None
    assert "AST_OPENING_RANGE_UNAVAILABLE" in result.decision["blockers"]
    assert result.created is None and result.decision["status"] == "NO_SETUP"


def test_no_signal_before_opening_range_completion():
    rows = [BarEval(fbar(0, 25100, high=25100, low=24900), "BULL", 24800.0, 10.0, "T0")]
    sim = Sim(rows)
    result = sim.tick(t(9, 20, 5))
    assert result.created is None and "AST_OPENING_RANGE_UNAVAILABLE" in result.decision["blockers"]


# 4-7: entries


@pytest.mark.parametrize(
    ("close", "direction", "side", "blocker"),
    [
        (25020, "BULL", "LONG", None),
        (24980, "BEAR", "SHORT", None),
        (25020, "BEAR", None, "AST_ORB_DIRECTION_CONFLICT"),
        (24980, "BULL", None, "AST_ORB_DIRECTION_CONFLICT"),
        (25003, "BULL", None, "ORB_NOT_BROKEN"),
        (24997, "BEAR", None, "ORB_NOT_BROKEN"),
    ],
)
def test_entry_requires_breakout_and_agreeing_completed_ast(close, direction, side, blocker):
    sim = Sim(session([(close, direction, "T1")]))
    result = sim.tick(t(9, 35, 5), price=25011)
    if side is None:
        assert result.created is None and blocker in result.decision["blockers"]
        assert result.decision["status"] == "WATCH"
    else:
        trade = result.created
        assert trade["side"] == side and trade["state"] == "OPEN" and trade["entry_price"] == 25011
        assert trade["signal_price"] == close and trade["ast"]["direction"] == direction
        assert trade["atr_period"] == 22 and trade["multiplier"] == 3.9 and trade["quantity"] == 65
        assert [kind for kind, _ in result.events] == ["PENDING_PAPER_ENTRY", "ENTRY"]


def test_entry_never_fills_on_its_signal_bar():
    sim = Sim(session([(25020, "BULL", "T1")]))
    inside = sim.tick(t(9, 34, 59), price=25030)  # Signal bar is still forming.
    assert inside.created is None and sim.active is None
    result = sim.tick(t(9, 35, 0), price=25012)
    trade = result.created
    assert trade["signal_timestamp"] == t(9, 35).isoformat()
    assert datetime.fromisoformat(trade["entry_timestamp"]) >= datetime.fromisoformat(trade["signal_timestamp"])
    assert trade["entry_price"] == 25012 != trade["signal_price"]


# 8-9: exits


def open_position(spec):
    sim = Sim(session([(25020, "BULL", "T1"), *spec]))
    sim.tick(t(9, 35, 5), price=25012)
    assert sim.active["state"] == "OPEN"
    return sim


def test_ast_flip_never_exits_on_its_own_bar_and_uses_next_executable_observation():
    sim = open_position([(25030, "BULL", "T1"), (24900, "BEAR", "T2")])
    assert sim.tick(t(9, 44, 59), price=24950).decision["status"] == "OPEN"  # Flip bar 09:40-09:45 still forming.
    pending = sim.tick(t(9, 45, 5), price=None)
    assert sim.active["state"] == "PENDING_PAPER_EXIT"
    assert pending.decision["fill_blocker"] == "FUTURES_PRICE_UNAVAILABLE"
    assert sim.active["flip_bar"]["end"] == t(9, 45).isoformat() and sim.active["flip_direction"] == "BEAR"
    closed = sim.tick(t(9, 45, 20), price=24920)
    trade = sim.finished[-1]
    assert trade["exit_reason"] == "AST_FLIP" and trade["exit_category"] == "STRATEGY"
    assert trade["exit_price"] == 24920 and trade["gross_points"] == 24920 - 25012
    assert trade["gross_inr"] == (24920 - 25012) * 65 and trade["exit_timestamp"] == t(9, 45, 20).isoformat()
    assert [kind for kind, _ in closed.events] == ["AST_FLIP_EXIT"]


def bull_day(close=25020):
    return [(close, "BULL", "T1")] * 72  # 09:30 through 15:30


def test_forced_exit_at_1520_ist():
    sim = open_position(bull_day()[1:])
    assert sim.tick(t(15, 19, 55)).decision["status"] == "OPEN"
    result = sim.tick(t(15, 20, 0), price=25040)
    trade = sim.finished[-1]
    assert trade["exit_reason"] == "FORCED_EXIT" and trade["exit_category"] == "SESSION"
    assert trade["exit_timestamp"] == t(15, 20).isoformat() and trade["gross_points"] == 28
    assert [kind for kind, _ in result.events] == ["PENDING_PAPER_EXIT", "FORCED_EXIT"]


def test_unexecutable_forced_exit_becomes_unresolved_exposure_not_a_fill():
    sim = open_position(bull_day()[1:])
    sim.tick(t(15, 20, 0), price=None)
    assert sim.active["state"] == "PENDING_PAPER_EXIT"
    sim.tick(t(15, 21, 0), price=None)
    sim.tick(t(15, 22, 15), price=None)
    assert sim.active["state"] == "UNRESOLVED_EXPOSURE"
    assert sim.active["unresolved_reason"] == "FORCED_EXIT_UNEXECUTABLE:FUTURES_PRICE_UNAVAILABLE"
    assert sim.active.get("exit_price") is None and sim.active.get("gross_points") is None


# 10-14: session boundary, single position and opportunity identity


def test_no_entries_at_or_after_forced_exit_boundary():
    spec = [(25000, "BULL", "T1")] * 69 + [(25020, "BULL", "T1")]  # 15:15-15:20 bar breaks out.
    sim = Sim(session(spec))
    result = sim.tick(t(15, 20, 5))
    assert result.created is None and result.decision["status"] == "SIGNAL_BLOCKED"
    assert "SESSION_EXIT_BOUNDARY" in result.decision["blockers"]


def test_pending_entry_is_cancelled_at_session_exit():
    spec = [(25000, "BULL", "T1")] * 68 + [(25020, "BULL", "T1")]  # 15:10-15:15 signal bar.
    sim = Sim(session(spec), DirectPolicy(lot_size=65, entry_fill_window_seconds=240))
    sim.tick(t(15, 15, 5), price=None)
    assert sim.active["state"] == "PENDING_PAPER_ENTRY"
    sim.tick(t(15, 20, 0))
    assert sim.finished[-1]["rejection_reason"] == "CANCELLED_AT_SESSION_EXIT"


def test_only_one_active_position():
    sim = open_position([(25030, "BULL", "T1"), (25060, "BULL", "T9")])
    result = sim.tick(t(9, 45, 5))
    assert result.created is None and "MAX_OPEN_POSITIONS_REACHED" in result.decision["blockers"]
    assert result.decision["status"] == "OPEN"


def test_same_opportunity_cannot_refill_after_exit():
    sim = open_position([(25030, "BULL", "T1"), (25040, "BULL", "T1"), (25050, "BULL", "T1")])
    sim.tick(t(9, 40, 5), kill=True)  # Operational safety exit, then the switch is cleared.
    assert sim.finished[-1]["exit_reason"] == "SAFETY_EXIT_KILL_SWITCH"
    assert sim.finished[-1]["exit_category"] == "SAFETY"
    result = sim.tick(t(9, 45, 5))
    assert result.created is None and "AST_OPPORTUNITY_CONSUMED" in result.decision["blockers"]


def test_genuine_later_transition_forms_new_opportunity_after_close():
    sim = open_position([(24980, "BEAR", "T2"), (24970, "BEAR", "T2"), (25030, "BULL", "T3")])
    flip = sim.tick(t(9, 40, 5), price=24985)
    # The flip bar itself cannot also open the reversal; one position at a time.
    assert sim.finished[-1]["exit_reason"] == "AST_FLIP" and flip.created is None
    assert "MAX_OPEN_POSITIONS_REACHED" in flip.decision["blockers"]
    short = sim.tick(t(9, 45, 5), price=24975).created
    assert short["side"] == "SHORT" and short["ast"]["transition"] == "T2"
    sim.tick(t(9, 50, 5), price=25025)
    assert sim.finished[-1]["exit_reason"] == "AST_FLIP"
    long = sim.tick(t(9, 55, 5), price=25035)
    assert long.created is None or long.created["side"] == "LONG"
    ids = {d["opportunity_id"] for d in sim.finished}
    assert len(ids) == len(sim.finished) == 2


# 16-17: data and contract failures


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"price": None}, "FUTURES_PRICE_UNAVAILABLE"),
        ({"lot": 75}, "FUTURES_LOT_SIZE_MISMATCH"),
        ({"lot": None}, "FUTURES_LOT_SIZE_MISMATCH"),
        ({"timing": False}, "OBSERVATION_TIMING_INVALID"),
        ({"fid": ""}, "FUTURES_IDENTITY_UNAVAILABLE_OR_EXPIRED"),
    ],
)
def test_invalid_or_missing_market_data_blocks_entry(fields, reason):
    sim = Sim(session([(25020, "BULL", "T1")]))
    first = sim.tick(t(9, 35, 5), **fields)
    assert first.decision["fill_blocker"] == reason and sim.active["state"] == "PENDING_PAPER_ENTRY"
    sim.tick(t(9, 35, 30), **fields)
    sim.tick(t(9, 36, 15), **fields)
    rejected = sim.finished[-1]
    assert rejected["state"] == "ENTRY_REJECTED"
    assert rejected["rejection_reason"] == f"ENTRY_FILL_WINDOW_EXPIRED:{reason}"
    assert rejected.get("entry_price") is None and sim.state["consumed"] == []


def test_contract_change_rejects_entry_and_held_contract_loss_is_unresolved():
    sim = Sim(session([(25020, "BULL", "T1")]))
    sim.tick(t(9, 35, 5), price=None)
    sim.tick(t(9, 35, 20), fid="FUT-NEXT")
    assert sim.finished[-1]["rejection_reason"] == "FUTURES_CONTRACT_CHANGED"

    rolled = open_position([(25030, "BULL", "T1")])
    rolled.tick(t(9, 36, 0), fid="FUT-NEXT")
    assert rolled.active["unresolved_reason"] == "HELD_CONTRACT_UNAVAILABLE"

    held = open_position([(24900, "BEAR", "T2"), (25100, "BULL", "T5")])
    held.tick(t(9, 40, 5), fid="FUT-NEXT", price=24910)
    assert held.active["state"] == "UNRESOLVED_EXPOSURE"
    assert held.active["unresolved_reason"] == "HELD_CONTRACT_UNAVAILABLE"
    assert held.active.get("exit_price") is None
    blocked = held.tick(t(9, 45, 5))
    assert blocked.created is None and "MAX_OPEN_POSITIONS_REACHED" in blocked.decision["blockers"]


def test_missed_session_exit_surfaces_unresolved_on_next_day():
    sim = open_position([(25030, "BULL", "T1")])
    sim.tick(t(9, 15, 5, day=DAY + timedelta(days=1)))
    assert sim.active["unresolved_reason"] == "MISSED_SESSION_EXIT"


# 18-19: frozen parameters and no optimizer


@pytest.mark.parametrize(
    "override",
    [
        {"atr_period": 7},
        {"multiplier_tenths": 11},
        {"optimizer": {"lookback_sessions": 15}},
        {"regime": "ADX_ER"},
        {"session_exit_time_ist": "15:25:00"},
        {"lots": 2},
        {"max_open_positions": 2},
        {"fill_basis": "BID_ASK"},
        {"lot_size": 0},
        {"lot_size": True},
    ],
)
def test_fixed_22_39_policy_cannot_change(override):
    with pytest.raises(ValidationError):
        DirectPolicy.model_validate({"lot_size": 65, **override})


def test_policy_is_immutable_and_evaluate_uses_22_39(monkeypatch):
    with pytest.raises(ValidationError):
        POLICY.atr_period = 7  # type: ignore[misc]
    bars = [fbar(i % 75, 25000 + (i % 13) * 3 - (i % 7) * 4, day=DAY - timedelta(days=i // 75)) for i in range(150)]
    bars.sort(key=lambda bar: bar.start)
    calls = []
    original = direct_engine.supertrend

    def audit(rows, period, tenths, atr_values=None):
        calls.append((period, tenths))
        return original(rows, period, tenths, atr_values)

    monkeypatch.setattr(direct_engine, "supertrend", audit)
    rows = evaluate(bars)
    assert calls and set(calls) == {(22, 39)}
    assert [r.direction for r in rows] == [r["direction"] for r in supertrend(bars, 22, 39)]


def test_runtime_optimizer_is_never_imported_or_invoked(monkeypatch):
    import app.adaptive_supertrend.optimizer as optimizer
    import app.adaptive_supertrend.service as legacy

    def forbidden(*args, **kwargs):
        raise AssertionError("optimizer invoked")

    for name in ("optimize", "evaluate_grid", "walk_forward", "selected_parameters", "signal_trades"):
        monkeypatch.setattr(optimizer, name, forbidden)
    monkeypatch.setattr(legacy, "freeze", forbidden)
    replay(random_days(5, seed=3), POLICY)
    package = Path(direct_engine.__file__).parent
    worker = Path(__file__).resolve().parents[2] / "worker" / "ast_direct_worker.py"
    for path in [*package.glob("*.py"), worker]:
        source = path.read_text()
        for word in ("optimizer", "freeze", "option_chain_selection", "greeks", "SessionParameters"):
            assert f"import {word}" not in source and f".{word} import" not in source, (path, word)
        assert "adaptive_supertrend.service" not in source and "adaptive_supertrend.decision" not in source


# 15 and evidence: service recovery


def quote(when, price, *, fid="FUT"):
    template = snapshot(0)
    return template.model_copy(
        update={
            "captured_at": when,
            "request_started_at": when - timedelta(seconds=1),
            "response_received_at": when,
            "source_market_timestamp": when,
            "nifty_spot": price,
            "nifty_future": price,
            "future_instrument_id": fid,
            "lot_size": 65,
            "future_expiry": EXP,
        }
    ).model_dump(exclude={"quotes"})


def bulk(sessions, rows, mode, producer):
    from app.adaptive_supertrend.capture import capture_bucket
    from app.research.manifest import canonical, digest
    from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot

    with sessions.begin() as db:
        records = []
        for fields in rows:
            bucket = capture_bucket(fields["captured_at"]).isoformat()
            records.append(Snapshot(**fields, capture_key=digest({"mode": mode, "bucket": bucket}), feature_json="{}",
                                    signal_json="{}", context_json=canonical(
                                        {"mode": mode, "producer": producer, "bucket": bucket, "status": "CAPTURED"})))
        db.add_all(records)
        db.flush()
        return [record.id for record in records]


def tape(engine, other_from=None):
    """Prior shared AST session for warm-up, then today's native direct capture: breakout, crash, recovery."""
    sessions = build_session_factory(engine)
    with sessions.begin() as db:
        db.add(DirectCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0, state_json="{}", latest_json="{}"))
    prior = DAY - timedelta(days=3)
    bulk(sessions, [quote(t(9, 15, day=prior) + timedelta(seconds=15 * i), 24700 + i * 0.15 + (i % 3))
                    for i in range(1500)], "AST_PAPER", "AST_FUTURES_V1")
    rows = []
    for i in range(300):  # 09:15:00 .. 10:29:45
        if i < 60:
            price = 25000 + (i % 5) - 2  # Opening range 24998-25002.
        elif i < 80:
            price = 25003 + (i - 59) * 1.5  # 09:30-09:35 closes above the range.
        elif i < 180:
            price = 25035 + (i - 80) * 0.1
        elif i < 200:
            price = 25045 - (i - 179) * 20  # 10:00-10:05 crash completes the AST flip.
        else:
            price = 25000 + (i % 3) - 1  # Back inside the range: no new breakout.
        fid = "OTHER" if other_from is not None and i >= other_from else "FUT"
        rows.append(quote(t(9, 15) + timedelta(seconds=15 * i), price, fid=fid))
    return sessions, bulk(sessions, rows, CAPTURE_MODE, VERSION)


def test_service_restart_restores_state_without_duplicate_entries(engine):
    sessions, ids = tape(engine)
    first = DirectPaperService(sessions, POLICY)
    assert first.pending_snapshot_ids()[:1] == ids[:1]
    with pytest.raises(RuntimeError, match="EARLIER_OBSERVATION_PENDING"):
        first.process_snapshot(ids[5])
    for snapshot_id in ids[:85]:
        decision = first.process_snapshot(snapshot_id)
    assert decision["status"] == "OPEN" and decision["position"]["side"] == "LONG"
    assert first.process_snapshot(ids[84])["status"] == "DUPLICATE"
    # Intraday restart: a new process reconstructs the authoritative state from persisted evidence.
    second = DirectPaperService(sessions, POLICY)
    assert second.pending_snapshot_ids()[0] == ids[85]
    statuses = [second.process_snapshot(snapshot_id)["status"] for snapshot_id in second.pending_snapshot_ids(500)]
    assert "OPEN" in statuses[:2]
    with sessions() as db:
        trades = [json.loads(row.document) for row in db.scalars(select(DirectTrade))]
        kinds = list(db.scalars(select(DirectEvent.event_type).where(DirectEvent.trade_id.is_not(None))
                                .order_by(DirectEvent.sequence)))
        observations = db.scalar(select(func.count()).select_from(DirectEvent)
                                 .where(DirectEvent.event_type == "OBSERVATION"))
    assert len(trades) == 1 and kinds == ["PENDING_PAPER_ENTRY", "ENTRY", "PENDING_PAPER_EXIT", "AST_FLIP_EXIT"]
    assert observations == len(ids)
    trade = trades[0]
    assert trade["state"] == "CLOSED" and trade["exit_reason"] == "AST_FLIP"
    assert trade["signal_timestamp"] == t(9, 35).isoformat() and trade["entry_timestamp"] == t(9, 35).isoformat()
    assert trade["fill_basis"] == "FUTURES_LTP_PAPER_V1" and trade["policy_hash"] == POLICY.policy_hash
    for key in ("opening_range", "ast", "signal_bar", "opportunity", "decision_timestamp", "lot_size", "side"):
        assert trade[key] is not None
    assert len(trade["opening_range"]["source_bars"]) == 3
    JOURNAL.verify(sessions, full=True)
    status = report(sessions, t(10, 30), POLICY)
    assert status["mode"] == "PAPER" and status["live_trading"] is False and status["position"] is None
    assert status["today_gross_points"] == trade["gross_points"] == status["cumulative_gross_points"]
    assert status["trades_today"][0]["trade_id"] == trade["trade_id"]
    assert status["journal_head"] and status["fixed_parameters"]["atr_period"] == 22
    assert status["opening_range"]["high"] == trade["opening_range"]["high"]
    assert status["worker"] == "STOPPED" and status["forced_exit_status"] == "SCHEDULED_15:20_IST"


def test_service_refuses_foreign_capture_and_policy_change_with_active_trade(engine):
    sessions, ids = tape(engine)
    service = DirectPaperService(sessions, POLICY)
    foreign = save_capture(sessions, quote(t(10, 31), 25000))  # Shared AST_PAPER stream.
    with pytest.raises(ValueError, match="FOREIGN_CAPTURE"):
        service.process_snapshot(foreign)
    for snapshot_id in ids[:85]:
        service.process_snapshot(snapshot_id)
    with pytest.raises(RuntimeError, match="POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE"):
        DirectPaperService(sessions, DirectPolicy(lot_size=65, friction_points_per_round_trip=2))


def test_contract_roll_while_open_is_unresolved_and_acknowledgement_invents_no_pnl(engine):
    sessions, ids = tape(engine, other_from=85)
    service = DirectPaperService(sessions, POLICY)
    for snapshot_id in ids[:85]:
        service.process_snapshot(snapshot_id)
    decision = service.process_snapshot(ids[85])
    assert decision["status"] == "UNRESOLVED_EXPOSURE"
    with sessions() as db:
        trade_id = db.scalar(select(DirectTrade.id))
    assert report(sessions, t(9, 37), POLICY)["position"]["unresolved_reason"] == "HELD_CONTRACT_UNAVAILABLE"
    with pytest.raises(ValueError, match="NOTE_REQUIRED"):
        service.acknowledge_unresolved(trade_id, " ")
    doc = service.acknowledge_unresolved(trade_id, "paper exposure reviewed")
    assert doc["state"] == "UNRESOLVED_ACKNOWLEDGED" and doc["gross_points"] is None and doc.get("exit_price") is None
    with pytest.raises(ValueError, match="UNRESOLVED_TRADE_REQUIRED"):
        service.acknowledge_unresolved(trade_id, "again")
    JOURNAL.verify(sessions, full=True)
    assert service.process_snapshot(ids[86])["position"] is None


# 20: historical regression


def random_days(days, seed, drift=0.0):
    rnd = random.Random(seed)
    price, rows, day, count = 25000.0, [], date(2025, 1, 6), 0
    while count < days:
        if day.weekday() < 5:
            trend = rnd.choice((-1, 0, 1)) * drift
            for i in range(75):
                start = t(9, 15, day=day) + timedelta(minutes=5 * i)
                open_ = price
                close = open_ + rnd.gauss(trend, 9)
                high, low = max(open_, close) + abs(rnd.gauss(0, 4)), min(open_, close) - abs(rnd.gauss(0, 4))
                rows.append(FutureBar(start, start + timedelta(minutes=5), round(open_, 2), round(high, 2),
                                      round(low, 2), round(close, 2), None, True, None, None, "FUT", date(2030, 1, 30)))
                price = close
            count += 1
        day += timedelta(days=1)
    return rows


def test_engine_matches_research_signal_semantics_before_1520(tmp_path):
    from app.adaptive_supertrend.optimizer import _segments, signal_trades

    bars = random_days(250, seed=11)
    ours = replay(bars, POLICY)["trades"]
    research = []
    for segment in _segments(bars):
        research.extend(signal_trades(segment, supertrend(segment, 22, 39), [True] * len(segment)))
    by_day_ours, by_day_research = defaultdict(list), defaultdict(list)
    for trade in ours:
        by_day_ours[trade["trading_date"]].append(trade)
    for trade in research:
        by_day_research[trade["date"]].append(trade)
    compared = flips = 0
    for day in sorted(set(by_day_ours) | set(by_day_research)):
        theirs = by_day_research[day]
        # Research marks session end at the 15:30 close; the frozen paper rule exits at 15:20.
        if any(datetime.fromisoformat(x["exit_at"]).time() >= time(15, 20)
               or datetime.fromisoformat(x["entry_at"]).time() >= time(15, 20) for x in theirs):
            continue
        mine = by_day_ours[day]
        assert [(x["entry_timestamp"], x["exit_timestamp"], x["side"]) for x in mine] == [
            (x["entry_at"], x["exit_at"], "LONG" if x["sign"] == 1 else "SHORT") for x in theirs
        ], day
        assert [x["gross_points"] for x in mine] == pytest.approx([x["signal_points"] for x in theirs])
        compared += 1
        flips += len(mine)
    assert compared >= 50 and flips >= 70  # Deterministic seed: 57 sessions, 76 trades compared exactly.


def test_regression_csv_runner_and_comparators(tmp_path):
    bars = random_days(3, seed=5, drift=3.0)
    path = tmp_path / "bars.csv"
    with path.open("w") as stream:
        stream.write("start,end,open,high,low,close,future_instrument_id,future_expiry\n")
        for bar in bars:
            stream.write(f"{bar.start.isoformat()},{bar.end.isoformat()},{bar.open},{bar.high},{bar.low},"
                         f"{bar.close},FUT,2030-01-30\n")
    result = replay(load_bars(path), POLICY)
    assert result == replay(load_bars(path), POLICY)  # Deterministic.
    summary = result["summary"]
    assert summary["net_points_at_2pt"] == pytest.approx(summary["gross_points"] - 2 * summary["trades"])
    assert compare_summary(summary)["status"] == "MISMATCH"  # Synthetic data is not the frozen decade.
    exact = {**summary, "trades": 3461, "gross_points": 18031.89, "average_points": 5.21,
             "max_drawdown_points": 1549.2, "net_points_at_2pt": 11109.89, "net_max_drawdown_points_at_2pt": 1701.2}
    assert compare_summary(exact)["status"] == "MATCH"
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps([{"entry_at": x["entry_timestamp"], "exit_at": x["exit_timestamp"],
                                   "signal_points": x["gross_points"]} for x in result["trades"]]))
    assert compare_ledger(result["trades"], load_ledger(ledger))["status"] == "MATCH"
    assert compare_ledger(result["trades"][1:], load_ledger(ledger))["status"] == "MISMATCH"


def test_frozen_decade_historical_regression():
    csv_path = os.getenv("AST_DIRECT_FROZEN_CSV")
    if not csv_path or not Path(csv_path).exists():
        pytest.skip("AST_DIRECT_FROZEN_CSV must name the frozen decade 5-minute NIFTY futures CSV")
    result = replay(load_bars(Path(csv_path)), DirectPolicy(lot_size=int(os.getenv("AST_DIRECT_LOT_SIZE", "65"))))
    comparison = compare_summary(result["summary"])
    ledger = os.getenv("AST_DIRECT_FROZEN_LEDGER")
    if ledger:
        assert compare_ledger(result["trades"], load_ledger(Path(ledger)))["status"] == "MATCH"
    assert comparison["status"] == "MATCH", comparison


# Worker: disabled by default, PAPER only, read-only status, operational kill switch, quiet logs.


def test_worker_controls(tmp_path, monkeypatch, engine, capsys):
    from worker import ast_direct_worker as worker
    from tests.test_scalper import settings

    monkeypatch.setattr(worker, "get_settings", lambda: settings(ast_direct_enabled=False))
    assert worker.main(["--mode", "PAPER"]) == 0  # Disabled: no database or broker access.
    with pytest.raises(SystemExit):
        worker.main(["--mode", "LIVE"])
    beat = tmp_path / "beat.json"
    configured = settings(ast_direct_heartbeat_file=str(beat), ast_direct_kill_switch_file=str(tmp_path / "KILL"))
    assert worker.heartbeat_path(configured) == beat
    monkeypatch.setattr(worker, "get_settings", lambda: configured)
    assert worker.main(["--mode", "PAPER", "--health"]) == 1
    worker.write_heartbeat(beat, status="WATCH")
    assert worker.main(["--mode", "PAPER", "--health"]) == 0
    assert worker.kill_switch(configured) is False
    (tmp_path / "KILL").touch()
    assert worker.kill_switch(configured) is True and worker.kill_switch(settings(ast_direct_kill_switch=True))
    assert worker.seconds(10) == "10,25,40,55"
    with pytest.raises(RuntimeError, match="ORDER_CAPABILITY"):
        worker.paper_only_builder(object())

    messages = []
    monkeypatch.setattr(worker.logger, "info", lambda *args: messages.append(args))
    log = worker.BlockerLog()
    for _ in range(3):
        log({"status": "NO_SETUP", "blockers": ["AST_WARMUP_REQUIRED"], "timestamp": "x"})
    log({"status": "WATCH", "blockers": ["ORB_NOT_BROKEN"], "timestamp": "y"})
    assert len(messages) == 2

    url = f"sqlite+pysqlite:///{tmp_path / 'status.db'}"
    from sqlalchemy import create_engine
    from app.db.base import Base
    status_engine = create_engine(url)
    Base.metadata.create_all(status_engine)
    with build_session_factory(status_engine).begin() as db:
        db.add(DirectCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0, state_json="{}", latest_json="{}"))
    status_engine.dispose()
    readonly = settings(database_url=url, ast_direct_config_json=POLICY.model_dump_json(),
                        ast_direct_heartbeat_file=str(beat))
    monkeypatch.setattr(worker, "get_settings", lambda: readonly)
    assert worker.main(["--mode", "PAPER", "--status", "--verify-journal"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "PAPER" and printed["worker"] == "RUNNING" and printed["journal_head"] == "GENESIS"
    assert printed["strategy_id"] == "AST_FUTURES_V1_DIRECT" and printed["worker_enabled"] is False
