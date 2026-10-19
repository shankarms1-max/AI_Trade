"""Versioned Phase 15 liquidation books: wide is a warning, never a fake fill."""
from copy import deepcopy
from datetime import timedelta
import json
import socket

import pytest
from sqlalchemy import select

from app.acts_paper.models import ActsPaperCursor, ActsPaperEvent, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.parallel_paper.bars import Bar
from app.parallel_paper.config import STRATEGIES
from app.parallel_paper.indicators import ema
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperEvent, ParallelPaperTrade
from app.parallel_paper.service import ParallelPaperService, verify_journals
from app.parallel_paper.trade_monitor import trades
from app.paper import exit_book, mirror_exit
from app.research.manifest import canonical, digest
from app.scalper.paper import executable_pair
from app.scalper.config import ScalperConfig
from app.scalper.strategy import validate_book
from tests.test_acts_paper import _capture, _seed_position
from tests.test_acts_paper_v2 import _decision
from tests.test_mirror_exit_v1 import (bars, mark_raw, open_acts, open_parallel, point)
from tests.test_parallel_paper_service import capture
from tests.test_scalper import settings

STRATEGIES_UNDER_TEST = ("ACTS_V2", *STRATEGIES)


def case(engine, strategy):
    if strategy == "ACTS_V2":
        return open_acts(engine)
    paper, raw, candidate, _ = open_parallel(engine, strategy)
    return paper, raw, candidate


def stored(paper, strategy):
    model = ActsPaperTrade if strategy == "ACTS_V2" else ParallelPaperTrade
    with paper.sessions() as session:
        row = session.scalar(select(model))
        return row.state, json.loads(row.document)


def wide(raw, candidate, seconds, debit, *, future=25000):
    current = mark_raw(raw, candidate, seconds, debit, future=future)
    return current.model_copy(update={"quotes": [
        quote.model_copy(update={"bid": 1, "ltp": 99999})
        if quote.instrument_token == candidate.short_leg.instrument_token else quote
        for quote in current.quotes]})


def process(engine, paper, strategy, raw, completed=None, samples=None):
    if strategy == "ACTS_V2":
        return paper.process_snapshot(_capture(engine, raw))
    return paper._process(strategy, capture(engine, raw), raw,
                          samples or [point(raw.captured_at, raw.nifty_future)], completed or [], {})


def audit(paper, strategy):
    if strategy == "ACTS_V2":
        verify_acts_paper_journal(paper.sessions)
    else:
        verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
