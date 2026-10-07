"""Deterministic offline coverage for the isolated Phase 15 scalper."""
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import MarketSnapshotRecord
from app.db.base import Base
from app.paper.models import PaperEvent, PaperTrade
from app.scalper.config import ScalperConfig
from app.scalper.features import build_features
from app.scalper.models import (ScalperCursor, ScalperDirection, ScalperEvent,
                                ScalperMarketSnapshot, ScalperOptionQuote,
                                ScalperSignal, ScalperStrength, ScalperTrade,
                                ScalperMarketSnapshotRecord,
                                ScalperOptionQuoteRecord)
from app.scalper.paper import exit_trigger
from app.scalper.risk import ScalperRiskState, evaluate_entry
from app.scalper.service import ScalperService, verify_scalper_journal
from app.scalper.signals import build_signal
from app.scalper.strategy import build_candidates, validate_book

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2099, 10, 8)


def settings(**updates):
    return Settings(_env_file=None, kotak_consumer_key="offline-scalper",
                    scalper_enabled=True, scalper_signal_min_score=75,
                    scalper_min_confirmations=2,
                    scalper_min_short_distance_points=0,
                    scalper_max_loss_per_trade=50_000, **updates)


def snapshot(index: int, *, direction: int = 1, gap_seconds: int = 15,
             mutate=None) -> ScalperMarketSnapshot:
    at = datetime(2099, 10, 1, 10, 0, tzinfo=IST) + timedelta(seconds=index * gap_seconds)
    spot = 25_000 + direction * index * 20
    quotes = []
    for strike in range(24_400, 25_601, 50):
        for option_type in ("CE", "PE"):
            premium = max(5.0, ((strike - 24_200) if option_type == "PE"
                                else (25_800 - strike)) * .1)
            values = dict(expiry=EXPIRY, strike=strike, option_type=option_type,
                          trading_symbol=f"NIFTY{strike}{option_type}",
                          instrument_token=f"{strike}{option_type}",
                          source_market_timestamp=at, bid=premium, ask=premium + 1,
                          bid_quantity=1000, ask_quantity=1000, depth_unit="UNITS",
                          tick_size=.05, ltp=premium + .5, volume=100 + index * 10,
                          open_interest=(1000 + index * (
                              30 if (option_type == "PE") == (direction > 0) else 5)))
            if mutate:
                values = mutate(values)
            quotes.append(ScalperOptionQuote(**values))
    return ScalperMarketSnapshot(
        captured_at=at, request_started_at=at - timedelta(seconds=1),
        response_received_at=at, source_market_timestamp=at,
        nifty_spot=spot, nifty_future=spot + direction * (15 + index * 4),
        india_vix=14, lot_size=50, atm_strike=round(spot / 50) * 50,
        expiry=EXPIRY, quotes=quotes)


def signal_for(raw, direction="BULL", score=90, confirmed=True):
    return ScalperSignal(timestamp=raw.captured_at, direction=direction,
                         strength="STRONG", score=score,
                         components={"momentum": 25, "structure": 20,
                                     "futures": 15, "participation": 15,
                                     "execution": 25},
                         contradiction_penalty=0, reasons=[], warnings=[],
                         confirmation_count=2 if confirmed else 1,
                         confirmed=confirmed)


@pytest.fixture
def scalper(session_factory):
    with session_factory.begin() as session:
        session.add(ScalperCursor(id=1))
    return SimpleNamespace(service=ScalperService(session_factory, settings()),
                           sessions=session_factory)


def trade(case):
    with case.sessions() as session:
        row = session.scalar(select(ScalperTrade).order_by(ScalperTrade.decision_snapshot_id))
        return None if row is None else (row.state, json.loads(row.document))


@pytest.mark.parametrize("direction,expected", [(1, ScalperDirection.BULL),
                                                (-1, ScalperDirection.BEAR)])
def test_short_window_features_and_directional_signal(direction, expected):
    history = [snapshot(index, direction=direction) for index in range(4)]
    features = build_features(history, expected_interval_seconds=15)
    prior = []
    first = build_signal(history[-2], build_features(history[:-1],
                         expected_interval_seconds=15), prior, min_score=75,
                         min_confirmations=2, max_confirmation_gap_seconds=22.5)
    result = build_signal(history[-1], features, [first], min_score=75,
                          min_confirmations=2, max_confirmation_gap_seconds=22.5)
    assert result.direction == expected
    assert result.score >= 75
    assert set(result.components) == {"momentum", "structure", "futures",
                                      "participation", "execution"}
    assert result.confirmed
    assert "UNDERLYING_VWAP_UNAVAILABLE" in result.warnings


