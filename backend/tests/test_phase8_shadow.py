from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.api.shadow import get_shadow_repository
from app.collector.service import collection_bucket
from app.db.models import ShadowTradeMarkRecord, ShadowTradeRecord
from app.main import app
from app.risk.models import RiskDecisionType
from app.risk.service import build_and_store_risk
from app.shadow.analytics import breakdown, performance
from app.shadow.entry import create_shadow_entry
from app.shadow.exits import ShadowConfig
from app.shadow.lifecycle import update_trade
from app.shadow.models import ShadowPricingBasis, ShadowStatus
from app.shadow.repository import ShadowRepository
from app.shadow.service import build_shadow_entry, replay_all, update_open_trades
from app.shadow.valuation import create_mark, value_trade
from tests.test_phase7_risk import approved_config, persisted_context, risk_context, run_risk
from app.risk.models import EvaluationContext


def shadow_context(market_snapshot, direction="BULLISH", quotes=True):
    result, raw, feature, regime, candidates = run_risk(
        market_snapshot, direction, quotes=quotes
    )
    approved = [
        (index + 1, item) for index, item in enumerate(result.decisions)
        if item.decision == RiskDecisionType.APPROVED
    ]
    return raw, feature, regime, candidates, approved


def entry_trade(market_snapshot, direction="BULLISH", quotes=True):
    raw, feature, regime, candidates, approved = shadow_context(
        market_snapshot, direction, quotes
    )
    result = create_shadow_entry(
        1, raw, feature, regime, candidates, approved,
        trades_today=0, open_trade_exists=False,
        max_new_trades_per_day=1, allow_multiple_open_trades=False,
    )
    assert result.created and result.trade
    return result.trade, raw, feature, regime, candidates, approved


def marked_snapshot(raw, trade, short_close, long_close, *, ltp=False, minutes=3, spot=None):
    options = []
    for item in raw.options:
        if item.option_type.value == trade.short_leg.option_type and item.strike == trade.short_leg.strike:
            update = ({"bid": None, "ask": None, "ltp": short_close} if ltp
                      else {"ask": short_close})
            item = item.model_copy(update=update)
        if item.option_type.value == trade.long_leg.option_type and item.strike == trade.long_leg.strike:
            update = ({"bid": None, "ask": None, "ltp": long_close} if ltp
                      else {"bid": long_close})
            item = item.model_copy(update=update)
        options.append(item)
    return raw.model_copy(update={
        "timestamp_ist": raw.timestamp_ist + timedelta(minutes=minutes),
        "nifty_spot": raw.nifty_spot if spot is None else spot,
        "options": options,
    })


def test_only_approved_decision_creates_entry(market_snapshot):
    trade, *_ = entry_trade(market_snapshot)
    assert trade.status == ShadowStatus.OPEN
    assert "SHADOW_ENTRY_CREATED" in trade.reason_codes
    raw, feature, regime, candidates, approved = shadow_context(market_snapshot)
    rejected = approved[0][1].model_copy(update={"decision": RiskDecisionType.REJECTED})
    result = create_shadow_entry(1, raw, feature, regime, candidates, [(1, rejected)],
                                 trades_today=0, open_trade_exists=False,
                                 max_new_trades_per_day=1, allow_multiple_open_trades=False)
    assert not result.created and result.reason_codes == ["NO_APPROVED_RISK_DECISION"]


def test_best_phase6_ranked_approved_candidate_selected(market_snapshot):
    trade, _, _, _, candidates, _ = entry_trade(market_snapshot)
    expected = max(candidates.candidates, key=lambda item: item.selection_score)
    assert trade.source_candidate["candidate_id"] == expected.candidate_id


@pytest.mark.parametrize(
    ("trades_today", "open_exists", "reason"),
    [(1, False, "DAILY_SHADOW_LIMIT_REACHED"), (0, True, "SHADOW_POSITION_ALREADY_OPEN")],
)
def test_daily_and_open_trade_protection(market_snapshot, trades_today, open_exists, reason):
    raw, feature, regime, candidates, approved = shadow_context(market_snapshot)
    result = create_shadow_entry(1, raw, feature, regime, candidates, approved,
                                 trades_today=trades_today, open_trade_exists=open_exists,
                                 max_new_trades_per_day=1, allow_multiple_open_trades=False)
    assert not result.created and reason in result.reason_codes


