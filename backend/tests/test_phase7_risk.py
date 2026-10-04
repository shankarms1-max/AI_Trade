from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.api.risk import get_risk_repository
from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.collector.service import collection_bucket
from app.db.models import RiskDecisionRecord
from app.features.repository import FeatureRepository
from app.main import app
from app.regime.repository import RegimeRepository
from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider, MarketEvent
from app.risk.exposure import ResearchRiskStateProvider, RiskState
from app.risk.limits import RiskConfig
from app.risk.models import EvaluationContext, RiskDecisionType
from app.risk.repository import RiskRepository
from app.risk.service import backfill_risk, build_and_store_risk
from app.risk.candidate_checks import strategy_fingerprint
from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.models import PricingBasis, StrategyType
from app.strategy.repository import StrategyRepository
from tests.test_phase6_strategy import phase6_context

IST = ZoneInfo("Asia/Kolkata")


class StateProvider:
    def __init__(self, trades=0, pnl=0.0, keys=frozenset(), authoritative=True):
        self.state = RiskState(trades, pnl, frozenset(keys), authoritative)

    def get_state(self, trading_date):
        return self.state


def approved_config(**updates) -> RiskConfig:
    base = RiskConfig(
        max_loss_per_trade=100_000,
        max_capital_per_trade=100_000,
        required_consecutive_directional_snapshots=1,
    )
    return RiskConfig(**(base.__dict__ | updates))


def risk_context(market_snapshot, direction="BULLISH", *, quotes=True):
    raw, feature, regime = phase6_context(market_snapshot, direction)
    raw = raw.model_copy(update={"lot_size": 50})
    if not quotes:
        raw = raw.model_copy(update={
            "options": [item.model_copy(update={"bid": None, "ask": None}) for item in raw.options]
        })
    candidate_set = generate_candidates(raw, feature, regime, 10, StrategyConfig())
    return raw, feature, regime, candidate_set


def run_risk(market_snapshot, direction="BULLISH", *, config=None, quotes=True,
             context=EvaluationContext.HISTORICAL, state=None, events=None,
             prior=None, evaluated_at=None, candidate_set_update=None):
    raw, feature, regime, candidate_set = risk_context(market_snapshot, direction, quotes=quotes)
    if candidate_set_update:
        candidate_set = candidate_set.model_copy(update=candidate_set_update)
    result = evaluate_risk(
        candidate_set, 99, raw, feature, regime, config or approved_config(), context,
        state or ResearchRiskStateProvider(), events or ConfiguredMarketEventProvider(),
        prior_regimes=prior or [], evaluated_at=evaluated_at,
    )
    return result, raw, feature, regime, candidate_set


def check(result, code):
    return next(item for item in result.decisions[0].checks if item.check_code == code)


def test_no_candidate_is_not_applicable(market_snapshot):
    result, *_ = run_risk(market_snapshot, "NO_TRADE")
    assert result.not_applicable and result.candidate_count == 0
    assert result.decisions[0].decision == RiskDecisionType.NOT_APPLICABLE
    assert result.decisions[0].reason_codes == ["NO_STRATEGY_CANDIDATE"]


@pytest.mark.parametrize("direction", ["BULLISH", "BEARISH"])
def test_valid_defined_risk_spreads_are_approved(market_snapshot, direction):
    result, *_ = run_risk(market_snapshot, direction)
    assert result.approved_count == result.candidate_count > 0
    assert check(result, "DEFINED_RISK_STRUCTURE").status == "PASS"


@pytest.mark.parametrize("mutation", ["naked", "order", "expiry"])
def test_invalid_structure_is_rejected(market_snapshot, mutation):
    _, raw, feature, regime, candidate_set = run_risk(market_snapshot)
    candidate = candidate_set.candidates[0]
    if mutation == "naked":
        broken = candidate.model_copy(update={
            "long_leg": candidate.long_leg.model_copy(update={"action": "SELL"})
        })
    elif mutation == "order":
        broken = candidate.model_copy(update={
            "long_leg": candidate.long_leg.model_copy(update={"strike": candidate.short_leg.strike + 50})
        })
    else:
        broken = candidate.model_copy(update={
            "long_leg": candidate.long_leg.model_copy(update={"expiry": candidate.expiry + timedelta(days=7)})
        })
    candidate_set = candidate_set.model_copy(update={"candidates": [broken], "candidate_count": 1})
    result = evaluate_risk(candidate_set, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider())
    assert result.decisions[0].decision == RiskDecisionType.REJECTED
    assert "INVALID_SPREAD_STRUCTURE" in result.decisions[0].reason_codes