def test_confirmation_resets_across_missing_fast_observation():
    history = [snapshot(index) for index in range(3)]
    previous = signal_for(history[1])
    later = history[2].model_copy(update={
        "captured_at": history[1].captured_at + timedelta(seconds=45),
        "request_started_at": history[1].captured_at + timedelta(seconds=44),
        "response_received_at": history[1].captured_at + timedelta(seconds=45)})
    features = build_features([history[0], history[1], later],
                              expected_interval_seconds=15)
    result = build_signal(later, features, [previous], min_score=75,
                          min_confirmations=2, max_confirmation_gap_seconds=22.5)
    assert not result.confirmed and result.confirmation_count == 1
    assert "MISSING_FAST_OBSERVATION" in features.warnings


def test_neutral_market_and_score_threshold_do_not_confirm():
    history = [snapshot(index).model_copy(update={
        "nifty_spot": 25_000, "nifty_future": 25_000,
        "quotes": [item.model_copy(update={"open_interest": 1000, "volume": 100})
                   for item in snapshot(index).quotes]}) for index in range(3)]
    features = build_features(history, expected_interval_seconds=15)
    result = build_signal(history[-1], features, [], min_score=80,
                          min_confirmations=2, max_confirmation_gap_seconds=22.5)
    assert result.direction == ScalperDirection.NEUTRAL
    assert result.strength == ScalperStrength.NONE
    assert not result.confirmed and result.score < 80


@pytest.mark.parametrize("direction,strategy", [("BULL", "BULL_PUT_SPREAD"),
                                                ("BEAR", "BEAR_CALL_SPREAD")])
def test_candidate_construction_uses_complete_bid_ask_verticals(direction, strategy):
    raw = snapshot(3, direction=1 if direction == "BULL" else -1)
    result = build_candidates(raw, signal_for(raw, direction),
                              ScalperConfig.from_settings(settings()))
    assert result.candidates
    assert result.candidates[0].strategy_type == strategy
    assert {100, 200, 300, 400} <= {item.spread_width for item in result.candidates}
    assert all(item.pricing_basis == "BID_ASK" and item.executable_credit > 0
               and item.broker_margin is None for item in result.candidates)


@pytest.mark.parametrize("change,reason", [
    ({"bid": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"ask": None}, "MISSING_EXECUTABLE_SIDE"),
    ({"depth_unit": "UNKNOWN"}, "UNKNOWN_REQUIRED_DEPTH"),
    ({"bid_quantity": 0}, "INSUFFICIENT_DEPTH"),
    ({"source_market_timestamp": None}, "MISSING_QUOTE_TIMESTAMP"),
])
def test_stale_missing_and_illiquid_quotes_are_rejected(change, reason):
    raw = snapshot(1)
    quote = raw.quotes[0].model_copy(update=change)
    result = validate_book(quote, raw.response_received_at, raw.lot_size,
                           ScalperConfig.from_settings(settings()))
    assert not result.valid and result.reason == reason
    stale = raw.quotes[0].model_copy(update={
        "source_market_timestamp": raw.captured_at - timedelta(seconds=31)})
    assert validate_book(stale, raw.response_received_at, raw.lot_size,
                         ScalperConfig.from_settings(settings())).reason == "STALE_OR_FUTURE_QUOTE"


def test_wide_book_and_missing_hedge_produce_no_candidate():
    raw = snapshot(3)
    wide = raw.quotes[0].model_copy(update={"bid": 1, "ask": 3})
    result = validate_book(wide, raw.response_received_at, raw.lot_size,
                           ScalperConfig.from_settings(settings()))
    assert result.reason == "BID_ASK_SPREAD_TOO_WIDE"
    no_put_hedges = raw.model_copy(update={
        "quotes": [item for item in raw.quotes
                   if item.option_type == "CE" or item.strike == 25_050]})
    built = build_candidates(no_put_hedges, signal_for(raw),
                             ScalperConfig.from_settings(settings()))
    assert not built.candidates
    assert built.rejection_counts["LONG_LEG_NOT_CAPTURED"] == 4


