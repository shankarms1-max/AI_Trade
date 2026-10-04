from datetime import date, datetime, time, timedelta
from math import log
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.alpha.atm import select_atm
from app.alpha.engine import (
    AlphaConfig, build_alpha_features, consecutive_confirmation_count, direction,
    joint_direction, raw_components,
)
from app.alpha.experiments import (
    ExperimentObservation, adjacent_robustness, bounded_parameter_grid,
    chronological_folds, chronological_split, evaluate_parameters,
)
from app.alpha.models import (
    AlphaDirection, AlphaEvidenceQuality, JointAlphaDirection,
)
from app.alpha.ranks import percentile_rank
from app.alpha.repository import AlphaRepository
from app.alpha.quality import assess_quality
from app.alpha.service import build_and_store_alpha
from app.alpha.volume_alpha import directional_volume_metrics, interval_volume, volume_ratio
from app.alpha.volatility import combined_option_volatility, observed_price_volatility
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.db.repositories import SnapshotRepository
from app.db.session import get_session_factory
from app.main import app
from app.regime.alpha_signals import statistical_alpha_signals
from app.regime.models import SignalDirection
from app.regime.scoring import RegimeWeights, weighted_scores

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 10, 8)


def snapshot(
    minute: int, *, spot: float = 25000, future: float | None = 25020,
    ce_ltp: float = 100, pe_ltp: float = 100, ce_volume: int | None = 100,
    pe_volume: int | None = 100, strike: float = 25000,
) -> MarketSnapshot:
    timestamp = datetime(2026, 10, 5, 9, 15, tzinfo=IST) + timedelta(minutes=minute)
    contracts = [
        OptionContractSnapshot(
            strike=strike, option_type=kind, expiry=EXPIRY,
            trading_symbol=f"NIFTY-{strike:g}-{kind}",
            instrument_token=f"{strike:g}-{kind}", ltp=ltp, volume=volume,
            open_interest=1000, previous_open_interest=900, change_in_open_interest=100,
        )
        for kind, ltp, volume in (("CE", ce_ltp, ce_volume), ("PE", pe_ltp, pe_volume))
    ]
    return MarketSnapshot(
        timestamp_ist=timestamp, nifty_spot=spot, nifty_future=future,
        future_instrument_id="NIFTY-FUT-OCT26" if future is not None else None,
        future_expiry=date(2026, 10, 29) if future is not None else None,
        india_vix=15, atm_strike=strike, expiry=EXPIRY, options=contracts,
    )


def alpha_config(**updates) -> AlphaConfig:
    values = dict(
        price_source="FUTURE", price_horizon_seconds=300,
        horizon_tolerance_seconds=120, alpha1_lookback_minutes=800,
        alpha2_lookback_minutes=300, volume_lookback_minutes=300,
        volatility_lookback_minutes=300, min_rank_observations=2,
        min_volume_observations=1, min_volatility_returns=2,
        required_consecutive_confirmations=2,
    )
    values.update(updates)
    return AlphaConfig(**values)


@pytest.mark.parametrize(
    ("now", "prior", "expected_sign"),
    [(25040, 25020, 1), (25000, 25020, -1)],
)
def test_price_return_direction_and_signed_log_formula(now, prior, expected_sign):
    history = [snapshot(0, future=25000), snapshot(3, future=prior)]
    raw = raw_components(snapshot(8, future=now), history, alpha_config())
    assert raw.price_return == pytest.approx(log(now / prior))
    assert raw.price_return * expected_sign > 0


def test_exact_horizon_and_tolerance_selection():
    history = [snapshot(1, future=24900), snapshot(3, future=25000)]
    raw = raw_components(snapshot(8, future=25050), history, alpha_config())
    assert raw.price_return == pytest.approx(log(25050 / 25000))
    unavailable = raw_components(
        snapshot(20, future=25050), history,
        alpha_config(horizon_tolerance_seconds=10),
    )
    assert unavailable.price_return is None


def test_future_snapshot_is_never_used():
    current = snapshot(8, future=25050)
    history = [snapshot(3, future=25000), snapshot(11, future=99999)]
    raw = raw_components(current, history, alpha_config())
    assert raw.price_return == pytest.approx(log(25050 / 25000))


