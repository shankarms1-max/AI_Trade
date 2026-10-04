from datetime import timedelta

import pytest

from app.data.models import MarketSnapshot
from app.features.engine import build_market_features
from app.features.models import ContractFeature, MarketFeatureSnapshot
from app.regime.confidence import calculate_confidence
from app.regime.engine import RegimeConfig, classify_regime
from app.regime.models import EvidenceQuality, Regime, SignalDirection, SignalGroup
from app.regime.oi_signals import positioning_signal, static_oi_signal
from app.regime.pcr_signals import pcr_signal
from app.regime.scoring import RegimeWeights, weighted_scores
from app.alpha.engine import AlphaConfig, build_alpha_features
from app.alpha.models import (
    AlphaDirection, AlphaEvidenceQuality, JointAlphaDirection,
)


def directional_features(
    raw: MarketSnapshot, direction: str = "bull"
) -> tuple[MarketFeatureSnapshot, MarketFeatureSnapshot]:
    prior_options = [item.model_copy(update={"ltp": 100.0}) for item in raw.options]
    prior_raw = raw.model_copy(
        update={
            "timestamp_ist": raw.timestamp_ist - timedelta(minutes=3),
            "nifty_spot": 25000.0,
            "nifty_future": 25020.0,
            "india_vix": 14.0,
            "options": prior_options,
        }
    )
    current_options = []
    for item in raw.options:
        bullish_contract = (
            {"ltp": 110.0, "change_in_open_interest": -100}
            if item.option_type.value == "CE"
            else {"ltp": 90.0, "change_in_open_interest": 100}
        )
        bearish_contract = (
            {"ltp": 90.0, "change_in_open_interest": 100}
            if item.option_type.value == "CE"
            else {"ltp": 110.0, "change_in_open_interest": -100}
        )
        current_options.append(
            item.model_copy(update=bullish_contract if direction == "bull" else bearish_contract)
        )
    sign = 1 if direction == "bull" else -1
    current_raw = raw.model_copy(
        update={
            "nifty_spot": 25000.0 + sign * 50,
            "nifty_future": 25020.0 + sign * 60,
            "india_vix": 14.0,
            "options": current_options,
        }
    )
    prior = build_market_features(1, prior_raw, [])
    current = build_market_features(2, current_raw, [prior_raw])
    return prior, current


def test_insufficient_contract_data_forces_no_trade(market_snapshot: MarketSnapshot) -> None:
    feature = build_market_features(1, market_snapshot, [])
    result = classify_regime(1, feature, None, RegimeConfig(minimum_contracts=100))
    assert result.regime.value == "NO_TRADE"
    assert result.evidence_quality == EvidenceQuality.INSUFFICIENT
    assert result.confidence <= 30


def test_low_evidence_is_capped_and_after_hours_direction_is_blocked(
    market_snapshot: MarketSnapshot,
) -> None:
    no_change = [item.model_copy(update={"change_in_open_interest": 0}) for item in market_snapshot.options]
    feature = build_market_features(1, market_snapshot.model_copy(update={"options": no_change}), [])
    result = classify_regime(1, feature, None)
    assert result.regime.value == "NO_TRADE"
    assert result.evidence_quality == EvidenceQuality.LOW
    assert result.confidence <= 55
    assert "INSUFFICIENT_DYNAMIC_OI" in result.warnings


@pytest.mark.parametrize(("direction", "expected"), [("bull", "BULLISH"), ("bear", "BEARISH")])
def test_multiple_independent_directional_groups(
    market_snapshot: MarketSnapshot, direction: str, expected: str
) -> None:
    prior, current = directional_features(market_snapshot, direction)
    result = classify_regime(2, current, prior)
    assert result.regime.value == expected
    assert result.evidence_quality == EvidenceQuality.HIGH
    assert result.confidence >= 60
    expected_reason = "FUTURES_CONFIRM_UPMOVE" if direction == "bull" else "FUTURES_CONFIRM_DOWNMOVE"
    evidence = result.bull_evidence if direction == "bull" else result.bear_evidence
    assert expected_reason in evidence