@pytest.mark.parametrize(
    ("regime_update", "reason"),
    [
        ({"confidence": 50}, "REGIME_CONFIDENCE_TOO_LOW"),
        ({"evidence_quality": "LOW"}, "EVIDENCE_QUALITY_TOO_LOW"),
        ({"regime": "BEARISH"}, "REGIME_NOT_DIRECTIONAL"),
    ],
)
def test_regime_gates_reject(market_snapshot, regime_update, reason):
    _, raw, feature, regime, candidates = run_risk(market_snapshot)
    regime = regime.model_copy(update=regime_update)
    result = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider())
    assert reason in result.decisions[0].reason_codes
    assert result.approved_count == 0


def test_payoff_and_lot_level_values_use_broker_lot(market_snapshot):
    result, raw, *_ = run_risk(market_snapshot)
    decision = result.decisions[0]
    assert decision.max_loss_per_unit == pytest.approx(
        decision.checks[next(i for i, c in enumerate(decision.checks) if c.check_code == "MAX_LOSS_VALID")].actual_value
    )
    assert decision.max_profit_per_unit > 0
    assert decision.lot_size == raw.lot_size == 50
    assert decision.max_loss_per_lot == pytest.approx(decision.max_loss_per_unit * 50)
    assert decision.estimated_capital_required == decision.max_loss_per_lot
    assert decision.capital_basis == "DEFINED_RISK_MAX_LOSS_PROXY"


def test_missing_lot_and_unconfigured_limits_fail_safely(market_snapshot):
    _, raw, feature, regime, candidates = run_risk(market_snapshot)
    raw = raw.model_copy(update={"lot_size": None})
    result = evaluate_risk(candidates, 99, raw, feature, regime, RiskConfig(
        required_consecutive_directional_snapshots=1
    ), EvaluationContext.HISTORICAL, ResearchRiskStateProvider(), ConfiguredMarketEventProvider())
    reasons = result.decisions[0].reason_codes
    assert "LOT_SIZE_UNAVAILABLE" in reasons
    assert "RISK_LIMIT_UNCONFIGURED" in reasons
    assert result.approved_count == 0


@pytest.mark.parametrize(
    ("limit_name", "limit", "reason"),
    [
        ("max_loss_per_trade", 1, "MAX_LOSS_EXCEEDED"),
        ("max_capital_per_trade", 1, "MAX_CAPITAL_EXCEEDED"),
    ],
)
def test_monetary_limits_veto(market_snapshot, limit_name, limit, reason):
    result, *_ = run_risk(market_snapshot, config=approved_config(**{limit_name: limit}))
    assert reason in result.decisions[0].reason_codes
    assert result.approved_count == 0


def test_ltp_estimate_warns_or_fails_by_policy(market_snapshot):
    warned, *_ = run_risk(market_snapshot, quotes=False)
    assert warned.decisions[0].decision == RiskDecisionType.APPROVED
    assert "NON_EXECUTABLE_PRICING_ESTIMATE" in warned.decisions[0].warnings
    failed, *_ = run_risk(market_snapshot, quotes=False,
                          config=approved_config(require_bid_ask=True))
    assert failed.approved_count == 0
    assert "BID_ASK_REQUIRED" in failed.decisions[0].reason_codes


@pytest.mark.parametrize(
    ("leg", "field", "value", "reason"),
    [
        ("short_leg", "open_interest", 0, "SHORT_OI_TOO_LOW"),
        ("long_leg", "open_interest", 0, "LONG_OI_TOO_LOW"),
        ("short_leg", "volume", 0, "SHORT_VOLUME_TOO_LOW"),
        ("long_leg", "volume", 0, "LONG_VOLUME_TOO_LOW"),
    ],
)
def test_liquidity_rechecked_independently(market_snapshot, leg, field, value, reason):
    _, raw, feature, regime, candidate_set = run_risk(market_snapshot)
    candidate = candidate_set.candidates[0]
    changed_leg = getattr(candidate, leg).model_copy(update={field: value})
    changed = candidate.model_copy(update={leg: changed_leg})
    candidate_set = candidate_set.model_copy(update={"candidates": [changed], "candidate_count": 1})
    result = evaluate_risk(candidate_set, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider())
    assert reason in result.decisions[0].reason_codes