@pytest.mark.parametrize(
    ("history", "expected"),
    [([1.0, 2.0, 3.0], 0.5), ([1.0, 2.0, 2.0, 3.0], 0.5)],
)
def test_percentile_rank_midrank_tie_handling(history, expected):
    assert percentile_rank(2.0, history, 2) == pytest.approx(expected)


def test_percentile_rank_requires_minimum_observations():
    assert percentile_rank(2.0, [1.0], 2) is None


def test_atm_selection_and_lower_tie_break():
    base = snapshot(0)
    options = []
    for strike in (24950, 25050):
        options.extend(snapshot(0, strike=strike).options)
    selected = select_atm(base.model_copy(update={"options": options}), 25000)
    assert selected is not None and selected.strike == 24950
    assert selected.call.option_type.value == "CE" and selected.put.option_type.value == "PE"


def test_atm_requires_both_legs_and_matching_expiry():
    base = snapshot(0)
    only_call = [item for item in base.options if item.option_type.value == "CE"]
    assert select_atm(base.model_copy(update={"options": only_call}), 25000) is None


def test_cumulative_volume_interval_and_reset_detection():
    previous = snapshot(0, ce_volume=100, pe_volume=90)
    current = snapshot(3, ce_volume=135, pe_volume=120)
    assert interval_volume(current.options[0], previous, current).value == 35
    later = snapshot(6, ce_volume=10)
    reset = interval_volume(later.options[0], current, later)
    assert reset.value is None and reset.reset


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    [(20, [10, 20, 30], 1.0), (10, [0, 0], None), (None, [10], None)],
)
def test_volume_ratio_safety(current, baseline, expected):
    ratio, _ = volume_ratio(current, baseline)
    assert ratio == expected


def test_directional_volume_metrics_are_raw_not_labelled():
    ratio, imbalance = directional_volume_metrics(30, 60)
    assert ratio == 2
    assert imbalance == pytest.approx(-1 / 3)


def test_observed_option_volatility_and_combination():
    ce, count = observed_price_volatility([100, 110, 99, 105], 2)
    pe, _ = observed_price_volatility([100, 95, 105, 98], 2)
    assert count == 3 and ce is not None and ce > 0 and pe is not None and pe > 0
    assert combined_option_volatility(ce, pe) == pytest.approx((ce + pe) / 2)


def test_zero_or_insufficient_volatility_is_protected():
    value, count = observed_price_volatility([100, 100, 100], 2)
    assert value == 0 and count == 2
    assert observed_price_volatility([100, 101], 2)[0] is None
    assert combined_option_volatility(None, 0.1) is None


def test_legacy_alpha2_raw_impulse_is_diagnostic_only():
    history = [
        snapshot(0, future=25000, ce_ltp=100, pe_ltp=100, ce_volume=100, pe_volume=100),
        snapshot(3, future=25010, ce_ltp=105, pe_ltp=96, ce_volume=120, pe_volume=130),
        snapshot(6, future=25020, ce_ltp=101, pe_ltp=102, ce_volume=140, pe_volume=160),
    ]
    raw = raw_components(
        snapshot(9, future=25040, ce_ltp=108, pe_ltp=94, ce_volume=170, pe_volume=200),
        history, alpha_config(),
    )
    assert raw.activity is not None and raw.option_volatility is not None
    assert raw.impulse == pytest.approx(raw.legacy_open_return * raw.activity / raw.option_volatility)
    assert raw.standardized_return is None  # independent underlying history is still warming up


def test_alpha2_never_uses_a_future_volume_or_price():
    current = snapshot(9, future=25040, ce_ltp=108, pe_ltp=94, ce_volume=170, pe_volume=200)
    history = [
        snapshot(0, future=25000, ce_ltp=100, pe_ltp=100, ce_volume=100, pe_volume=100),
        snapshot(3, future=25010, ce_ltp=105, pe_ltp=96, ce_volume=120, pe_volume=130),
        snapshot(6, future=25020, ce_ltp=101, pe_ltp=102, ce_volume=140, pe_volume=160),
    ]
    expected = raw_components(current, history, alpha_config()).impulse
    future = snapshot(12, future=30000, ce_ltp=500, pe_ltp=1, ce_volume=99999, pe_volume=99999)
    assert raw_components(current, history + [future], alpha_config()).impulse == expected