def test_wide_mark_is_executable_but_same_book_cannot_enter(engine, monkeypatch, strategy):
    paper, initial, candidate = case(engine, strategy)
    config = paper.config if strategy == "ACTS_V2" else paper.configs[strategy]
    credit = stored(paper, strategy)[1]["entry_credit"]
    raw = wide(initial, candidate, 15, credit * .8)
    quote = next(quote for quote in raw.quotes if quote.instrument_token == candidate.short_leg.instrument_token)
    entry = validate_book(quote, raw.response_received_at, raw.lot_size, config)
    assert not entry.valid and entry.reason == exit_book.WIDE_SPREAD
    assert set(entry.payload()) == {"valid", "reason", "spread_pct", "available_units"}
    # A caller cannot relax entry validation even when opting into the new exit policy.
    pair, failure = executable_pair(raw, candidate, config, entry=True, exit_book_policy=exit_book.VERSION)
    assert pair is None and failure == exit_book.WIDE_SPREAD
    # Generic scalper / replay and ACTS V1 retain their legacy contract by default.
    assert executable_pair(raw, candidate, config, entry=False)[1] == exit_book.WIDE_SPREAD
    exit_pair, failure = executable_pair(raw, candidate, config, entry=False, exit_book_policy=exit_book.VERSION)
    assert exit_pair is not None and failure is None
    validation = exit_pair.evidence["short"]["validation"]
    assert validation["valid"] and validation["wide_spread"]
    assert validation["spread_pct"] > config.max_bid_ask_spread_pct
    assert validation["quality_warning"] == exit_book.WIDE_SPREAD and validation["reason"] is None
    assert exit_pair.evidence["long"]["validation"]["wide_spread"] is False

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paper marks must never call a broker or order API")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("app.broker.kotak.market_data.KotakMarketDataAdapter.__init__", forbidden)
    process(engine, paper, strategy, raw)
    state, doc = stored(paper, strategy)
    assert state == "OPEN" and doc["current_debit"] == pytest.approx(exit_pair.value)
    assert doc["gross_mark_points"] == pytest.approx(credit - exit_pair.value)
    assert doc["gross_mark_rupees"] == pytest.approx((credit - exit_pair.value) * raw.lot_size)
    assert doc["mark_quality_warning"] == exit_book.WIDE_SPREAD
    row = next(row for row in trades(paper.sessions, raw.captured_at, positions=True)["items"]
               if row["strategy_id"] == strategy)
    assert row["mark_available"] and row["mark_unavailable_reason"] is None
    assert row["mark_quality_warning"] == exit_book.WIDE_SPREAD
    assert row["current_debit"] == pytest.approx(exit_pair.value)
    assert row["current_unrealized_gross_pnl"] == pytest.approx(doc["gross_mark_rupees"])
    assert row["latest_mark_quotes"]["short"]["wide_spread"] is True
    assert row["latest_mark_quotes"]["short"]["spread_pct"] == validation["spread_pct"]
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
def test_wide_forced_close_uses_actual_held_ask_bid(engine, strategy):
    paper, initial, candidate = case(engine, strategy)
    credit = stored(paper, strategy)[1]["entry_credit"]
    seconds = int((initial.captured_at.replace(hour=15, minute=20, second=0) - initial.captured_at).total_seconds())
    raw = wide(initial, candidate, seconds, credit)
    process(engine, paper, strategy, raw)
    state, doc = stored(paper, strategy)
    assert state == "CLOSED" and doc["exit_reason"] == "FORCED_CLOSE"
    assert doc["exit_debit"] == pytest.approx(
        doc["exit_quotes"]["short"]["fill_price"] - doc["exit_quotes"]["long"]["fill_price"])
    assert doc["exit_quotes"]["short"]["side"] == "ask" and doc["exit_quotes"]["long"]["side"] == "bid"
    assert doc["exit_quotes"]["short"]["validation"]["wide_spread"]
    assert doc["mark_quality_warning"] == exit_book.WIDE_SPREAD
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
def test_mirror_exit_accepts_wide_held_book_without_weakening_signal(engine, monkeypatch, strategy):
    paper, initial, candidate = case(engine, strategy)
    credit = stored(paper, strategy)[1]["entry_credit"]
    if strategy == "ACTS_V2":
        ctx, _, _, _ = _decision(-1)
        raw = wide(initial, candidate, 15, credit)
        ctx["timestamp"] = raw.captured_at.isoformat()
        monkeypatch.setattr(paper, "_context", lambda _: deepcopy(ctx))
        process(engine, paper, strategy, raw)
        expected = mirror_exit.ACTS_REASON
    elif strategy.startswith("ORB_"):
        for seconds in (15, 30, 45):
            raw = wide(initial, candidate, seconds, credit, future=24980)
            process(engine, paper, strategy, raw)
            if seconds < 45:
                assert stored(paper, strategy)[0] == "OPEN"
        expected = "OPPOSITE_ORB_BREAKOUT"
    else:
        series = bars(-1, count=27, spike=100)

        def observe():
            at = series[-1].end
            raw = wide(initial, candidate, int((at - initial.captured_at).total_seconds()),
                       credit, future=series[-1].close)
            process(engine, paper, strategy, raw, completed=series)

        observe()
        assert stored(paper, strategy)[0] == "OPEN"
        if strategy == "EMA_PULLBACK":
            fast, previous = ema([bar.close for bar in series], 9)[-1], series[-1].close
            pullback = Bar(series[-1].end, series[-1].end + timedelta(minutes=3), previous,
                           fast + 3, previous - 3, fast, 100)
            series.append(pullback)
            observe()
            assert stored(paper, strategy)[0] == "OPEN"
            resumed = previous - 10
            series.append(Bar(pullback.end, pullback.end + timedelta(minutes=3), fast,
                              fast + 3, resumed - 3, resumed, 150))
            expected = "OPPOSITE_EMA_PULLBACK_RESUMPTION"
        else:
            series.append(bars(-1, count=28, spike=150)[-1])
            expected = "OPPOSITE_SUPERTREND_RSI_VOLUME_SIGNAL"
        observe()
    state, doc = stored(paper, strategy)
    assert state == "CLOSED" and doc["exit_reason"] == expected
    assert doc["exit_quotes"]["short"]["validation"]["quality_warning"] == exit_book.WIDE_SPREAD
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
@pytest.mark.parametrize("failure", ["missing", "stale", "future", "side", "depth", "unit", "crossed", "zero", "tick"])
def test_wide_quote_does_not_mask_genuine_exit_failures_or_reuse_marks(engine, strategy, failure):
    paper, initial, candidate = case(engine, strategy)
    credit = stored(paper, strategy)[1]["entry_credit"]
    valid = wide(initial, candidate, 15, credit)
    process(engine, paper, strategy, valid)
    raw = wide(initial, candidate, 30, credit)
    changes = {"stale": {"source_market_timestamp": initial.captured_at - timedelta(seconds=60)},
               "future": {"source_market_timestamp": raw.captured_at + timedelta(seconds=30)},
               "side": {"ask": None}, "depth": {"ask_quantity": 0}, "unit": {"depth_unit": "UNKNOWN"},
               "crossed": {"bid": 1000, "ask": 1}, "zero": {"ask": 0},
               "tick": {"tick_size": .05, "ask": 100.023}}
    raw = raw.model_copy(update={"quotes": [
        quote.model_copy(update=changes.get(failure, {}))
        if quote.instrument_token == candidate.short_leg.instrument_token else quote
        for quote in raw.quotes
        if failure != "missing" or quote.instrument_token != candidate.short_leg.instrument_token]})
    process(engine, paper, strategy, raw)
    state, doc = stored(paper, strategy)
    assert state == ("UNRESOLVED_EXPOSURE" if failure == "missing" else "OPEN")
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    row = next(row for row in trades(paper.sessions, raw.captured_at, positions=True)["items"]
               if row["strategy_id"] == strategy)
    assert row["mark_available"] is False and row["current_debit"] is None
    assert row["current_unrealized_gross_pnl"] is None and row["mark_quality_warning"] is None
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
@pytest.mark.parametrize("failure", ["missing", "stale", "side", "depth"])
def test_forced_close_never_fabricates_a_genuinely_invalid_book(engine, strategy, failure):
    paper, initial, candidate = case(engine, strategy)
    credit = stored(paper, strategy)[1]["entry_credit"]
    seconds = int((initial.captured_at.replace(hour=15, minute=20, second=0) - initial.captured_at).total_seconds())
    raw = wide(initial, candidate, seconds, credit)
    changes = {"stale": {"source_market_timestamp": raw.captured_at - timedelta(seconds=60)},
               "side": {"ask": None}, "depth": {"ask_quantity": 0}}
    raw = raw.model_copy(update={"quotes": [
        quote.model_copy(update=changes.get(failure, {}))
        if quote.instrument_token == candidate.short_leg.instrument_token else quote
        for quote in raw.quotes
        if failure != "missing" or quote.instrument_token != candidate.short_leg.instrument_token]})
    process(engine, paper, strategy, raw)
    state, doc = stored(paper, strategy)
    assert state == "UNRESOLVED_EXPOSURE"
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    assert doc["unresolved_reason"] != exit_book.WIDE_SPREAD
    audit(paper, strategy)