def test_volume_requirement_can_be_disabled(market_snapshot):
    _, raw, feature, regime, candidate_set = run_risk(market_snapshot)
    candidate = candidate_set.candidates[0]
    changed = candidate.model_copy(update={
        "short_leg": candidate.short_leg.model_copy(update={"volume": None}),
        "long_leg": candidate.long_leg.model_copy(update={"volume": None}),
    })
    candidate_set = candidate_set.model_copy(update={"candidates": [changed], "candidate_count": 1})
    result = evaluate_risk(candidate_set, 99, raw, feature, regime,
                           approved_config(require_volume=False), EvaluationContext.HISTORICAL,
                           ResearchRiskStateProvider(), ConfiguredMarketEventProvider())
    assert result.approved_count == 1


def test_bid_ask_spread_rejected(market_snapshot):
    _, raw, feature, regime, candidate_set = run_risk(market_snapshot)
    candidate = candidate_set.candidates[0]
    changed = candidate.model_copy(update={
        "short_leg": candidate.short_leg.model_copy(update={"bid": 10, "ask": 20})
    })
    candidate_set = candidate_set.model_copy(update={"candidates": [changed], "candidate_count": 1})
    result = evaluate_risk(candidate_set, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider())
    assert "BID_ASK_TOO_WIDE" in result.decisions[0].reason_codes


@pytest.mark.parametrize(
    ("hour", "minute", "reason", "approved"),
    [(9, 30, "ENTRY_TOO_EARLY", False), (10, 0, None, True), (14, 0, "ENTRY_TOO_LATE", False)],
)
def test_live_entry_window(market_snapshot, hour, minute, reason, approved):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    observed = raw.timestamp_ist.replace(hour=hour, minute=minute)
    raw = raw.model_copy(update={"timestamp_ist": observed})
    result = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.LIVE, StateProvider(), ConfiguredMarketEventProvider(),
                           evaluated_at=observed + timedelta(seconds=1))
    assert (result.approved_count > 0) is approved
    if reason:
        assert reason in result.decisions[0].reason_codes


def test_historical_bypasses_freshness_and_live_rejects_stale(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    evaluated = raw.timestamp_ist + timedelta(hours=1)
    historical = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                               EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                               ConfiguredMarketEventProvider(), evaluated_at=evaluated)
    live = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                         EvaluationContext.LIVE, StateProvider(), ConfiguredMarketEventProvider(),
                         evaluated_at=evaluated)
    assert historical.approved_count > 0
    assert "SNAPSHOT_STALE" in live.decisions[0].reason_codes


def test_expiry_day_policy(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    raw = raw.model_copy(update={"timestamp_ist": raw.timestamp_ist.replace(
        year=raw.expiry.year, month=raw.expiry.month, day=raw.expiry.day
    )})
    rejected = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                             EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                             ConfiguredMarketEventProvider())
    allowed = evaluate_risk(candidates, 99, raw, feature, regime,
                            approved_config(allow_expiry_day=True), EvaluationContext.HISTORICAL,
                            ResearchRiskStateProvider(), ConfiguredMarketEventProvider())
    assert "EXPIRY_DAY_DISABLED" in rejected.decisions[0].reason_codes
    assert allowed.approved_count > 0


@pytest.mark.parametrize(
    ("state", "config", "reason"),
    [
        (StateProvider(trades=1), approved_config(), "MAX_TRADES_REACHED"),
        (StateProvider(pnl=-1000), approved_config(max_daily_loss=500), "DAILY_LOSS_LIMIT_REACHED"),
    ],
)
def test_state_limits(market_snapshot, state, config, reason):
    result, *_ = run_risk(market_snapshot, config=config, state=state)
    assert reason in result.decisions[0].reason_codes


def test_duplicate_strategy_rejected(market_snapshot):
    baseline, *_ = run_risk(market_snapshot)
    fingerprint = baseline.decisions[0].candidate_fingerprint
    result, *_ = run_risk(market_snapshot, state=StateProvider(keys={fingerprint}))
    assert "DUPLICATE_STRATEGY" in result.decisions[0].reason_codes