@pytest.mark.parametrize(
    ("rank", "expected"),
    [(0.85, AlphaDirection.STRONG_BULLISH), (0.75, AlphaDirection.BULLISH),
     (0.5, AlphaDirection.NEUTRAL), (0.25, AlphaDirection.BEARISH),
     (0.15, AlphaDirection.STRONG_BEARISH), (None, AlphaDirection.INSUFFICIENT_DATA)],
)
def test_alpha_signal_states(rank, expected):
    signed = -0.001 if rank is not None and rank < .5 else 0.001
    assert direction(rank, alpha_config(), signed) == expected


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [(AlphaDirection.STRONG_BULLISH, AlphaDirection.STRONG_BULLISH, JointAlphaDirection.STRONG_BULLISH_CONFIRMATION),
     (AlphaDirection.BULLISH, AlphaDirection.STRONG_BULLISH, JointAlphaDirection.BULLISH_CONFIRMATION),
     (AlphaDirection.STRONG_BEARISH, AlphaDirection.STRONG_BEARISH, JointAlphaDirection.STRONG_BEARISH_CONFIRMATION),
     (AlphaDirection.BULLISH, AlphaDirection.BEARISH, JointAlphaDirection.CONFLICT),
     (AlphaDirection.NEUTRAL, AlphaDirection.NEUTRAL, JointAlphaDirection.NEUTRAL),
     (AlphaDirection.INSUFFICIENT_DATA, AlphaDirection.BULLISH, JointAlphaDirection.INSUFFICIENT_DATA)],
)
def test_joint_signal_does_not_average_conflicts(first, second, expected):
    assert joint_direction(first, second) == expected


def test_warmup_is_null_and_explicit():
    result = build_alpha_features(1, snapshot(0), [], [], alpha_config())
    assert result.alpha_1 is None and result.alpha_2 is None
    assert result.status.value == "WARMING_UP"
    assert result.evidence_quality == AlphaEvidenceQuality.INSUFFICIENT
    assert "INSUFFICIENT_ALPHA_HISTORY" in result.warnings


@pytest.mark.parametrize(
    ("prior_states", "current", "expected"),
    [([], JointAlphaDirection.BULLISH_CONFIRMATION, 1),
     ([JointAlphaDirection.BULLISH_CONFIRMATION], JointAlphaDirection.STRONG_BULLISH_CONFIRMATION, 2),
     ([JointAlphaDirection.BEARISH_CONFIRMATION], JointAlphaDirection.STRONG_BEARISH_CONFIRMATION, 2),
     ([JointAlphaDirection.BULLISH_CONFIRMATION], JointAlphaDirection.BEARISH_CONFIRMATION, 1),
     ([JointAlphaDirection.NEUTRAL], JointAlphaDirection.BULLISH_CONFIRMATION, 1)],
)
def test_configurable_consecutive_confirmation(prior_states, current, expected):
    seed = build_alpha_features(1, snapshot(0), [], [], alpha_config())
    prior = [seed.model_copy(update={
        "timestamp": seed.timestamp + timedelta(minutes=index),
        "joint_alpha_direction": state,
    }) for index, state in enumerate(prior_states)]
    assert consecutive_confirmation_count(current, prior) == expected


def test_statistical_alpha_weight_has_independent_cap():
    warmed = build_alpha_features(1, snapshot(0), [], [], alpha_config()).model_copy(update={
        "alpha_1": .95, "alpha_2": .9,
        "alpha_1_direction": AlphaDirection.STRONG_BULLISH,
        "alpha_2_direction": AlphaDirection.STRONG_BULLISH,
        "evidence_quality": AlphaEvidenceQuality.HIGH,
        "validity_state": "VALID",
    })
    groups = statistical_alpha_signals(warmed)
    bull, _, _, contributions = weighted_scores(groups, RegimeWeights(statistical_alpha_cap=5))
    assert bull <= 5
    assert all(group.direction == SignalDirection.BULLISH for group in groups)
    assert sum(contributions.values()) == pytest.approx(bull)


