"""Precommitted mirror exits over synthetic, causal fixtures; no broker access."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.acts_paper import acts_v2
from app.acts_paper.models import ActsPaperCursor, ActsPaperTrade
from app.acts_paper.service import ActsPaperService, verify_acts_paper_journal
from app.parallel_paper.bars import Bar, MarketPoint
from app.parallel_paper.config import ExperimentConfig, VERSIONS
from app.parallel_paper.decisions import decide
from app.parallel_paper.indicators import ema
from app.parallel_paper.models import ParallelPaperCursor, ParallelPaperTrade
from app.parallel_paper.service import ParallelPaperService, verify_journals
from app.db.session import build_session_factory
from app.paper import mirror_exit as mirror
from app.research.manifest import canonical, digest, json_value
from app.scalper.config import ScalperConfig
from app.scalper.paper import executable_pair
from app.scalper.strategy import build_candidates
from app.scalper_lab.engine import _construction_signal
from tests.test_acts_paper import _capture, _next
from tests.test_acts_paper_v2 import _decision
from tests.test_parallel_paper_service import capture, raw, service
from tests.test_parallel_paper_strategies import FUTURE_EXPIRY, START, bars, points
from tests.test_scalper import settings
from tests.test_scalper_acts import qualified_rows


def document(strategy="ORB_15", direction="BULL"):
    return {"strategy_id": strategy, "direction": direction,
            "exit_policy_version": mirror.VERSION,
            "future_instrument_id": "NIFTY_FUT", "future_expiry": FUTURE_EXPIRY.isoformat(),
            "decision_timestamp": (START + timedelta(minutes=60)).isoformat(),
            "entry_timestamp": (START + timedelta(minutes=60)).isoformat(),
            "entry_credit": 20.0, "lot_size": 50,
            "best_gross_points": 0.0, "mfe": 0.0, "mae": 0.0,
            "strategy_features": {"complete": True, "opening_high": 25010,
                                  "opening_low": 24990, "pullback_anchor": 25000}}


def point(at, price):
    return MarketPoint(at, price, "NIFTY_FUT", FUTURE_EXPIRY)


@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize("direction,sign", [("BULL", 1), ("BEAR", -1)])
def test_orb_only_exits_confirmed_opposite_original_range(minutes, direction, sign):
    doc = document(f"ORB_{minutes}", direction)
    high, low = 25000 + minutes, 25000 - minutes
    doc["strategy_features"].update(opening_high=high, opening_low=low)
    at = START + timedelta(minutes=61)
    # Own boundary and midpoint crosses are diagnostics, never exits.
    for offset, price in ((0, 25000 + sign), (15, 25000 - sign)):
        now = at + timedelta(seconds=offset)
        assert mirror.parallel_signal(doc, [point(now, price)], [], now, ExperimentConfig(), available=True) is None
    opposite_price = low - 1 if sign > 0 else high + 1
    for offset in (30, 45, 60):
        now = at + timedelta(seconds=offset)
        result = mirror.parallel_signal(doc, [point(now, opposite_price)], [], now,
                                        ExperimentConfig(), available=True)
        assert result == ("OPPOSITE_ORB_BREAKOUT" if offset == 60 else None)
    assert doc["strategy_features"]["opening_high"] == high
    assert doc["mirror_exit_latest"]["features"]["opening_low"] == low
    assert {"same_boundary_reentry_first_at", "opening_midpoint_cross_first_at",
            "opposite_breakout_first_at", "opposite_breakout_confirmed_at"} <= doc["mirror_diagnostics"].keys()


@pytest.mark.parametrize("interruption", ["inside", "gap", "missing", "identity"])
def test_orb_confirmation_resets(interruption):
    doc, config = document(), ExperimentConfig()
    at = START + timedelta(minutes=61)
    assert mirror.parallel_signal(doc, [point(at, 24980)], [], at, config, available=True) is None
    now = at + timedelta(seconds=15)
    broken = point(now, 25000 if interruption == "inside" else 24980)
    if interruption == "missing":
        broken = replace(broken, price=None)
    if interruption == "identity":
        broken = replace(broken, future_id="OTHER")
    if interruption != "gap":
        assert mirror.parallel_signal(doc, [broken], [], now, config, available=True) is None
    now = at + timedelta(seconds=60 if interruption == "gap" else 30)
    assert mirror.parallel_signal(doc, [point(now, 24980)], [], now, config, available=True) is None
    assert doc["mirror_exit_latest"]["features"]["confirmation_seconds"] == 0


@pytest.mark.parametrize("held,opposite", [("BULL", -1), ("BEAR", 1)])
def test_ema_requires_full_opposite_pullback_resumption_and_isolates_state(held, opposite):
    doc, config = document("EMA_PULLBACK", held), ExperimentConfig()
    trend = bars(opposite, count=23)

    def evaluate():
        now = trend[-1].end
        return mirror.parallel_signal(doc, [point(now, trend[-1].close)], trend, now, config, available=True)

    assert evaluate() is None  # Opposite EMA ordering alone.
    fast = ema([bar.close for bar in trend], config.ema_fast)[-1]
    previous = trend[-1].close
    pullback = Bar(trend[-1].end, trend[-1].end + timedelta(minutes=3), previous,
                   max(previous, fast) + 3, min(previous, fast) - 3, fast, 100)
    trend.append(pullback)
    assert evaluate() is None
    resumed = previous + opposite * 10
    trend.append(Bar(pullback.end, pullback.end + timedelta(minutes=3), fast,
                     max(resumed, fast) + 3, min(resumed, fast) - 3, resumed, 150))
    assert evaluate() == "OPPOSITE_EMA_PULLBACK_RESUMPTION"
    assert {"adverse_ema_cross_first_at", "opposite_setup_watch_first_at",
            "opposite_entry_confirmed_at"} <= doc["mirror_diagnostics"].keys()


def test_mirror_uses_only_completed_bars_and_rejects_future_points():
    doc, series = document("EMA_PULLBACK"), bars(-1)
    now = series[-1].end - timedelta(seconds=1)
    with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
        mirror.parallel_signal(doc, [point(now, 1)], series, now, ExperimentConfig(), available=True)
    with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
        mirror.parallel_signal(doc, [point(now + timedelta(seconds=1), 1)], [], now,
                               ExperimentConfig(), available=True)
    doc["entry_timestamp"] = series[-1].end.isoformat()
    assert mirror.parallel_signal(doc, [point(series[-1].end, 1)], series, series[-1].end,
                                  ExperimentConfig(), available=True) is None
    assert "mirror_exit_latest" not in doc


@pytest.mark.parametrize("held,opposite", [("BULL", -1), ("BEAR", 1)])
def test_technical_requires_opposite_rsi_and_volume_and_records_early_events(monkeypatch, held, opposite):
    from app.parallel_paper import decisions
    doc, config, series = document("SUPERTREND_RSI_VOLUME", held), ExperimentConfig(), bars(opposite, spike=100)
    original = decisions.rsi
    monkeypatch.setattr(decisions, "rsi", lambda *_: 50.0)
    at = series[-1].end
    assert mirror.parallel_signal(doc, [point(at, series[-1].close)], series, at, config, available=True) is None
    assert "opposite_supertrend_first_at" in doc["mirror_diagnostics"]
    assert "opposite_supertrend_rsi_first_at" not in doc["mirror_diagnostics"]
    monkeypatch.setattr(decisions, "rsi", original)
    assert mirror.parallel_signal(doc, [point(at, series[-1].close)], series, at, config, available=True) is None
    assert "opposite_supertrend_rsi_first_at" in doc["mirror_diagnostics"]
    new = bars(opposite, count=26, spike=150)[-1]
    series.append(new)
    assert mirror.parallel_signal(doc, [point(new.end, new.close)], series, new.end, config,
                                  available=True) == "OPPOSITE_SUPERTREND_RSI_VOLUME_SIGNAL"
    evidence = doc["mirror_diagnostics"]["evidence"]["opposite_full_signal_first_at"]
    assert evidence["features"]["volume_ratio"] == 1.5
    assert evidence["features"]["atr"] > 0
    assert {"rsi", "supertrend", "supertrend_direction"} <= evidence["features"].keys()
    assert {"gross_pnl", "mfe", "mae"} <= evidence.keys()


def acts_case(held="BULL"):
    ctx, candidate, rejected, config = _decision(-1 if held == "BULL" else 1)
    doc = document("ACTS_V2", held)
    doc.update(entry_timestamp=(datetime.fromisoformat(ctx["timestamp"]) - timedelta(minutes=1)).isoformat(),
               future_instrument_id=ctx["future_instrument_id"], future_expiry=str(ctx["future_expiry"]))
    trade = SimpleNamespace(decision={"direction": held, "thesis": {"wall": None}},
                            acts_adverse_since={}, acts_last_adverse_at={})
    return doc, trade, ctx, candidate, rejected, config


def execution_needed_context(ctx):
    """Fixed synthetic market evidence worth 45 non-execution points."""
    ctx = deepcopy(ctx)
    ctx["acts"]["alpha"]["status"] = "UNAVAILABLE"
    ctx["options"]["walls"] = {"put_support": None, "call_resistance": None}
    for radius in ("3", "5"):
        ctx["options"]["local_pcr"][radius]["volume"] = 1
        ctx["options"]["local_pcr"][radius]["oi_slope_per_minute"] = {"3m": None, "5m": None}
    ctx["structure"]["range_position"] = .15
    return ctx


@pytest.mark.parametrize("range_position,expected_status", [(.330145, "WATCH"), (.33014, "ENTER")])
def test_acts_uses_exact_original_55_point_boundary_without_adjustment(range_position, expected_status):
    doc, trade, ctx, candidate, rejected, config = acts_case()
    ctx = execution_needed_context(ctx)
    ctx["structure"]["range_position"] = range_position
    expected = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert (config.watch_score, config.tradeable_score, config.strong_score, config.very_strong_score) == (40, 55, 70, 85)
    assert expected["opportunity_score"] == (55 if expected_status == "ENTER" else 54.9999)
    assert expected["status"] == expected_status
    result = mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                                candidate_rejections=rejected, available=True)
    assert result == (mirror.ACTS_REASON if expected_status == "ENTER" else None)


def test_acts_native_pullback_watch_and_resumption_are_retained():
    doc, trade, ctx, candidate, rejected, config = acts_case()
    ctx["futures_returns_bps"]["3m"] = 3
    assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                              candidate_rejections=rejected, available=True) is None
    assert doc["mirror_exit_latest"]["status"] == "WATCH"
    ctx["timestamp"] = (datetime.fromisoformat(ctx["timestamp"]) + timedelta(seconds=15)).isoformat()
    ctx["futures_returns_bps"]["3m"] = -5
    assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                              candidate_rejections=rejected, available=True) == mirror.ACTS_REASON
    assert doc["mirror_exit_latest"]["entry_reason"] == "PULLBACK_RESUMPTION"


@pytest.mark.parametrize("held", ["BULL", "BEAR"])
def test_acts_full_score_parity_without_portfolio_entry_permission_or_extra_delay(held):
    doc, trade, ctx, candidate, rejected, config = acts_case(held)
    entry = acts_v2.decide(ctx, {}, candidate, rejected, ["MAX_OPEN_POSITIONS_REACHED"], config)
    expected = acts_v2.decide(ctx, {}, candidate, rejected, [], config)
    assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                              candidate_rejections=rejected, available=True) == mirror.ACTS_REASON
    actual = {key: value for key, value in doc["mirror_exit_latest"].items() if key != "signal_candidate"}
    assert actual == expected
    assert entry["status"] == "NO_SETUP" and actual["status"] == "ENTER"
    assert entry["opportunity_score"] == actual["opportunity_score"]
    assert actual["score_components"]["execution"] > 0
    short, long = candidate.quote_evidence["short"], candidate.quote_evidence["long"]
    spread = config.weights["execution_spread"] * (1 - (
        short["validation"]["spread_pct"] + long["validation"]["spread_pct"]
    ) / (2 * config.max_bid_ask_spread_pct))
    depth = config.weights["execution_depth"] * min(
        1, min(short["bid_quantity"], long["ask_quantity"]) / (2 * ctx["lot_size"]))
    credit = config.weights["execution_credit"] * min(
        1, candidate.credit_to_width / (2 * config.min_credit_to_width))
    assert all(value > 0 for value in (spread, depth, credit))
    assert actual["score_components"]["execution"] == pytest.approx(round(spread + depth + credit, 4))
    assert doc["mirror_exit_request"]["observed_at"] == ctx["timestamp"]
    assert mirror.ACTS_REASON not in trade.acts_adverse_since


@pytest.mark.parametrize("interruption", ["neutral", "identity"])
def test_acts_signal_requires_current_opposite_market_and_original_future(interruption):
    doc, trade, ctx, candidate, rejected, config = acts_case()
    at = datetime.fromisoformat(ctx["timestamp"])
    saved = deepcopy(ctx)
    if interruption == "neutral":
        ctx["futures_returns_bps"].update({"3m": 0, "5m": 0, "15m": 0})
    else:
        ctx["future_instrument_id"] = "OTHER"
    assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                              candidate_rejections=rejected, available=True) is None
    ctx = saved
    ctx["timestamp"] = (at + timedelta(seconds=90)).isoformat()
    assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                              candidate_rejections=rejected, available=True) == mirror.ACTS_REASON


def test_acts_missing_candidate_never_qualifies_and_old_invalidation_is_diagnostic_only():
    doc, trade, ctx, _, _, config = acts_case()
    start = datetime.fromisoformat(ctx["timestamp"])
    for seconds in range(0, 91, 15):
        ctx["timestamp"] = (start + timedelta(seconds=seconds)).isoformat()
        assert mirror.acts_signal(doc, trade, ctx, config, candidate=None,
                                  candidate_rejections={"STALE_QUOTE": 1}, available=True) is None
    assert "SUSTAINED_TREND_REVERSAL" in doc["mirror_diagnostics"]
    assert doc["mirror_exit_latest"]["status"] == "NO_SETUP"
    assert "NO_EXECUTABLE_CANDIDATE" in doc["mirror_exit_latest"]["hard_blockers"]
    assert "mirror_exit_request" not in doc


def test_acts_component_reversal_with_weak_full_thesis_remains_diagnostic_under_default_thresholds():
    doc, trade, ctx, candidate, rejected, config = acts_case()
    ctx["acts"]["alpha"]["status"] = "UNAVAILABLE"
    ctx["options"]["walls"] = {"put_support": None, "call_resistance": None}
    for radius in ("3", "5"):
        ctx["options"]["local_pcr"][radius]["volume"] = None
        ctx["options"]["local_pcr"][radius]["oi_slope_per_minute"] = {"3m": None, "5m": None}
    ctx["structure"]["range_position"] = .49
    start = datetime.fromisoformat(ctx["timestamp"])
    for seconds in range(0, 91, 15):
        ctx["timestamp"] = (start + timedelta(seconds=seconds)).isoformat()
        assert mirror.acts_signal(doc, trade, ctx, config, candidate=candidate,
                                  candidate_rejections=rejected, available=True) is None
    assert "SUSTAINED_TREND_REVERSAL" in doc["mirror_diagnostics"]
    assert doc["mirror_exit_latest"]["opportunity_score"] < config.tradeable_score


@pytest.mark.parametrize("debit,changes,blocked,at,expected", [
    (30, {}, False, (11, 0), "SPREAD_STOP"),
    (24, {"max_loss_per_trade": 200}, False, (11, 0), "HARD_MAX_LOSS"),
    (20, {"kill_switch": True}, False, (11, 0), "KILL_SWITCH"),
    (20, {}, True, (11, 0), "MARKET_EVENT_EXIT"),
    (30, {"kill_switch": True}, True, (15, 20), "FORCED_CLOSE"),
])
def test_exact_stop_safety_and_forced_close_precedence(debit, changes, blocked, at, expected):
    config = replace(ScalperConfig.from_settings(settings()), **changes)
    now = START.replace(hour=at[0], minute=at[1])
    assert mirror.safety_reason(document(), now, debit, config, event_blocked=blocked) == expected


def open_parallel(engine, strategy="ORB_15", *, legacy=False, closed=False, **overrides):
    paper = service(engine, **overrides)
    observation = raw(300, 25000)
    snapshot_id = capture(engine, observation)
    candidate = build_candidates(observation, _construction_signal(observation, "BULL"),
                                 paper.configs[strategy]).candidates[0]
    entry, failure = executable_pair(observation, candidate, paper.configs[strategy], entry=True)
    assert entry is not None and failure is None
    doc = document(strategy)
    doc.update(json_value({"strategy_version": VERSIONS[strategy], "trade_id": "mirror-test",
                           "policy": paper.policies[strategy], "policy_hash": paper.hashes[strategy],
                           "candidate": candidate, "decision": {"timestamp": observation.captured_at,
                           "direction": "BULL", "thesis": {}}, "decision_timestamp": observation.captured_at,
                           "entry_timestamp": observation.captured_at, "entry_credit": entry.value,
                           "entry_quotes": entry.evidence}))
    if legacy:
        doc.pop("exit_policy_version")
        doc["policy"] = {key: value for key, value in doc["policy"].items()
                         if key not in {"exit_policy", "exit_policy_version"}}
        doc["policy_hash"] = digest(doc["policy"])
    if closed:
        doc.update(exit_timestamp=observation.captured_at.isoformat(),
                   exit_reason="PROFIT_CAPTURE", final_outcome={"gross_rupees": 1})
    with paper.sessions.begin() as session:
        row = ParallelPaperTrade(id="mirror-test", strategy_id=strategy, strategy_version=VERSIONS[strategy],
                                 decision_snapshot_id=snapshot_id, state="CLOSED" if closed else "OPEN", document=canonical(doc))
        session.add(row)
        session.flush()
        paper._emit(session, session.get(ParallelPaperCursor, strategy), snapshot_id, observation,
                    "EXIT" if closed else "ENTRY", row)
    return paper, observation, candidate, doc


def stored(paper, model=ParallelPaperTrade):
    with paper.sessions() as session:
        row = session.get(model, "mirror-test") if model == ParallelPaperTrade else session.scalar(select(model))
        return row.state, json.loads(row.document)


def mark_raw(observation, candidate, seconds, debit, *, future=25000):
    updated = _next(observation, seconds=seconds, candidate=candidate,
                     short=(round(100 + debit - .05, 2), round(100 + debit, 2)), long=(100, 100.05))
    return updated.model_copy(update={"nifty_future": future})


@pytest.mark.parametrize("case", ["time", "profit", "trailing"])
def test_old_normal_exit_thresholds_only_record_diagnostics_and_remain_open(engine, case):
    paper, initial, candidate, doc = open_parallel(engine)
    credit = doc["entry_credit"]
    if case == "trailing":
        paper.process_snapshot(capture(engine, mark_raw(initial, candidate, 15, credit * .3)))
    seconds = 31 * 60 if case == "time" else 30
    debit = credit * .3 if case == "profit" else credit * .8
    paper.process_snapshot(capture(engine, mark_raw(initial, candidate, seconds, debit)))
    state, updated = stored(paper)
    assert state == "OPEN" and updated.get("exit_debit") is None
    key = {"time": "old_time_stop_at", "profit": "old_profit_capture_first_at",
           "trailing": "old_trailing_exit_first_at"}[case]
    assert key in updated["mirror_diagnostics"]
    verify_journals(paper.sessions)


@pytest.mark.parametrize("strategy", ["EMA_PULLBACK", "SUPERTREND_RSI_VOLUME"])
def test_completed_bar_mirror_closes_in_service_without_mutating_entry_cursor(engine, strategy):
    paper, initial, candidate, doc = open_parallel(engine, strategy)
    series = bars(-1, count=27, spike=100)
    normal_state = {}

    def observe():
        at = series[-1].end
        observation = mark_raw(initial, candidate, int((at - initial.captured_at).total_seconds()),
                               doc["entry_credit"], future=series[-1].close)
        market_points = [point(at, series[-1].close)]
        decide(strategy, market_points, series, at, normal_state, paper.experiment)
        paper._process(strategy, capture(engine, observation), observation, market_points, series, {})
        with paper.sessions() as session:
            cursor = session.get(ParallelPaperCursor, strategy)
            assert json.loads(cursor.state_json) == normal_state

    observe()
    assert stored(paper)[0] == "OPEN"
    if strategy == "EMA_PULLBACK":
        fast = ema([bar.close for bar in series], 9)[-1]
        previous = series[-1].close
        pullback = Bar(series[-1].end, series[-1].end + timedelta(minutes=3), previous,
                       fast + 3, previous - 3, fast, 100)
        series.append(pullback)
        observe()
        assert stored(paper)[0] == "OPEN"
        resumed = previous - 10
        series.append(Bar(pullback.end, pullback.end + timedelta(minutes=3), fast,
                          fast + 3, resumed - 3, resumed, 150))
    else:
        series.append(bars(-1, count=28, spike=150)[-1])
    observe()
    assert stored(paper)[0] == "CLOSED"
    assert stored(paper)[1]["exit_reason"] == (
        "OPPOSITE_EMA_PULLBACK_RESUMPTION" if strategy == "EMA_PULLBACK" else
        "OPPOSITE_SUPERTREND_RSI_VOLUME_SIGNAL")
    verify_journals(paper.sessions)


def open_acts(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(ActsPaperCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0,
                                    watch_json="{}", latest_json="{}"))
    paper = ActsPaperService(sessions, settings())
    history = qualified_rows(1, 70, "NEUTRAL")
    for row in history:
        snapshot_id = _capture(engine, row.snapshot)
    assert paper.process_snapshot(snapshot_id) == "PENDING_PAPER_ENTRY"
    candidate = stored(paper, ActsPaperTrade)[1]["candidate"]
    from app.scalper.models import ScalperCandidate
    selected = ScalperCandidate.model_validate(candidate)
    initial = _next(history[-1].snapshot, candidate=selected)
    # This labelled synthetic chain needs the 25,650 CE hedge for a 400-point
    # opposite spread at the bullish fixture's 25,207 spot. Never relax the
    # production builder or fall back to the old narrower hedge for these tests.
    call = next(quote for quote in initial.quotes if quote.option_type == "CE")
    initial = initial.model_copy(update={"quotes": [*initial.quotes, call.model_copy(update={
        "strike": 25650, "instrument_token": "25650CE", "trading_symbol": "NIFTY25650CE",
        "bid": 15, "ask": 16, "ltp": 15.5})]})
    paper.process_snapshot(_capture(engine, initial))
    assert stored(paper, ActsPaperTrade)[0] == "OPEN"
    return paper, initial, selected


def test_acts_service_execution_score_symmetry_no_portfolio_permission_or_reverse_entry(engine, monkeypatch):
    from app.acts_paper import service as acts_service
    paper, initial, candidate = open_acts(engine)
    ctx, _, _, _ = _decision(-1)
    ctx = execution_needed_context(ctx)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    def context(observation):
        value = deepcopy(ctx)
        value["timestamp"] = observation.snapshot.captured_at.isoformat()
        return value
    monkeypatch.setattr(paper, "_context", context)
    # These permissions prevent a new position, never evaluation of the exit.
    monkeypatch.setattr(acts_service, "_risk_blockers", lambda *_: [
        "MAX_OPEN_POSITIONS_REACHED", "MAX_TRADES_PER_DAY_REACHED", "LOSS_COOLDOWN_ACTIVE"])
    observation = mark_raw(initial, candidate, 15, credit)
    snapshot_id = _capture(engine, observation)
    paper.process_snapshot(snapshot_id)
    state, closed = stored(paper, ActsPaperTrade)
    assert state == "CLOSED"
    assert closed["exit_policy_version"] == mirror.VERSION
    assert closed["policy_hash"] == digest(closed["policy"])
    assert closed["policy"]["exit_policy"]["acts_confirmation_seconds"] == 0
    initial_draft = deepcopy(closed["policy"])
    initial_draft["exit_policy"] = mirror.policy(paper.config)
    assert closed["policy_hash"] != digest(initial_draft)
    assert closed["exit_reason"] == mirror.ACTS_REASON
    assert closed["exit_quotes"]["short"]["side"] == "ask"
    assert closed["exit_quotes"]["long"]["side"] == "bid"
    assert closed["exit_debit"] == pytest.approx(credit)
    assert closed["holding_seconds"] == 15
    actual = closed["mirror_exit_latest"]
    assert actual["score_components"]["execution"] > 0
    non_execution = actual["opportunity_score"] - actual["score_components"]["execution"]
    assert non_execution == pytest.approx(45)
    assert non_execution < 55 <= actual["opportunity_score"]
    assert acts_v2.decide(context(SimpleNamespace(snapshot=observation)), {}, None, {}, [], paper.v2_config)["status"] == "NO_SETUP"
    with paper.sessions() as session:
        assert len(session.scalars(select(ActsPaperTrade)).all()) == 1
    restarted = ActsPaperService(paper.sessions, settings())
    assert restarted.process_snapshot(snapshot_id) == "DUPLICATE"
    assert stored(restarted, ActsPaperTrade) == (state, closed)
    verify_acts_paper_journal(paper.sessions)


def test_acts_mirror_cannot_create_a_reverse_trade_even_without_portfolio_blockers(engine, monkeypatch):
    from app.acts_paper import service as acts_service
    paper, initial, candidate = open_acts(engine)
    ctx, _, _, _ = _decision(-1)
    observation = mark_raw(initial, candidate, 15, stored(paper, ActsPaperTrade)[1]["entry_credit"])
    ctx["timestamp"] = observation.captured_at.isoformat()
    monkeypatch.setattr(paper, "_context", lambda _: deepcopy(ctx))
    monkeypatch.setattr(acts_service, "_risk_blockers", lambda *_: [])
    paper.process_snapshot(_capture(engine, observation))
    assert stored(paper, ActsPaperTrade)[0] == "CLOSED"
    with paper.sessions() as session:
        assert len(session.scalars(select(ActsPaperTrade)).all()) == 1
        latest = json.loads(session.get(ActsPaperCursor, 1).latest_json)
        assert latest["primary_blocker"] == "MIRROR_EXIT_OBSERVATION_NO_REENTRY"
    verify_acts_paper_journal(paper.sessions)


def test_acts_opposite_signal_waits_for_held_executable_book_and_survives_restart(engine, monkeypatch):
    paper, initial, held = open_acts(engine)
    ctx, _, _, _ = _decision(-1)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    observation = mark_raw(initial, held, 15, credit)
    observation = observation.model_copy(update={"quotes": [quote.model_copy(update={
        "source_market_timestamp": initial.captured_at - timedelta(seconds=60)})
        if quote.instrument_token == held.short_leg.instrument_token else quote
        for quote in observation.quotes]})
    ctx["timestamp"] = observation.captured_at.isoformat()
    monkeypatch.setattr(paper, "_context", lambda _: deepcopy(ctx))
    paper.process_snapshot(_capture(engine, observation))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "OPEN" and doc["mirror_exit_request"]["reason"] == mirror.ACTS_REASON
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    evidence = doc["mirror_diagnostics"]["evidence"]["opposite_full_entry_signal_first_at"]
    assert evidence["mark_available"] is False and evidence["gross_pnl"] is None
    paper = ActsPaperService(paper.sessions, settings())
    fresh = mark_raw(initial, held, 30, credit)
    paper.process_snapshot(_capture(engine, fresh))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "CLOSED" and doc["exit_reason"] == mirror.ACTS_REASON
    assert doc["exit_timestamp"] == fresh.captured_at.isoformat()
    assert doc["exit_debit"] == pytest.approx(credit)
    verify_acts_paper_journal(paper.sessions)


def test_acts_mirror_rejects_context_from_a_different_observation(engine):
    paper, initial, _ = open_acts(engine)
    doc, trade, ctx, _, _, _ = acts_case()
    ctx["timestamp"] = (initial.captured_at + timedelta(seconds=15)).isoformat()
    with pytest.raises(ValueError, match="ACTS_MIRROR_OBSERVATION_MISMATCH"):
        paper._mirror_signal(doc, trade, initial, ctx, available=True)


def test_acts_builds_opposite_candidate_from_current_observed_book(engine, monkeypatch):
    paper, initial, held = open_acts(engine)
    ctx, _, _, _ = _decision(-1)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    def context(observation):
        value = deepcopy(ctx)
        value["timestamp"] = observation.snapshot.captured_at.isoformat()
        return value
    monkeypatch.setattr(paper, "_context", context)
    invalid = mark_raw(initial, held, 15, credit)
    invalid = invalid.model_copy(update={"quotes": [quote.model_copy(update={
        "bid_quantity": 0, "ask_quantity": 0}) if quote.option_type == "CE" else quote
        for quote in invalid.quotes]})
    paper.process_snapshot(_capture(engine, invalid))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "OPEN" and "mirror_exit_request" not in doc
    assert doc["mirror_exit_latest"]["signal_candidate"] is None
    assert doc["mirror_exit_latest"]["candidate_rejections"]["INSUFFICIENT_DEPTH"] > 0
    valid = mark_raw(initial, held, 30, credit)
    paper.process_snapshot(_capture(engine, valid))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "CLOSED" and doc["exit_reason"] == mirror.ACTS_REASON
    opposite = doc["mirror_exit_request"]["signal"]["signal_candidate"]
    assert opposite["short_leg"]["option_type"] == "CE"
    assert opposite["quote_evidence"]["short"]["source_market_timestamp"] == valid.captured_at.isoformat()
    assert opposite["construction_method"] == "SHORT_LEG_FIRST_V1"
    verify_acts_paper_journal(paper.sessions)


@pytest.mark.parametrize("safety", ["spread", "event", "forced"])
def test_acts_no_opposite_candidate_holds_until_independent_safety_exit(engine, monkeypatch, safety):
    from app.acts_paper import service as acts_service
    paper, initial, candidate = open_acts(engine)
    ctx, _, _, _ = _decision(-1)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    def context(observation):
        value = deepcopy(ctx)
        value["timestamp"] = observation.snapshot.captured_at.isoformat()
        return value
    monkeypatch.setattr(paper, "_context", context)
    monkeypatch.setattr(acts_service, "_candidate", lambda *_: (None, {"STALE_QUOTE": 1}))
    paper.process_snapshot(_capture(engine, mark_raw(initial, candidate, 15, credit)))
    assert stored(paper, ActsPaperTrade)[0] == "OPEN"
    assert "NO_EXECUTABLE_CANDIDATE" in stored(paper, ActsPaperTrade)[1]["mirror_exit_latest"]["blockers"]
    seconds = int((initial.captured_at.replace(hour=15, minute=20, second=0) - initial.captured_at).total_seconds()) if safety == "forced" else 30
    if safety == "event":
        monkeypatch.setattr(paper.events, "events_at", lambda _: [SimpleNamespace(block_entries=True)])
    paper.process_snapshot(_capture(engine, mark_raw(initial, candidate, seconds, credit * (1.6 if safety == "spread" else 1))))
    assert stored(paper, ActsPaperTrade)[0] == "CLOSED"
    assert stored(paper, ActsPaperTrade)[1]["exit_reason"] == {
        "spread": "SPREAD_STOP", "event": "MARKET_EVENT_EXIT", "forced": "FORCED_CLOSE"}[safety]
    verify_acts_paper_journal(paper.sessions)


def test_acts_service_legacy_normal_thresholds_hold_but_stop_still_closes(engine):
    paper, initial, candidate = open_acts(engine)
    credit = stored(paper, ActsPaperTrade)[1]["entry_credit"]
    paper.process_snapshot(_capture(engine, mark_raw(initial, candidate, 15, credit * .3)))
    paper.process_snapshot(_capture(engine, mark_raw(initial, candidate, 31 * 60, credit * .8)))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "OPEN"
    assert {"old_profit_capture_first_at", "old_trailing_exit_first_at", "old_time_stop_at"} <= doc["mirror_diagnostics"].keys()
    paper.process_snapshot(_capture(engine, mark_raw(initial, candidate, 31 * 60 + 15, credit * 1.6)))
    assert stored(paper, ActsPaperTrade)[1]["exit_reason"] == "SPREAD_STOP"
    assert stored(paper, ActsPaperTrade)[0] == "CLOSED"
    verify_acts_paper_journal(paper.sessions)


def test_acts_new_policy_missing_book_at_forced_close_stays_unresolved(engine):
    paper, initial, candidate = open_acts(engine)
    seconds = int((initial.captured_at.replace(hour=15, minute=20, second=0) - initial.captured_at).total_seconds())
    observation = _next(initial, candidate=candidate, seconds=seconds, missing="long_leg")
    paper.process_snapshot(_capture(engine, observation))
    state, doc = stored(paper, ActsPaperTrade)
    assert state == "UNRESOLVED_EXPOSURE"
    assert doc.get("exit_debit") is None and doc.get("final_outcome") is None
    verify_acts_paper_journal(paper.sessions)


@pytest.mark.parametrize("closed", [False, True])
def test_upgrade_does_not_reinterpret_legacy_trades_or_weaken_hash_guard(engine, closed):
    paper, initial, candidate, original = open_parallel(engine, legacy=True, closed=closed)
    restarted = ParallelPaperService(paper.sessions, settings())
    if closed:
        assert stored(restarted) == ("CLOSED", original)
    else:
        restarted.process_snapshot(capture(engine, mark_raw(initial, candidate, 15, original["entry_credit"])))
        state, doc = stored(restarted)
        assert state == "UNRESOLVED_EXPOSURE"
        assert doc["unresolved_reason"] == "EXECUTION_POLICY_CHANGED"
        assert doc["policy"] == original["policy"]
        assert "exit_policy_version" not in doc
        assert doc.get("final_outcome") is None
    verify_journals(restarted.sessions)


def test_frozen_base_commit_entries_and_candidates_remain_unchanged():
    # Digests captured from unchanged code at 417bc9a, not new exit outcomes.
    decisions = {strategy: decide(strategy, points(60), bars(), bars()[-1].end, {}, ExperimentConfig())
                 for strategy in VERSIONS}
    assert digest(decisions) == "4886c1680129a114d3a41937a7ae2e2b689a4ff6948eaa01870e2ce3abe8b7b0"
    ctx, candidate, rejected, config = _decision()
    assert digest(acts_v2.decide(ctx, {}, candidate, rejected, [], config)) == (
        "f14e7cc3b4769eac928a120bb4cf9af4b13e061defa55832824114285db22b93")
    assert digest(candidate.model_dump(mode="json")) == "245423ff3842db898ecbd894e2b879f93d8779561880603da35f2042bbd39c5f"


def test_confirmed_orb_exit_survives_restart_unusable_book_and_is_idempotent(engine):
    paper, initial, candidate, doc = open_parallel(engine)
    credit = doc["entry_credit"]
    for offset in (15, 30):
        paper.process_snapshot(capture(engine, mark_raw(initial, candidate, offset, credit, future=24980)))
    # Full confirmation occurs with a stale (not missing) exact book.
    unavailable = mark_raw(initial, candidate, 45, credit, future=24980)
    unavailable = unavailable.model_copy(update={"quotes": [quote.model_copy(update={
        "source_market_timestamp": initial.captured_at - timedelta(seconds=60)}) for quote in unavailable.quotes]})
    paper.process_snapshot(capture(engine, unavailable))
    state, pending = stored(paper)
    assert state == "OPEN" and pending["mirror_exit_request"]["reason"] == "OPPOSITE_ORB_BREAKOUT"
    assert pending.get("exit_debit") is None and pending.get("final_outcome") is None
    restarted = ParallelPaperService(paper.sessions, settings())
    fresh = mark_raw(initial, candidate, 60, credit * .8, future=25000)
    snapshot_id = capture(engine, fresh)
    restarted.process_snapshot(snapshot_id)
    state, closed = stored(restarted)
    assert state == "CLOSED" and closed["exit_reason"] == "OPPOSITE_ORB_BREAKOUT"
    assert closed["exit_quotes"]["short"]["side"] == "ask"
    assert closed["exit_quotes"]["long"]["side"] == "bid"
    assert closed["exit_debit"] == pytest.approx(closed["exit_quotes"]["short"]["fill_price"] -
                                                closed["exit_quotes"]["long"]["fill_price"])
    assert restarted.process_snapshot(snapshot_id)["ORB_15"] == "DUPLICATE"
    assert stored(restarted) == (state, closed)
    verify_journals(restarted.sessions)


@pytest.mark.parametrize("kind", ["spread", "kill", "event", "eod", "missing"])
def test_parallel_safety_closes_or_preserves_unresolved_without_fabrication(engine, kind):
    overrides = {"scalper_kill_switch": True} if kind == "kill" else {}
    if kind == "event":
        overrides["scalper_market_events_json"] = json.dumps([{
            "name": "test", "start_time": START.isoformat(), "end_time": START.replace(hour=15).isoformat(),
            "severity": "HIGH", "block_entries": True}])
    paper, initial, candidate, doc = open_parallel(engine, **overrides)
    seconds = int((initial.captured_at.replace(hour=15, minute=20) - initial.captured_at).total_seconds()) if kind == "eod" else 15
    observation = mark_raw(initial, candidate, seconds, doc["entry_credit"] * (1.6 if kind == "spread" else 1))
    if kind == "missing":
        observation = observation.model_copy(update={"quotes": [quote for quote in observation.quotes
            if quote.instrument_token != candidate.long_leg.instrument_token]})
    paper.process_snapshot(capture(engine, observation))
    state, result = stored(paper)
    if kind == "missing":
        assert state == "UNRESOLVED_EXPOSURE"
        assert result.get("exit_debit") is None and result.get("final_outcome") is None
    else:
        assert state == "CLOSED"
        assert result["exit_reason"] == {"spread": "SPREAD_STOP", "kill": "KILL_SWITCH",
                                         "event": "MARKET_EVENT_EXIT", "eod": "FORCED_CLOSE"}[kind]
    assert result["policy_hash"] == digest(result["policy"])
    verify_journals(paper.sessions)