def test_live_research_state_is_not_faked(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    result = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.LIVE, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider(), evaluated_at=raw.timestamp_ist)
    assert "RISK_STATE_UNAVAILABLE" in result.decisions[0].reason_codes


@pytest.mark.parametrize(("blocking", "approved"), [(True, False), (False, True)])
def test_event_risk(market_snapshot, blocking, approved):
    raw, *_ = risk_context(market_snapshot)
    event = MarketEvent(name="RBI_POLICY", start_time=raw.timestamp_ist - timedelta(minutes=1),
                        end_time=raw.timestamp_ist + timedelta(minutes=1), severity="HIGH",
                        block_entries=blocking)
    result, *_ = run_risk(market_snapshot, events=ConfiguredMarketEventProvider([event]))
    assert (result.approved_count > 0) is approved
    if blocking:
        assert "BLOCKING_MARKET_EVENT" in result.decisions[0].reason_codes
    else:
        assert "NONBLOCKING_MARKET_EVENT" in result.decisions[0].warnings


def test_intraday_oi_policy(market_snapshot):
    _, raw, feature, regime, candidates = run_risk(market_snapshot)
    feature = feature.model_copy(update={
        "data_quality": feature.data_quality.model_copy(update={"intraday_oi_usable": False})
    })
    required = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                             EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                             ConfiguredMarketEventProvider())
    optional = evaluate_risk(candidates, 99, raw, feature, regime,
                             approved_config(require_intraday_oi=False), EvaluationContext.HISTORICAL,
                             ResearchRiskStateProvider(), ConfiguredMarketEventProvider())
    assert "INTRADAY_OI_UNUSABLE" in required.decisions[0].reason_codes
    assert optional.approved_count > 0


@pytest.mark.parametrize(("prior_direction", "approved"), [("BULLISH", True), ("BEARISH", False)])
def test_consecutive_regime_confirmation(market_snapshot, prior_direction, approved):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    prior = regime.model_copy(update={"regime": prior_direction})
    result = evaluate_risk(candidates, 99, raw, feature, regime,
                           approved_config(required_consecutive_directional_snapshots=2),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider(), prior_regimes=[prior])
    assert (result.approved_count > 0) is approved
    if not approved:
        assert "INSUFFICIENT_REGIME_CONFIRMATION" in result.decisions[0].reason_codes


def test_insufficient_confirmation_and_disabled_behavior(market_snapshot):
    required, *_ = run_risk(
        market_snapshot, config=approved_config(required_consecutive_directional_snapshots=2)
    )
    disabled, *_ = run_risk(
        market_snapshot, config=approved_config(required_consecutive_directional_snapshots=1)
    )
    assert "INSUFFICIENT_REGIME_CONFIRMATION" in required.decisions[0].reason_codes
    assert disabled.approved_count > 0