@pytest.mark.parametrize(
    ("alpha1", "alpha2", "checks", "count", "expected"),
    [(True, True, 4, 100, AlphaEvidenceQuality.HIGH),
     (True, True, 3, 50, AlphaEvidenceQuality.MEDIUM),
     (True, False, 2, 50, AlphaEvidenceQuality.LOW),
     (False, False, 4, 100, AlphaEvidenceQuality.INSUFFICIENT)],
)
def test_alpha_evidence_quality(alpha1, alpha2, checks, count, expected):
    flags = [True] * checks + [False] * (4 - checks)
    assert assess_quality(
        alpha1_available=alpha1, alpha2_available=alpha2,
        atm_available=flags[0], volume_available=flags[1],
        volatility_available=flags[2], sequence_continuous=flags[3],
        rank_count=count, minimum_rank=50,
    ) == expected


def test_parameter_grid_is_bounded_and_chronological():
    windows = [(time(9, 35), time(13, 30))]
    assert len(bounded_parameter_grid([.7, .8], [1, 2], windows, 4)) == 4
    with pytest.raises(ValueError, match="maximum"):
        bounded_parameter_grid([.7, .8], [1, 2], windows, 3)


def test_chronological_split_and_folds_never_shuffle():
    class Item:
        def __init__(self, minute):
            self.entry_timestamp = datetime(2026, 1, 1, 9, minute, tzinfo=IST)
    values = [Item(4), Item(1), Item(3), Item(2), Item(0)]
    split = chronological_split(values)
    ordered = split["TRAIN"] + split["VALIDATION"] + split["FINAL_TEST"]
    assert [item.entry_timestamp.minute for item in ordered] == [0, 1, 2, 3, 4]
    folds = chronological_folds(values, 2)
    assert folds[0][-1].entry_timestamp < folds[1][0].entry_timestamp


def test_experiment_reports_frequencies_and_small_sample_warning():
    parameters = bounded_parameter_grid(
        [.8], [2], [(time(9, 35), time(13, 30))], 1
    )[0]
    observations = [ExperimentObservation(
        timestamp=datetime(2026, 10, 5, 10, 0, tzinfo=IST),
        alpha_1=.9, alpha_2=.85, joint_direction="STRONG_BULLISH_CONFIRMATION",
        confirmation_count=2, candidate_count=1, approved_count=1,
    )]
    result = evaluate_parameters([], parameters, 30, observations)
    assert result["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert result["signal_frequency"] == result["candidate_frequency"] == result["approval_frequency"] == 1


def test_adjacent_robustness_output_exposes_neighbor_behavior():
    rows = [{
        "parameters": {"alpha_threshold": threshold, "confirmations": 2,
                       "entry_window": "09:35-13:30"},
        "net_pnl": pnl,
    } for threshold, pnl in ((.75, 10), (.8, 11), (.85, 9))]
    result = adjacent_robustness(rows)
    assert result[1]["neighbor_count"] == 3
    assert result[1]["adjacent_mean_net_pnl"] == 10


def test_alpha_persistence_idempotency_and_read_only_api(engine, session_factory):
    raw_repository = SnapshotRepository(session_factory)
    ids = []
    for point in (snapshot(0), snapshot(3), snapshot(6), snapshot(9)):
        ids.append(raw_repository.save_market_snapshot(point, point.timestamp_ist).snapshot_id)
    repository = AlphaRepository(session_factory)
    result = build_and_store_alpha(repository, ids[-1], alpha_config())
    first_id = repository.upsert(result)
    assert repository.upsert(result) == first_id
    assert repository.get(ids[-1])["snapshot_id"] == ids[-1]

    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        client = TestClient(app)
        assert client.get(f"/api/alpha/{ids[-1]}").status_code == 200
        assert client.get("/api/alpha/latest").status_code == 200
        assert client.get("/api/alpha?limit=10").status_code == 200
        assert client.get("/api/alpha/history?date=2026-10-05").status_code == 200
    finally:
        app.dependency_overrides.clear()