def test_strict_next_observation_entry_and_no_same_observation_fill(scalper):
    for index in range(3):
        scalper.service.capture_once(snapshot(index))
    state, document = trade(scalper)
    assert state == "PENDING_ENTRY"
    assert "entry_timestamp" not in document
    scalper.service.engine.process_snapshot(document["decision_snapshot_id"])
    assert trade(scalper)[0] == "PENDING_ENTRY"
    scalper.service.capture_once(snapshot(3))
    state, document = trade(scalper)
    assert state == "OPEN"
    assert document["execution_snapshot_id"] > document["decision_snapshot_id"]
    assert document["entry_quotes"]["short"]["side"] == "bid"
    assert document["entry_quotes"]["long"]["side"] == "ask"
    assert verify_scalper_journal(scalper.sessions) > 0


def test_profit_capture_exit_uses_short_ask_and_long_bid_and_gross_only(scalper):
    for index in range(4):
        scalper.service.capture_once(snapshot(index))
    state, document = trade(scalper)
    assert state == "OPEN"
    short_token = document["candidate"]["short_leg"]["instrument_token"]
    long_token = document["candidate"]["long_leg"]["instrument_token"]

    def exit_prices(values):
        if values["instrument_token"] == short_token:
            return values | {"bid": 9, "ask": 10}
        if values["instrument_token"] == long_token:
            return values | {"bid": 9, "ask": 10}
        return values

    scalper.service.capture_once(snapshot(4, mutate=exit_prices))
    state, document = trade(scalper)
    assert state == "CLOSED" and document["exit_reason"] == "PROFIT_CAPTURE"
    assert document["exit_quotes"]["short"]["side"] == "ask"
    assert document["exit_quotes"]["long"]["side"] == "bid"
    assert document["final_outcome"]["cost_completeness"] == "GROSS_ONLY"
    assert document["final_outcome"]["net_rupees"] is None
    assert document["profitability_claim"] is False


def test_risk_limits_daily_loss_consecutive_losses_and_cooldowns():
    raw = snapshot(3)
    candidate = build_candidates(raw, signal_for(raw),
                                 ScalperConfig.from_settings(settings())).candidates[0]
    now = raw.captured_at
    state = ScalperRiskState(open_positions=1, executed_trades_today=10,
                             gross_realized_today=-50_000, consecutive_losses=3,
                             last_exit_at=now - timedelta(seconds=10),
                             last_loss_at=now - timedelta(seconds=10))
    decision = evaluate_entry(candidate, signal_for(raw), state,
                              ScalperConfig.from_settings(settings()), now)
    assert not decision.approved
    assert {"MAX_OPEN_POSITIONS_REACHED", "MAX_TRADES_PER_DAY_REACHED",
            "HARD_DAILY_LOSS_REACHED", "MAX_CONSECUTIVE_LOSSES_REACHED",
            "ANY_EXIT_COOLDOWN_ACTIVE", "LOSS_COOLDOWN_ACTIVE"} <= set(decision.reasons)


@pytest.mark.parametrize("case,expected", [
    ("signal", "SIGNAL_FAILURE"), ("structure", "STRUCTURE_FAILURE"),
    ("stop", "SPREAD_STOP"), ("profit", "PROFIT_CAPTURE"),
    ("trailing", "TRAILING_EXIT"), ("time", "TIME_STOP"),
    ("forced", "FORCED_INTRADAY_CLOSE"),
])
def test_fast_exit_rules(case, expected):
    raw = snapshot(4)
    config = ScalperConfig.from_settings(settings())
    document = {"entry_credit": 10, "candidate": {"direction": "BULL"},
                "structural_reference": raw.nifty_spot - 100,
                "entry_timestamp": (raw.captured_at - timedelta(minutes=5)).isoformat(),
                "best_gross_points": 0}
    signal = signal_for(raw)
    debit = 9
    if case == "signal":
        signal = signal_for(raw, "NEUTRAL", 0, False)
    elif case == "structure":
        document["structural_reference"] = raw.nifty_spot + 1
    elif case == "stop":
        debit = 15
    elif case == "profit":
        debit = 4
    elif case == "trailing":
        document["best_gross_points"] = 7
        debit = 6
    elif case == "time":
        document["entry_timestamp"] = (raw.captured_at - timedelta(minutes=31)).isoformat()
    elif case == "forced":
        forced = raw.captured_at.replace(hour=15, minute=20)
        raw = raw.model_copy(update={"captured_at": forced,
                                     "request_started_at": forced - timedelta(seconds=1),
                                     "response_received_at": forced})
    assert exit_trigger(document, raw, signal, debit, config) == expected