@pytest.mark.parametrize("quotes", [True, False])
def test_entry_pricing_basis_is_exact_phase6_basis(market_snapshot, quotes):
    trade, *_ = entry_trade(market_snapshot, quotes=quotes)
    expected = ShadowPricingBasis.BID_ASK if quotes else ShadowPricingBasis.LTP_ESTIMATE
    assert trade.entry_pricing_basis == expected
    assert trade.entry_credit == pytest.approx(trade.entry_short_price - trade.entry_long_price)


def test_missing_entry_prices_reject(market_snapshot):
    raw, feature, regime, candidates, approved = shadow_context(market_snapshot)
    candidate = candidates.candidates[0]
    candidate = candidate.model_copy(update={
        "short_leg": candidate.short_leg.model_copy(update={"bid": None, "ltp": None}),
        "long_leg": candidate.long_leg.model_copy(update={"ask": None, "ltp": None}),
    })
    candidates = candidates.model_copy(update={"candidates": [candidate], "candidate_count": 1})
    approved = [(1, approved[0][1].model_copy(update={"candidate_reference": candidate.candidate_id}))]
    result = create_shadow_entry(1, raw, feature, regime, candidates, approved,
                                 trades_today=0, open_trade_exists=False,
                                 max_new_trades_per_day=1, allow_multiple_open_trades=False)
    assert not result.created and "VALUATION_UNAVAILABLE" in result.reason_codes


@pytest.mark.parametrize(("direction", "quotes"), [("BULLISH", True), ("BEARISH", True), ("BULLISH", False)])
def test_exact_leg_valuation_and_pnl(market_snapshot, direction, quotes):
    trade, raw, *_ = entry_trade(market_snapshot, direction, quotes)
    later = marked_snapshot(raw, trade, 30, 10, ltp=not quotes)
    valuation = value_trade(trade, later)
    assert valuation is not None
    expected_basis = ShadowPricingBasis.BID_ASK if quotes else ShadowPricingBasis.LTP_ESTIMATE
    assert valuation.basis == expected_basis
    mark = create_mark(trade, 2, later, valuation)
    assert mark.exit_debit == 20
    assert mark.pnl_per_unit == pytest.approx(trade.entry_credit - 20)
    assert mark.pnl_per_lot == pytest.approx(mark.pnl_per_unit * trade.lot_size)


def test_bidask_entry_does_not_fallback_to_ltp_and_missing_leg_is_unavailable(market_snapshot):
    trade, raw, *_ = entry_trade(market_snapshot)
    no_quotes = raw.model_copy(update={
        "timestamp_ist": raw.timestamp_ist + timedelta(minutes=3),
        "options": [item.model_copy(update={"bid": None, "ask": None}) for item in raw.options],
    })
    assert value_trade(trade, no_quotes) is None
    missing = no_quotes.model_copy(update={
        "options": [item for item in no_quotes.options if not (
            item.expiry == trade.short_leg.expiry
            and item.option_type.value == trade.short_leg.option_type
            and item.strike == trade.short_leg.strike
        )]
    })
    assert value_trade(trade, missing) is None


def test_expiry_or_nearby_strike_is_never_substituted(market_snapshot):
    trade, raw, *_ = entry_trade(market_snapshot)
    wrong = raw.model_copy(update={
        "timestamp_ist": raw.timestamp_ist + timedelta(minutes=3),
        "options": [item for item in raw.options if item.strike != trade.short_leg.strike],
    })
    assert value_trade(trade, wrong) is None