def test_range_requires_positive_containment_and_muted_evidence(
    market_snapshot: MarketSnapshot,
) -> None:
    prior, current = directional_features(market_snapshot, "bull")
    zero_rows = [
        row.model_copy(
            update={
                "change_in_open_interest": 0,
                "oi_change": 0,
                "price_change": 0,
                "price_change_pct": 0,
                "positioning_class": "INSUFFICIENT_DATA",
            }
        )
        for row in current.oi_features.contracts
    ]
    quality = current.data_quality.model_copy(update={"intraday_oi_usable": False})
    price = current.price_structure_features.model_copy(
        update={
            "spot_change_from_previous_snapshot": 1.0,
            "spot_change_pct_from_previous_snapshot": 0.004,
            "future_change_from_previous_snapshot": 0.0,
            "future_change_pct_from_previous_snapshot": 0.0,
            "session_open_proxy": current.spot,
            "observed_opening_range_low": current.spot - 20,
            "observed_opening_range_high": current.spot + 20,
            "observed_opening_range_complete": True,
        }
    )
    seed = current.support_resistance.potential_support_clusters[0]
    structure = current.support_resistance.model_copy(
        update={
            "potential_support_clusters": [seed.model_copy(update={"center_strike": current.spot - 50})],
            "potential_resistance_clusters": [seed.model_copy(update={
                "side": "POTENTIAL_RESISTANCE_CLUSTER", "center_strike": current.spot + 50
            })],
        }
    )
    feature = current.model_copy(
        update={
            "data_quality": quality,
            "oi_features": current.oi_features.model_copy(update={"contracts": zero_rows}),
            "price_structure_features": price,
            "support_resistance": structure,
        }
    )
    result = classify_regime(2, feature, prior)
    assert result.regime.value == "RANGE", (
        result.confidence, result.evidence_quality, result.bull_score,
        result.bear_score, result.range_score, result.warnings,
        [(group.name, group.direction, group.score) for group in result.signal_groups],
    )
    assert "PRICE_INSIDE_OBSERVED_OPENING_RANGE" in result.range_evidence
    assert "SPOT_MOVE_MUTED" in result.range_evidence


def test_conflicting_strong_signals_lower_confidence() -> None:
    high = calculate_confidence(
        8, 1, maximum_score=12.5, confirming_groups=4, contradictory_groups=0,
        quality=EvidenceQuality.HIGH, low_cap=55, insufficient_cap=30,
    )
    conflict = calculate_confidence(
        8, 6, maximum_score=12.5, confirming_groups=4, contradictory_groups=2,
        quality=EvidenceQuality.HIGH, low_cap=55, insufficient_cap=30,
    )
    assert conflict < high
    assert high == calculate_confidence(
        8, 1, maximum_score=12.5, confirming_groups=4, contradictory_groups=0,
        quality=EvidenceQuality.HIGH, low_cap=55, insufficient_cap=30,
    )


def test_oi_dependency_contribution_is_capped() -> None:
    groups = [
        SignalGroup(name=name, direction=SignalDirection.BULLISH, score=1, strength=1, reason_codes=[])
        for name in ("DYNAMIC_OI", "OPTION_POSITIONING", "PCR")
    ]
    bull, _, _, contributions = weighted_scores(groups, RegimeWeights(oi_dependency_cap=6))
    assert bull == pytest.approx(6)
    assert sum(contributions.values()) == pytest.approx(6)


@pytest.mark.parametrize(
    ("kind", "classification", "direction"),
    [
        ("CE", "SHORT_BUILDUP", SignalDirection.BEARISH),
        ("PE", "SHORT_BUILDUP", SignalDirection.BULLISH),
        ("CE", "SHORT_COVERING", SignalDirection.BULLISH),
        ("PE", "SHORT_COVERING", SignalDirection.BEARISH),
    ],
)
def test_positioning_interpretation(
    market_snapshot: MarketSnapshot, kind: str, classification: str, direction: SignalDirection
) -> None:
    _, feature = directional_features(market_snapshot)
    row = feature.oi_features.contracts[0].model_copy(
        update={"option_type": kind, "strike": feature.spot, "positioning_class": classification}
    )
    modified = feature.model_copy(
        update={"oi_features": feature.oi_features.model_copy(update={"contracts": [row]})}
    )
    assert positioning_signal(modified).direction == direction


def test_far_positioning_strike_is_excluded(market_snapshot: MarketSnapshot) -> None:
    _, feature = directional_features(market_snapshot)
    row = feature.oi_features.contracts[0].model_copy(
        update={"strike": feature.spot + 1000, "positioning_class": "SHORT_COVERING"}
    )
    modified = feature.model_copy(
        update={"oi_features": feature.oi_features.model_copy(update={"contracts": [row]})}
    )
    assert positioning_signal(modified).direction == SignalDirection.UNAVAILABLE