def test_multiple_candidates_are_independent_and_best_uses_phase6_score(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    assert len(candidates.candidates) >= 2
    first, second = candidates.candidates[:2]
    rejected = second.model_copy(update={
        "short_leg": second.short_leg.model_copy(update={"open_interest": 0})
    })
    candidates = candidates.model_copy(update={"candidates": [first, rejected], "candidate_count": 2})
    result = evaluate_risk(candidates, 99, raw, feature, regime, approved_config(),
                           EvaluationContext.HISTORICAL, ResearchRiskStateProvider(),
                           ConfiguredMarketEventProvider())
    assert result.approved_count == 1 and result.rejected_count == 1
    assert result.best_approved_candidate == first.candidate_id


def test_optional_candidate_stability_policy(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot)
    fingerprint = strategy_fingerprint(candidates.candidates[0], raw.timestamp_ist.date())
    config = approved_config(require_candidate_stability=True, candidate_stability_snapshots=2)
    stable = evaluate_risk(
        candidates, 99, raw, feature, regime, config, EvaluationContext.HISTORICAL,
        ResearchRiskStateProvider(), ConfiguredMarketEventProvider(),
        prior_candidate_keys=[{fingerprint for _ in candidates.candidates}],
    )
    unstable = evaluate_risk(
        candidates, 99, raw, feature, regime, config, EvaluationContext.HISTORICAL,
        ResearchRiskStateProvider(), ConfiguredMarketEventProvider(),
        prior_candidate_keys=[set()],
    )
    assert stable.decisions[0].decision == RiskDecisionType.APPROVED
    assert "CANDIDATE_UNSTABLE" in unstable.decisions[0].reason_codes


def persisted_context(repository, session_factory, market_snapshot, direction="BULLISH"):
    raw, feature, regime, candidates = risk_context(market_snapshot, direction)
    run_id = repository.create_collector_run(raw.timestamp_ist)
    saved = repository.save_market_snapshot(raw, collection_bucket(raw.timestamp_ist, 3), run_id)
    feature = feature.model_copy(update={"snapshot_id": saved.snapshot_id})
    feature_id = FeatureRepository(session_factory).upsert(feature)
    regime = regime.model_copy(update={"snapshot_id": saved.snapshot_id, "feature_snapshot_id": feature_id})
    regime_id = RegimeRepository(session_factory).upsert(regime)
    candidates = candidates.model_copy(update={
        "snapshot_id": saved.snapshot_id,
        "regime_snapshot_id": regime_id,
        "candidates": [item.model_copy(update={
            "market_snapshot_id": saved.snapshot_id, "regime_snapshot_id": regime_id
        }) for item in candidates.candidates],
    })
    StrategyRepository(session_factory).upsert(candidates)
    return saved.snapshot_id, RiskRepository(session_factory)


def test_persistence_idempotency_multiple_candidates_and_api(repository, session_factory, market_snapshot):
    snapshot_id, risk_repository = persisted_context(repository, session_factory, market_snapshot)
    config = approved_config()
    first = build_and_store_risk(risk_repository, snapshot_id, config, EvaluationContext.HISTORICAL)
    second = build_and_store_risk(risk_repository, snapshot_id, config, EvaluationContext.HISTORICAL)
    with session_factory() as session:
        count = session.scalar(select(func.count(RiskDecisionRecord.id)))
    assert count == first.candidate_count == second.candidate_count
    app.dependency_overrides[get_risk_repository] = lambda: risk_repository
    try:
        client = TestClient(app)
        assert client.get("/api/risk/latest").status_code == 200
        assert client.get(f"/api/risk/{snapshot_id}").json()["candidate_count"] == count
        assert len(client.get("/api/risk?limit=50").json()) == 1
        assert len(client.get(f"/api/risk/{snapshot_id}/approved").json()) == count
    finally:
        app.dependency_overrides.clear()


def test_not_applicable_persistence(repository, session_factory, market_snapshot):
    snapshot_id, risk_repository = persisted_context(
        repository, session_factory, market_snapshot, "NO_TRADE"
    )
    result = build_and_store_risk(
        risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL
    )
    assert result.not_applicable
    assert risk_repository.get(snapshot_id)["decisions"][0]["decision"] == "NOT_APPLICABLE"


def test_broker_confirmed_mkt_lot_is_parsed_without_guessing():
    class Client:
        def option_chain(self, **kwargs):
            from tests.test_snapshot_validation import live_option_response
            value = live_option_response()
            value["common_data"] = {"mktLot": "65"}
            return value

    adapter = KotakMarketDataAdapter(Client())
    adapter.get_nifty_option_chain(datetime(2099, 10, 8).date())
    assert adapter.get_nifty_lot_size() == 65


def test_backfill_is_chronological_and_persisted_only(market_snapshot):
    raw, feature, regime, candidates = risk_context(market_snapshot, "NO_TRADE")

    class Repository:
        def __init__(self):
            self.saved = []

        def context_snapshot_ids_chronological(self):
            return [1, 2]

        def load_context(self, snapshot_id):
            return (
                raw,
                feature.model_copy(update={"snapshot_id": snapshot_id}),
                snapshot_id + 20,
                regime.model_copy(update={"snapshot_id": snapshot_id}),
                snapshot_id + 30,
                candidates.model_copy(update={
                    "snapshot_id": snapshot_id,
                    "regime_snapshot_id": snapshot_id + 20,
                }),
            )

        def prior_regimes(self, timestamp, limit):
            return []

        def prior_candidate_keys(self, timestamp, limit):
            return []

        def upsert(self, result, candidate_set_id):
            self.saved.append(result.snapshot_id)

    repository = Repository()
    results = backfill_risk(repository, approved_config())
    assert [item.snapshot_id for item in results] == [1, 2]
    assert repository.saved == [1, 2]
