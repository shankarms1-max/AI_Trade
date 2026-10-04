from collections import Counter

from app.ai.models import (
    AIResearchInput,
    DataQualityInput,
    Phase3Summary,
    Phase4Summary,
    Phase14AlphaSummary,
    SnapshotInput,
)
from app.features.models import ContractFeature, MarketFeatureSnapshot
from app.regime.models import RegimeResult
from app.alpha.models import AlphaFeatureSnapshot


def _contract(row: ContractFeature) -> dict:
    return {
        "strike": row.strike,
        "option_type": row.option_type,
        "open_interest": row.open_interest,
        "oi_change": row.change_in_open_interest,
        "volume": row.volume,
        "ltp": row.ltp,
        "distance_from_spot": row.distance_from_spot,
        "positioning_class": row.positioning_class,
    }


def _contracts(rows: list[ContractFeature] | None) -> list[dict] | None:
    return None if rows is None else [_contract(row) for row in rows]


def _cluster(item) -> dict:
    return {
        "low_strike": item.low_strike,
        "high_strike": item.high_strike,
        "center_strike": item.center_strike,
        "share_of_side_oi": item.share_of_side_oi,
        "strength_score": item.strength_score,
        "distance_from_spot": item.distance_from_spot,
    }


def build_ai_research_input(
    feature: MarketFeatureSnapshot, regime: RegimeResult,
    alpha: AlphaFeatureSnapshot | None = None,
) -> AIResearchInput:
    price = feature.price_structure_features
    positioning = Counter(
        row.positioning_class
        for row in feature.oi_features.contracts
        if row.positioning_class != "INSUFFICIENT_DATA"
    )
    all_reasons = sorted({
        reason for group in regime.signal_groups for reason in group.reason_codes
    })
    return AIResearchInput(
        snapshot_id=feature.snapshot_id,
        snapshot=SnapshotInput(
            timestamp=feature.timestamp,
            spot=feature.spot,
            future=feature.future,
            india_vix=feature.volatility_features.india_vix,
            atm=feature.atm_strike,
            expiry=feature.expiry,
        ),
        data_quality=DataQualityInput(
            evidence_quality=regime.evidence_quality.value,
            intraday_oi_usable=feature.data_quality.intraday_oi_usable,
            volume_usable=feature.data_quality.intraday_volume_usable,
            warnings=regime.warnings,
        ),
        phase3_summary=Phase3Summary(
            local_pcr_oi=feature.pcr_features.local_pcr_oi,
            local_pcr_oi_change=feature.pcr_features.local_pcr_oi_change,
            top_call_oi=_contracts(feature.oi_features.top_call_oi) or [],
            top_put_oi=_contracts(feature.oi_features.top_put_oi) or [],
            top_call_oi_additions=_contracts(feature.oi_features.top_call_oi_additions),
            top_put_oi_additions=_contracts(feature.oi_features.top_put_oi_additions),
            top_call_oi_reductions=_contracts(feature.oi_features.top_call_oi_reductions),
            top_put_oi_reductions=_contracts(feature.oi_features.top_put_oi_reductions),
            support_clusters=[
                _cluster(item)
                for item in feature.support_resistance.potential_support_clusters
            ],
            resistance_clusters=[
                _cluster(item)
                for item in feature.support_resistance.potential_resistance_clusters
            ],
            futures_basis=feature.futures_features.futures_basis,
            vix_regime=feature.volatility_features.vix_regime,
            observed_opening_range={
                "high": price.observed_opening_range_high,
                "low": price.observed_opening_range_low,
                "complete": price.observed_opening_range_complete,
            },
            collector_observed_high=price.collector_observed_high,
            collector_observed_low=price.collector_observed_low,
            positioning_summary=dict(positioning),
        ),
        phase4=Phase4Summary(
            regime=regime.regime.value,
            confidence=regime.confidence,
            evidence_quality=regime.evidence_quality.value,
            bull_score=regime.bull_score,
            bear_score=regime.bear_score,
            range_score=regime.range_score,
            bull_evidence=regime.bull_evidence,
            bear_evidence=regime.bear_evidence,
            range_evidence=regime.range_evidence,
            warnings=regime.warnings,
            reason_codes=all_reasons,
        ),
        phase14_alpha=Phase14AlphaSummary(
            available=alpha is not None and alpha.validity_state.value == "VALID",
            price_return=None if alpha is None else alpha.price_return,
            alpha_1=None if alpha is None else alpha.alpha_1,
            signed_log_return=None if alpha is None else alpha.signed_log_return,
            hypothesis_type=None if alpha is None else alpha.hypothesis_type.value,
            alpha_1_direction=None if alpha is None else alpha.alpha_1_direction.value,
            alpha_2_direction=None if alpha is None else alpha.alpha_2_direction.value,
            participation_state=None if alpha is None else alpha.participation_state.value,
            underlying_horizon_volatility=None if alpha is None else alpha.underlying_horizon_volatility,
            validity_state=None if alpha is None else alpha.validity_state.value,
            signal_persistence_count=0 if alpha is None else alpha.consecutive_confirmation_count,
            legacy_alpha2_raw=None if alpha is None else alpha.legacy_alpha2_raw,
            alpha_2=None if alpha is None else alpha.alpha_2,
            atm_volume_activity=None if alpha is None else alpha.atm_volume_activity,
            atm_option_volatility=None if alpha is None else alpha.atm_option_volatility,
            joint_alpha_direction=None if alpha is None else alpha.joint_alpha_direction.value,
            confirmation_count=0 if alpha is None else alpha.consecutive_confirmation_count,
            evidence_quality="INSUFFICIENT" if alpha is None else alpha.evidence_quality.value,
            warnings=[] if alpha is None else alpha.warnings,
        ),
    )