def test_default_is_disabled_and_interval_bounds_are_enforced():
    default = Settings(_env_file=None, kotak_consumer_key="offline-scalper")
    assert default.scalper_enabled is False and default.scalper_interval_seconds == 15
    with pytest.raises(ValueError):
        settings(scalper_interval_seconds=9)
    with pytest.raises(ValueError):
        settings(scalper_interval_seconds=61)


def test_no_broker_order_path_and_no_phase14_table_interference(scalper, monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(
        AssertionError("network path forbidden")))
    scalper.service.capture_once(snapshot(0))
    with scalper.sessions() as session:
        assert session.scalar(select(func.count(MarketSnapshotRecord.id))) == 0
        assert session.scalar(select(func.count(PaperTrade.id))) == 0
        assert session.scalar(select(func.count(PaperEvent.sequence))) == 0
        assert session.scalar(select(func.count(ScalperEvent.sequence))) == 1


def test_duplicate_snapshot_is_idempotent(scalper):
    raw = snapshot(0)
    first = scalper.service.capture_once(raw)
    second = scalper.service.capture_once(raw)
    assert first == second
    with scalper.sessions() as session:
        assert session.scalar(select(func.count(ScalperEvent.sequence))) == 1


def test_restart_is_idempotent(scalper):
    raw = snapshot(0)
    scalper.service.capture_once(raw)
    restarted = ScalperService(scalper.sessions, settings())
    assert restarted.capture_once(raw) == 1
    assert verify_scalper_journal(scalper.sessions) == 1


def test_out_of_order_capture_fails_before_persistence(scalper):
    newer = snapshot(2)
    scalper.service.capture_once(newer)
    with pytest.raises(ValueError, match="OUT_OF_ORDER_CAPTURE"):
        scalper.service.capture_once(snapshot(1))
    with scalper.sessions() as session:
        assert session.scalar(select(func.count(ScalperMarketSnapshotRecord.id))) == 1


def test_projection_tampering_is_detected(scalper):
    for index in range(3):
        scalper.service.capture_once(snapshot(index))
    with scalper.sessions.begin() as session:
        session.execute(update(ScalperTrade).values(state="CLOSED"))
    with pytest.raises(ValueError, match="PROJECTION_INTEGRITY"):
        verify_scalper_journal(scalper.sessions)


def test_source_and_ingestion_timestamps_are_persisted_separately(scalper):
    raw = snapshot(0)
    scalper.service.capture_once(raw)
    with scalper.sessions() as session:
        saved = session.scalar(select(ScalperMarketSnapshotRecord))
        quote = session.scalar(select(ScalperOptionQuoteRecord))
        assert saved.response_received_at is not None
        assert saved.request_started_at is not None
        assert quote.source_market_timestamp is not None
        assert saved.request_started_at != saved.response_received_at


def test_replay_is_deterministic_and_future_observations_do_not_change_history(tmp_path):
    observations = [snapshot(index) for index in range(5)]

    def replay(name, count):
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / name}")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions.begin() as session:
            session.add(ScalperCursor(id=1))
        service = ScalperService(sessions, settings())
        for raw in observations[:count]:
            service.capture_once(raw)
        with sessions() as session:
            snapshots = [(row.feature_json, row.signal_json) for row in session.scalars(
                select(ScalperMarketSnapshotRecord).order_by(ScalperMarketSnapshotRecord.id))]
            trades = [(row.state, row.document) for row in session.scalars(
                select(ScalperTrade).order_by(ScalperTrade.id))]
            events = [row.payload for row in session.scalars(
                select(ScalperEvent).order_by(ScalperEvent.sequence))]
        engine.dispose()
        return snapshots, trades, events

    first = replay("first.db", 3)
    second = replay("second.db", 3)
    extended = replay("extended.db", 5)
    assert first == second
    assert first[0] == extended[0][:3]
    assert first[2] == extended[2][:len(first[2])]