def test_static_oi_and_pcr_contributions_are_limited(market_snapshot: MarketSnapshot) -> None:
    feature = build_market_features(1, market_snapshot, [])
    assert static_oi_signal(feature).score <= 0.7
    assert pcr_signal(feature, 0.8, 1.2).score <= 0.35


def test_pcr_change_unavailable_without_dynamic_oi(market_snapshot: MarketSnapshot) -> None:
    feature = build_market_features(1, market_snapshot, [])
    modified = feature.model_copy(
        update={"data_quality": feature.data_quality.model_copy(update={"intraday_oi_usable": False})}
    )
    assert "PCR_OI_CHANGE_UNAVAILABLE" in pcr_signal(modified, 0.8, 1.2).reason_codes


def test_vix_changes_risk_only_not_direction(market_snapshot: MarketSnapshot) -> None:
    prior, feature = directional_features(market_snapshot, "bull")
    low = feature.model_copy(
        update={"volatility_features": feature.volatility_features.model_copy(
            update={"india_vix": 10, "vix_regime": "LOW"}
        )}
    )
    high = feature.model_copy(
        update={"volatility_features": feature.volatility_features.model_copy(
            update={"india_vix": 25, "vix_regime": "HIGH"}
        )}
    )
    low_result = classify_regime(2, low, prior)
    high_result = classify_regime(2, high, prior)
    assert low_result.regime == high_result.regime
    assert low_result.bull_score == high_result.bull_score
    assert low_result.bear_score == high_result.bear_score
    assert low_result.risk_flags == ["LOW_VOLATILITY"]
    assert high_result.risk_flags == ["HIGH_VOLATILITY"]


def _alpha(raw, direction: str):
    alpha = build_alpha_features(2, raw, [], [], AlphaConfig(min_rank_observations=2))
    bullish = direction == "BULLISH"
    return alpha.model_copy(update={
        "alpha_1": .9 if bullish else .1,
        "alpha_2": .9 if bullish else .1,
        "alpha_1_direction": AlphaDirection.STRONG_BULLISH if bullish else AlphaDirection.STRONG_BEARISH,
        "alpha_2_direction": AlphaDirection.STRONG_BULLISH if bullish else AlphaDirection.STRONG_BEARISH,
        "joint_alpha_direction": (JointAlphaDirection.STRONG_BULLISH_CONFIRMATION
                                  if bullish else JointAlphaDirection.STRONG_BEARISH_CONFIRMATION),
        "consecutive_confirmation_count": 2,
        "confirmed": True,
        "evidence_quality": AlphaEvidenceQuality.HIGH,
    })


def test_confirmed_bullish_alpha_contributes_with_independent_confirmation(market_snapshot):
    prior, current = directional_features(market_snapshot, "bull")
    config = RegimeConfig(
        use_statistical_alpha=True, require_alpha_for_directional=True,
        weights=RegimeWeights(),
    )
    result = classify_regime(2, current, prior, config, alpha=_alpha(market_snapshot, "BULLISH"))
    assert result.regime == Regime.BULLISH
    assert any(group.name == "STATISTICAL_PRICE_ALPHA" for group in result.signal_groups)


def test_alpha_unavailable_safely_blocks_direction_when_required(market_snapshot):
    prior, current = directional_features(market_snapshot, "bull")
    result = classify_regime(
        2, current, prior,
        RegimeConfig(use_statistical_alpha=True, require_alpha_for_directional=True),
        alpha=None,
    )
    assert result.regime == Regime.NO_TRADE
    assert "DIRECTION_REQUIRES_CONFIRMED_ALPHA" in result.warnings


def test_strong_alpha_derivatives_contradiction_forces_no_trade(market_snapshot):
    prior, current = directional_features(market_snapshot, "bull")
    result = classify_regime(
        2, current, prior,
        RegimeConfig(use_statistical_alpha=True, require_alpha_for_directional=True),
        alpha=_alpha(market_snapshot, "BEARISH"),
    )
    assert result.regime == Regime.NO_TRADE
    assert "STRONG_SIGNAL_CONTRADICTION" in result.warnings
