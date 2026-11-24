"""ACTS_V3_MCX_FUTURES paper execution, costs and engine tests (SYNTHETIC data only)."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json

import pytest

from app.mcx_futures import paper
from app.mcx_futures.bars import Observation
from app.mcx_futures.config import CRUDEOILM, NATGASMINI
from app.mcx_futures.costs import DEFAULT_SCHEDULE, round_trip
from app.mcx_futures.engine import EVENT_TYPES, GENESIS, McxFuturesEngine
from tests.test_mcx_futures_support import observations_from_bars, spec_for, trend_bars

CRUDE_SPEC = spec_for("CRUDEOILM")
GAS_SPEC = spec_for("NATGASMINI")


# -- hand-computed reference examples ---------------------------------------------------------------------------
def _position(direction: str, price: float, stop: float) -> dict:
    return {"direction": direction, "sign": 1 if direction == "LONG" else -1, "lots": 1,
            "entry_at": "2026-11-24T12:00:15+05:30", "entry_price": price, "stop": stop, "initial_stop": stop,
            "best_price": price, "mae_points": 0.0, "mfe_points": 0.0, "trail_active": False, "entry_reason": "TEST",
            "cycle_id": "c", "score": 70.0, "entry_atr": 6.0, "stop_points": abs(price - stop)}


def test_crudeoilm_long_reference_pnl_and_costs() -> None:
    from app.mcx_futures.spec import IST
    from datetime import datetime

    trade = paper.close(_position("LONG", 5600.0, 5590.0), datetime(2026, 11, 24, 12, 30, tzinfo=IST), 5615.0,
                        "TEST", DEFAULT_SCHEDULE, CRUDE_SPEC)
    assert trade["gross_pnl"] == 150.0  # (5615 - 5600) x 10 x 1
    costs = trade["costs"]
    assert (costs["buy_value"], costs["sell_value"], costs["turnover"]) == (56000.0, 56150.0, 112150.0)
    assert costs["items"] == {"ctt": 5.615, "stamp_duty": 1.12, "sebi_fee": 0.11215, "exchange_txn": 2.35515,
                              "brokerage": 40.0, "gst": 7.644114}
    assert costs["total"] == 56.85
    assert trade["net_pnl"] == pytest.approx(93.15)
    assert costs["statuses"]["exchange_txn"].startswith("UNVERIFIED_ASSUMPTION")
    assert costs["statuses"]["ctt"] == "STATUTORY" and costs["statuses"]["brokerage"] == "ASSUMPTION"


def test_natgasmini_short_reference_pnl_and_costs() -> None:
    from app.mcx_futures.spec import IST
    from datetime import datetime

    trade = paper.close(_position("SHORT", 310.0, 312.0), datetime(2026, 11, 24, 12, 30, tzinfo=IST), 308.5,
                        "TEST", DEFAULT_SCHEDULE, GAS_SPEC)
    assert trade["gross_pnl"] == 375.0  # (308.5 - 310.0) x -1 x 250
    costs = trade["costs"]
    assert (costs["sell_value"], costs["buy_value"]) == (77500.0, 77125.0)  # short: entry is the sell
    assert costs["items"] == {"ctt": 7.75, "stamp_duty": 1.5425, "sebi_fee": 0.154625, "exchange_txn": 3.247125,
                              "brokerage": 40.0, "gst": 7.812315}
    assert costs["total"] == 60.51
    assert trade["net_pnl"] == pytest.approx(314.49)


def test_brokerage_scenarios() -> None:
    zero = round_trip(DEFAULT_SCHEDULE.with_brokerage(0), 5600, 5615, "LONG", 10, 1)
    ten = round_trip(DEFAULT_SCHEDULE.with_brokerage(10), 5600, 5615, "LONG", 10, 1)
    assert zero["total"] == 9.65 and zero["items"]["gst"] == pytest.approx(0.444114)
    assert ten["items"]["brokerage"] == 20.0 and ten["total"] == pytest.approx(33.25)


def test_stop_gap_through_fills_at_worse_price_plus_slippage() -> None:
    from app.mcx_futures.spec import IST
    from datetime import datetime

    at = datetime(2026, 11, 24, 12, 1, tzinfo=IST)
    long = _position("LONG", 5600.0, 5590.0)
    touch = Observation(at=at, ltp=5591, bid=5590, ask=5592, source_ts=at)
    assert paper.stop_fill(long, touch, CRUDEOILM, CRUDE_SPEC) == 5589.0  # stop - 1 tick slippage
    gap = Observation(at=at, ltp=5581, bid=5580, ask=5582, source_ts=at)
    assert paper.stop_fill(long, gap, CRUDEOILM, CRUDE_SPEC) == 5579.0  # bid (worse) - 1 tick
    short = _position("SHORT", 310.0, 312.0)
    gap_up = Observation(at=at, ltp=313.0, bid=312.9, ask=313.1, source_ts=at)
    assert paper.stop_fill(short, gap_up, NATGASMINI, GAS_SPEC) == pytest.approx(313.2)
    safe = Observation(at=at, ltp=311.0, bid=310.9, ask=311.1, source_ts=at)
    assert paper.stop_fill(short, safe, NATGASMINI, GAS_SPEC) is None


# -- engine -----------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def crude_stream() -> list[Observation]:
    bars = trend_bars(date(2026, 11, 24), start_price=5600, slope=0.6, tick=1.0, minutes=600, wiggle=2.0)
    return observations_from_bars(bars, 1.0)


@pytest.fixture(scope="module")
def gas_stream() -> list[Observation]:
    bars = trend_bars(date(2026, 11, 24), start_price=310, slope=0.03, tick=0.1, minutes=600, wiggle=0.12)
    return observations_from_bars(bars, 0.1)


def run(engine: McxFuturesEngine, stream: list[Observation]) -> list[dict]:
    return [event for item in stream for event in engine.on_observation(item)]


def crude_engine() -> McxFuturesEngine:
    return McxFuturesEngine(CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE)


def test_entry_fills_at_next_observation_at_ask(crude_stream: list[Observation]) -> None:
    events = run(crude_engine(), crude_stream)
    pending = [e for e in events if e["type"] == "ENTRY_PENDING"]
    filled = [e for e in events if e["type"] == "ENTRY_FILLED"]
    assert pending and len(pending) == len(filled)
    times = [o.at for o in crude_stream]
    for p, f in zip(pending, filled):
        decided = times.index(next(o.at for o in crude_stream if o.at.isoformat() == p["at"]))
        assert f["at"] == times[decided + 1].isoformat()  # the NEXT observation, never the deciding one
        assert f["payload"]["price"] == f["payload"]["ask"]  # LONG pays the ask
        decision = [e for e in events if e["type"] == "DECISION" and e["at"] == p["at"]
                    and e["payload"].get("status") == "ENTER"]
        assert decision and decision[0]["payload"]["score"] >= CRUDEOILM.entry_score_floor
    assert {e["type"] for e in events} <= set(EVENT_TYPES)


def test_events_are_json_safe_and_hash_chained(crude_stream: list[Observation]) -> None:
    events = run(crude_engine(), crude_stream[:1200])
    previous = GENESIS
    for event in events:
        assert event["prev_hash"] == previous
        body = {key: value for key, value in event.items() if key != "hash"}
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert hashlib.sha256(blob.encode()).hexdigest() == event["hash"]
        previous = event["hash"]
    assert json.loads(json.dumps(events)) == events


def test_restore_then_replay_is_identical(crude_stream: list[Observation]) -> None:
    reference = run(crude_engine(), crude_stream)
    for cut in (649, 700, 1500, 1810):
        first = crude_engine()
        head = run(first, crude_stream[:cut])
        state = json.loads(json.dumps(first.snapshot()))
        restored = McxFuturesEngine.restore(CRUDEOILM, CRUDE_SPEC, DEFAULT_SCHEDULE, state)
        tail = run(restored, crude_stream[cut:])
        assert head + tail == reference
    with pytest.raises(ValueError, match="SNAPSHOT_IDENTITY_MISMATCH"):
        McxFuturesEngine.restore(CRUDEOILM.with_overrides(entry_score_floor=65.0), CRUDE_SPEC, DEFAULT_SCHEDULE,
                                 state)


def test_duplicate_observations_are_idempotent(crude_stream: list[Observation]) -> None:
    engine = crude_engine()
    run(engine, crude_stream[:1500])
    before = engine.snapshot()
    replayed = run(engine, crude_stream[1400:1500])
    assert {e["type"] for e in replayed} == {"OBSERVATION_REJECTED"}
    assert {e["payload"]["reason"] for e in replayed} <= {"DUPLICATE_OBSERVATION", "OUT_OF_ORDER_OBSERVATION"}
    after = engine.snapshot()
    for key in ("position", "cycle", "risk", "pending_entry", "bars", "builder"):
        assert after[key] == before[key]


def test_instruments_are_independent(crude_stream: list[Observation], gas_stream: list[Observation]) -> None:
    alone_crude = run(crude_engine(), crude_stream)
    alone_gas = run(McxFuturesEngine(NATGASMINI, GAS_SPEC, DEFAULT_SCHEDULE), gas_stream)
    crude, gas = crude_engine(), McxFuturesEngine(NATGASMINI, GAS_SPEC, DEFAULT_SCHEDULE)
    mixed_crude, mixed_gas = [], []
    for a, b in zip(crude_stream, gas_stream):
        mixed_gas.extend(gas.on_observation(b))
        mixed_crude.extend(crude.on_observation(a))
    assert mixed_crude == alone_crude and mixed_gas == alone_gas
    with pytest.raises(ValueError):
        McxFuturesEngine(NATGASMINI, CRUDE_SPEC, DEFAULT_SCHEDULE)


def _engine_at(stream: list[Observation], kind: str) -> tuple[McxFuturesEngine, Observation]:
    engine = crude_engine()
    for item in stream:
        events = engine.on_observation(item)
        if any(e["type"] == kind for e in events):
            return engine, item
    raise AssertionError(f"no {kind}")


def test_unfilled_entry_is_rejected_with_reason_and_not_consumed(crude_stream: list[Observation]) -> None:
    engine, decided = _engine_at(crude_stream, "ENTRY_PENDING")
    at = decided.at + timedelta(seconds=15)
    wide = Observation(at=at, ltp=decided.ltp, bid=decided.ltp - 3, ask=decided.ltp + 3,
                       bid_qty=1000, ask_qty=1000, source_ts=at)
    events = engine.on_observation(wide)
    rejected = [e for e in events if e["type"] == "ENTRY_REJECTED"]
    assert rejected and rejected[0]["payload"]["reason"] == "SPREAD_TOO_WIDE"
    assert rejected[0]["payload"]["consumed"] is False
    cycle_events = [e for e in events if e["type"] == "CYCLE"]
    assert cycle_events[0]["payload"]["event"] == "RESERVATION_RELEASED"
    assert engine.s["cycle"]["consumed_directions"] == [] and engine.s["cycle"]["armed_direction"] == "LONG"
    assert engine.position is None
    stale_engine, decided = _engine_at(crude_stream, "ENTRY_PENDING")
    stale = Observation(at=at, ltp=decided.ltp, bid=decided.ltp - 1, ask=decided.ltp + 1,
                        bid_qty=1000, ask_qty=1000, source_ts=at - timedelta(seconds=90))
    reasons = [e["payload"]["reason"] for e in stale_engine.on_observation(stale) if e["type"] == "ENTRY_REJECTED"]
    assert reasons == ["STALE_QUOTE"]
    late_engine, decided = _engine_at(crude_stream, "ENTRY_PENDING")
    late = decided.at + timedelta(seconds=31)
    reasons = [e["payload"]["reason"] for e in late_engine.on_observation(
        Observation(at=late, ltp=decided.ltp, bid=decided.ltp - 1, ask=decided.ltp + 1,
                    bid_qty=1000, ask_qty=1000, source_ts=late))
        if e["type"] == "ENTRY_REJECTED"]
    assert reasons == ["ENTRY_LATENCY_EXCEEDED"]


def test_collect_only_releases_pending_entry_without_a_fill(crude_stream: list[Observation]) -> None:
    engine, decided = _engine_at(crude_stream, "ENTRY_PENDING")
    at = decided.at + timedelta(seconds=15)
    quote = Observation(at=at, ltp=decided.ltp, bid=decided.ltp - 1, ask=decided.ltp + 1,
                        bid_qty=1000, ask_qty=1000, source_ts=at)
    events = engine.on_observation(quote, allow_entries=False)
    assert [e["payload"]["reason"] for e in events if e["type"] == "ENTRY_REJECTED"] == [
        "PAPER_EXECUTION_DISABLED"]
    assert not any(e["type"] == "ENTRY_FILLED" for e in events)
    assert engine.position is None
    assert engine.s["cycle"]["consumed_directions"] == []


def test_executable_depth_and_future_quote_time_are_required() -> None:
    from app.mcx_futures.spec import IST
    from datetime import datetime

    at = datetime(2026, 11, 24, 12, 0, 15, tzinfo=IST)
    pending = {"direction": "LONG", "decided_at": (at - timedelta(seconds=15)).isoformat()}
    base = dict(at=at, ltp=5600.0, bid=5599.0, ask=5601.0, source_ts=at)
    assert paper.entry_fill(pending, Observation(**base), CRUDEOILM, CRUDE_SPEC, [])[1] == \
        "ASK_DEPTH_UNAVAILABLE"
    assert paper.entry_fill(pending, Observation(**base, bid_qty=10, ask_qty=9), CRUDEOILM,
                            CRUDE_SPEC, [])[1] == "ASK_DEPTH_INSUFFICIENT"
    assert paper.entry_fill(pending, Observation(**base, bid_qty=10, ask_qty=10), CRUDEOILM,
                            CRUDE_SPEC, [])[0] is not None
    future = Observation(at=at, ltp=5600, bid=5599, ask=5601, bid_qty=10, ask_qty=10,
                         source_ts=at + timedelta(seconds=6))
    assert paper.entry_fill(pending, future, CRUDEOILM, CRUDE_SPEC, [])[1] == "FUTURE_QUOTE_TIMESTAMP"


def test_missing_exit_depth_keeps_position_unpriced(crude_stream: list[Observation]) -> None:
    engine, filled = _engine_at(crude_stream, "ENTRY_FILLED")
    position = engine.position
    assert position is not None
    at = filled.at + timedelta(seconds=15)
    quote = Observation(at=at, ltp=filled.ltp, bid=filled.ltp - 1, ask=filled.ltp + 1,
                        source_ts=at, bid_qty=None, ask_qty=None)
    events = engine.on_observation(quote)
    assert any(e["type"] == "MARK_UNAVAILABLE" for e in events)
    assert not any(e["type"] in ("MARK", "EXIT") for e in events)
    assert engine.position is not None


def test_stale_feed_never_marks_and_exits_only_on_fresh_quote(crude_stream: list[Observation]) -> None:
    engine, filled = _engine_at(crude_stream, "ENTRY_FILLED")
    stop = engine.position["stop"]
    price = filled.ltp
    t = filled.at
    for k in range(1, 9):  # 2 minutes of stale quotes (exchange timestamp frozen)
        at = t + timedelta(seconds=15 * k)
        events = engine.on_observation(Observation(at=at, ltp=price, bid=price - 1, ask=price + 1,
                                                   bid_qty=1000, ask_qty=1000,
                                                   source_ts=filled.at - timedelta(seconds=60)))
        kinds = [e["type"] for e in events]
        assert "MARK" not in kinds and "EXIT" not in kinds and "MARK_UNAVAILABLE" in kinds
    fresh_at = t + timedelta(seconds=15 * 9)
    events = engine.on_observation(Observation(at=fresh_at, ltp=price, bid=price - 1, ask=price + 1,
                                               bid_qty=1000, ask_qty=1000, source_ts=fresh_at))
    exits = [e for e in events if e["type"] == "EXIT"]
    assert exits and exits[0]["payload"]["trade"]["exit_reason"] == "STALE_FEED_EXIT"
    assert exits[0]["payload"]["trade"]["exit_price"] == price - 1 and price - 1 > stop


def test_engine_stop_gap_through_and_forced_exit(crude_stream: list[Observation]) -> None:
    engine, filled = _engine_at(crude_stream, "ENTRY_FILLED")
    stop = engine.position["stop"]
    at = filled.at + timedelta(seconds=15)
    events = engine.on_observation(Observation(at=at, ltp=stop - 20, bid=stop - 21, ask=stop - 19,
                                               bid_qty=1000, ask_qty=1000, source_ts=at))
    trade = [e for e in events if e["type"] == "EXIT"][0]["payload"]["trade"]
    assert trade["exit_reason"] == "STOP" and trade["exit_price"] == stop - 22  # worse bid - 1 tick slippage
    assert trade["gross_pnl"] == pytest.approx((stop - 22 - trade["entry_price"]) * 10)
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["costs"]["total"])
    forced, filled = _engine_at(crude_stream, "ENTRY_FILLED")
    late = filled.at.replace(hour=23, minute=45, second=0)
    events = forced.on_observation(Observation(at=late, ltp=filled.ltp, bid=filled.ltp - 1, ask=filled.ltp + 1,
                                               bid_qty=1000, ask_qty=1000, source_ts=late))
    assert [e["payload"]["trade"]["exit_reason"] for e in events if e["type"] == "EXIT"] == ["SESSION_FORCED_EXIT"]


def test_outside_session_observations_are_rejected(crude_stream: list[Observation]) -> None:
    engine = crude_engine()
    night = crude_stream[0].at.replace(hour=23, minute=58)
    events = engine.on_observation(Observation(at=night - timedelta(days=1), ltp=1.0, bid=1.0, ask=1.0,
                                               source_ts=night - timedelta(days=1)))
    assert events[-1]["payload"]["reason"] == "OUTSIDE_SESSION"