@pytest.mark.parametrize("strategy", STRATEGIES_UNDER_TEST)
@pytest.mark.parametrize("state", ["PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE", "CLOSED", "ENTRY_REJECTED"])
def test_exit_book_policy_upgrade_preserves_all_historical_rows(engine, strategy, state):
    paper, initial, candidate = case(engine, strategy)
    acts = strategy == "ACTS_V2"
    trade_model, event_model = (ActsPaperTrade, ActsPaperEvent) if acts else (ParallelPaperTrade, ParallelPaperEvent)
    with paper.sessions.begin() as session:
        row = session.scalar(select(trade_model))
        doc = json.loads(row.document)
        old_policy = deepcopy(doc["policy"])
        old_policy.pop("exit_book_policy")
        assert old_policy["exit_policy"] == (mirror_exit.acts_policy(paper.config) if acts else
                                             mirror_exit.policy(paper.configs[strategy]))
        doc.update(policy=old_policy, policy_hash=digest(old_policy))
        row.state, row.document = state, canonical(doc)
        if acts:
            paper._emit(session, session.get(ActsPaperCursor, 1), "ACTS_TEST_OLD_BOOK_POLICY",
                        row.decision_snapshot_id, initial, row)
        else:
            paper._emit(session, session.get(ParallelPaperCursor, strategy), row.decision_snapshot_id,
                        initial, "TEST_OLD_BOOK_POLICY", row)

    def records():
        with paper.sessions() as session:
            return ([(row.id, row.state, row.document) for row in session.scalars(select(trade_model))],
                    [(row.sequence, row.payload, row.previous_hash, row.event_hash)
                     for row in session.scalars(select(event_model).order_by(event_model.sequence))])

    before = records()
    audit(paper, strategy)
    if state in {"PENDING_PAPER_ENTRY", "OPEN", "UNRESOLVED_EXPOSURE"}:
        with pytest.raises(RuntimeError, match="EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES"):
            (ActsPaperService if acts else ParallelPaperService)(paper.sessions, settings())
        raw = wide(initial, candidate, 15, doc["entry_credit"])
        if acts:
            with pytest.raises(RuntimeError, match="EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES"):
                process(engine, paper, strategy, raw)
        else:
            other = next(name for name in STRATEGIES if name != strategy)
            with pytest.raises(RuntimeError, match="EXIT_BOOK_POLICY_UPGRADE_REQUIRES_ZERO_ACTIVE_TRADES"):
                paper._process(other, capture(engine, raw), raw, [], [], {})
    else:
        (ActsPaperService if acts else ParallelPaperService)(paper.sessions, settings())
    assert records() == before
    audit(paper, strategy)