def test_mfe_mae_update_across_multiple_marks(market_snapshot):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1})
    favorable = marked_snapshot(raw, trade, 5, 1, minutes=3)
    first = update_trade(trade, 2, favorable, regime,
                         ShadowConfig(profit_target_credit_capture_pct=100))
    adverse = marked_snapshot(raw, trade, 90, 10, minutes=6)
    second = update_trade(first.trade, 3, adverse, regime,
                          ShadowConfig(profit_target_credit_capture_pct=100,
                                       stop_loss_credit_multiple=100))
    assert first.trade.mfe_per_unit > 0
    assert second.trade.mfe_per_unit == first.trade.mfe_per_unit
    assert second.trade.mae_per_unit < 0
    assert second.trade.mae_per_lot == pytest.approx(second.trade.mae_per_unit * trade.lot_size)


def test_missing_lot_has_only_per_unit_pnl(market_snapshot):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1, "lot_size": None})
    later = marked_snapshot(raw, trade, 20, 10)
    result = update_trade(trade, 2, later, regime, ShadowConfig())
    assert result.mark.pnl_per_lot is None


@pytest.mark.parametrize(
    ("short_close", "long_close", "config", "reason"),
    [
        (5, 1, ShadowConfig(), "PROFIT_TARGET_EXIT"),
        (100, 5, ShadowConfig(), "STOP_LOSS_EXIT"),
    ],
)
def test_profit_and_stop_exits(market_snapshot, short_close, long_close, config, reason):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1})
    result = update_trade(trade, 2, marked_snapshot(raw, trade, short_close, long_close), regime, config)
    assert result.trade.status == ShadowStatus.CLOSED
    assert result.trade.exit_reason == reason
    assert result.trade.realized_pnl_per_unit == result.mark.pnl_per_unit


def test_opposite_regime_and_structural_exit(market_snapshot):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1})
    neutral_mark = marked_snapshot(raw, trade, 50, 10)
    opposite = regime.model_copy(update={"regime": "BEARISH"})
    result = update_trade(trade, 2, neutral_mark, opposite,
                          ShadowConfig(profit_target_credit_capture_pct=100,
                                       stop_loss_credit_multiple=100))
    assert result.trade.exit_reason == "OPPOSITE_REGIME_EXIT"
    breach = marked_snapshot(raw, trade, 50, 10, spot=trade.structural_reference - 1)
    result = update_trade(trade, 3, breach, regime,
                          ShadowConfig(profit_target_credit_capture_pct=100,
                                       stop_loss_credit_multiple=100,
                                       exit_on_opposite_regime=False))
    assert result.trade.exit_reason == "STRUCTURAL_INVALIDATION_EXIT"


def test_time_exit_and_exit_only_once(market_snapshot):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1})
    minutes = (15 * 60 + 20) - (raw.timestamp_ist.hour * 60 + raw.timestamp_ist.minute)
    later = marked_snapshot(raw, trade, 50, 10, minutes=minutes)
    closed = update_trade(trade, 2, later, regime,
                          ShadowConfig(profit_target_credit_capture_pct=100,
                                       stop_loss_credit_multiple=100))
    assert closed.trade.exit_reason == "TIME_EXIT"
    again = update_trade(closed.trade, 3, later.model_copy(update={
        "timestamp_ist": later.timestamp_ist + timedelta(minutes=1)
    }), regime, ShadowConfig())
    assert not again.changed and again.mark is None


def test_unresolved_expiry_becomes_invalid(market_snapshot):
    trade, raw, _, regime, *_ = entry_trade(market_snapshot)
    trade = trade.model_copy(update={"id": 1})
    expiry_snapshot = raw.model_copy(update={
        "timestamp_ist": raw.timestamp_ist.replace(
            year=trade.expiry.year, month=trade.expiry.month, day=trade.expiry.day
        )
    })
    result = update_trade(trade, 2, expiry_snapshot, regime, ShadowConfig())
    assert result.trade.status == ShadowStatus.INVALID
    assert "EXPIRY_EXIT_UNRESOLVED" in result.trade.reason_codes


