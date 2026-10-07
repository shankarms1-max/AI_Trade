from app.strategy.candidate_engine import StrategyConfig, generate_candidates
from app.strategy.policy import policy_from_settings
from app.strategy.models import StrategyCandidateSet
from app.strategy.repository import StrategyRepository


def config_from_settings(settings, *, risk=None) -> StrategyConfig:
    if risk is None:
        from app.risk.service import config_from_settings as risk_config
        risk = risk_config(settings)
    return StrategyConfig(
        max_defined_loss_rupees=risk.max_loss_per_trade,
        max_defined_capital_rupees=risk.max_capital_per_trade,
        max_defined_width=risk.max_spread_width,
        credit_spread_policy=policy_from_settings(settings),
        min_regime_confidence=settings.strategy_min_regime_confidence,
        min_evidence_quality=settings.strategy_min_evidence_quality,
        require_structure_reference=settings.strategy_require_structure_reference,
        min_short_distance_points=settings.strategy_min_short_distance_points,
        min_short_distance_pct=settings.strategy_min_short_distance_pct,
        allowed_spread_widths=settings.configured_strategy_widths,
        min_short_premium=settings.strategy_min_short_premium,
        min_net_credit=settings.strategy_min_net_credit,
        min_credit_to_width_ratio=settings.strategy_min_credit_to_width_ratio,
        min_short_open_interest=settings.strategy_min_short_open_interest,
        min_long_open_interest=settings.strategy_min_long_open_interest,
        min_short_volume=settings.strategy_min_short_volume,
        min_long_volume=settings.strategy_min_long_volume,
        require_usable_volume=settings.strategy_require_usable_volume,
        max_bid_ask_spread_pct=settings.strategy_max_bid_ask_spread_pct,
        short_delta_min_abs=settings.strategy_short_delta_min_abs,
        short_delta_max_abs=settings.strategy_short_delta_max_abs,
        max_candidates=settings.strategy_max_candidates,
        max_snapshot_age_seconds=settings.strategy_max_snapshot_age_seconds,
        volatility_buffer_enabled=settings.strategy_volatility_buffer_enabled,
        vix_low_distance_multiplier=settings.strategy_vix_low_distance_multiplier,
        vix_normal_distance_multiplier=settings.strategy_vix_normal_distance_multiplier,
        vix_elevated_distance_multiplier=settings.strategy_vix_elevated_distance_multiplier,
        vix_high_distance_multiplier=settings.strategy_vix_high_distance_multiplier,
        no_candidate_on_high_vix=settings.strategy_no_candidate_on_high_vix,
    )


def build_and_store_candidates(
    repository: StrategyRepository,
    snapshot_id: int,
    config: StrategyConfig,
    *,
    enforce_freshness: bool = False,
) -> StrategyCandidateSet:
    context = repository.load_context(snapshot_id)
    if context is None:
        raise LookupError(f"raw snapshot, phase3_v1, and phase4_v1 for snapshot {snapshot_id} are required")
    raw, _, feature, regime_id, regime = context
    result = generate_candidates(
        raw, feature, regime, regime_id, config, enforce_freshness=enforce_freshness
    )
    repository.upsert(result)
    return result


def backfill_candidates(
    repository: StrategyRepository, config: StrategyConfig
) -> list[StrategyCandidateSet]:
    return [
        build_and_store_candidates(repository, snapshot_id, config, enforce_freshness=False)
        for snapshot_id in repository.context_snapshot_ids_chronological()
    ]