def test_legacy_v1_wide_exit_remains_strict_and_policy_hash_unchanged(engine):
    paper, initial, candidate, _ = _seed_position(engine)
    raw = wide(initial, candidate, 15, stored(paper, "ACTS_V2")[1]["entry_credit"])
    assert executable_pair(raw, candidate, paper.config, entry=False)[1] == exit_book.WIDE_SPREAD
    assert paper.policy_hash == paper.v1_policy_hash


@pytest.mark.parametrize("change", [
    {"bid": 1, "ask": 3}, {"bid": None}, {"ask": 0},
    {"source_market_timestamp": None}, {"bid_quantity": 0},
    {"open_interest": 0}, {"volume": 0}, {"depth_unit": "UNKNOWN"},
])
def test_exit_opt_in_cannot_change_entry_validation_or_entry_evidence(change):
    from tests.test_scalper import snapshot
    raw = snapshot(0)
    config = ScalperConfig.from_settings(settings())
    quote = raw.quotes[0].model_copy(update=change)
    default = validate_book(quote, raw.response_received_at, raw.lot_size, config, entry=True)
    opted_in = validate_book(
        quote, raw.response_received_at, raw.lot_size, config, entry=True, allow_wide_exit=True)
    assert not default.valid and opted_in.payload() == default.payload()


@pytest.mark.parametrize("unit", ["UNITS", "LOTS"])
def test_exit_depth_is_checked_only_on_short_ask_and_hedge_bid(engine, unit):
    paper, raw, candidate, _ = open_parallel(engine)
    required = raw.lot_size if unit == "UNITS" else 1
    raw = raw.model_copy(update={"quotes": [quote.model_copy(update={
        "depth_unit": unit,
        "bid_quantity": 0 if quote.instrument_token == candidate.short_leg.instrument_token else required,
        "ask_quantity": 0 if quote.instrument_token == candidate.long_leg.instrument_token else required})
        for quote in raw.quotes]})
    pair, failure = executable_pair(raw, candidate, paper.configs["ORB_15"],
                                    entry=False, exit_book_policy=exit_book.VERSION)
    assert pair is not None and failure is None
    assert pair.evidence["short"]["validation"]["available_units"] == raw.lot_size
    assert pair.evidence["long"]["validation"]["available_units"] == raw.lot_size