def test_analytics_formulas_and_breakdowns(market_snapshot):
    trade, *_ = entry_trade(market_snapshot)
    win = trade.model_copy(update={"status": ShadowStatus.CLOSED, "realized_pnl_per_unit": 10,
                                   "realized_pnl_per_lot": 500, "holding_minutes": 30,
                                   "mfe_per_unit": 12, "mae_per_unit": -2})
    loss = trade.model_copy(update={"candidate_fingerprint": "other", "status": ShadowStatus.CLOSED,
                                    "realized_pnl_per_unit": -5, "realized_pnl_per_lot": -250,
                                    "holding_minutes": 60, "mfe_per_unit": 2, "mae_per_unit": -7})
    metrics = performance([win, loss])
    assert metrics["per_unit"]["wins"] == 1 and metrics["per_unit"]["losses"] == 1
    assert metrics["per_unit"]["win_rate"] == 50
    assert metrics["per_unit"]["net_pnl"] == 5
    assert metrics["per_unit"]["profit_factor"] == 2
    assert metrics["per_unit"]["expectancy_per_trade"] == 2.5
    assert metrics["per_unit"]["max_drawdown"] == 5
    groups = breakdown([win, loss])
    assert "BULL_PUT_SPREAD" in groups["strategy_type"]
    assert (trade.entry_vix_regime or "UNAVAILABLE") in groups["vix_regime"]


def persisted_shadow_context(repository, session_factory, market_snapshot):
    snapshot_id, risk_repository = persisted_context(repository, session_factory, market_snapshot)
    build_and_store_risk(risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
    return snapshot_id, ShadowRepository(session_factory)


def test_persistence_api_and_idempotent_entry(repository, session_factory, market_snapshot):
    snapshot_id, shadow_repository = persisted_shadow_context(repository, session_factory, market_snapshot)
    first = build_shadow_entry(shadow_repository, snapshot_id, ShadowConfig())
    second = build_shadow_entry(shadow_repository, snapshot_id, ShadowConfig())
    with session_factory() as session:
        count = session.scalar(select(func.count(ShadowTradeRecord.id)))
    assert first.created and not second.created and count == 1
    app.dependency_overrides[get_shadow_repository] = lambda: shadow_repository
    try:
        client = TestClient(app)
        trade_id = first.trade.id
        assert client.get("/api/shadow/latest").status_code == 200
        assert len(client.get("/api/shadow/trades").json()) == 1
        assert client.get(f"/api/shadow/trades/{trade_id}").status_code == 200
        assert client.get(f"/api/shadow/trades/{trade_id}/marks").json() == []
        assert client.get("/api/shadow/performance").json()["total_trades"] == 1
    finally:
        app.dependency_overrides.clear()


def test_mark_persistence_and_replay_idempotency(repository, session_factory, market_snapshot):
    snapshot_id, shadow_repository = persisted_shadow_context(repository, session_factory, market_snapshot)
    entry = build_shadow_entry(shadow_repository, snapshot_id, ShadowConfig())
    trade = entry.trade
    entry_raw = shadow_repository.load_snapshot_context(snapshot_id)[0]
    later = marked_snapshot(entry_raw, trade, 20, 10)
    run_id = repository.create_collector_run(later.timestamp_ist)
    saved = repository.save_market_snapshot(later, collection_bucket(later.timestamp_ist, 3), run_id)
    update_open_trades(shadow_repository, saved.snapshot_id, ShadowConfig())
    update_open_trades(shadow_repository, saved.snapshot_id, ShadowConfig())
    with session_factory() as session:
        marks = session.scalar(select(func.count(ShadowTradeMarkRecord.id)))
    assert marks == 1
    before = shadow_repository.get_trade(trade.id)
    replay_all(shadow_repository, ShadowConfig())
    replay_all(shadow_repository, ShadowConfig())
    assert shadow_repository.get_trade(trade.id) == before


def test_no_approved_snapshot_creates_no_shadow_trade(repository, session_factory, market_snapshot):
    snapshot_id, risk_repository = persisted_context(
        repository, session_factory, market_snapshot, "NO_TRADE"
    )
    build_and_store_risk(risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
    shadow_repository = ShadowRepository(session_factory)
    result = build_shadow_entry(shadow_repository, snapshot_id, ShadowConfig())
    assert not result.created and "NO_APPROVED_RISK_DECISION" in result.reason_codes
