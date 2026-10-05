from app.risk.engine import evaluate_risk
from app.risk.event_checks import ConfiguredMarketEventProvider, MarketEventProvider
from app.risk.exposure import ResearchRiskStateProvider, RiskStateProvider
from app.risk.limits import RiskConfig
from app.strategy.policy import policy_from_settings
from app.risk.models import EvaluationContext, RiskEvaluationSet
from app.risk.repository import RiskRepository


def config_from_settings(settings) -> RiskConfig:
    return RiskConfig(
        credit_spread_policy=policy_from_settings(settings),
        max_loss_per_trade=settings.risk_max_loss_per_trade,
        max_capital_per_trade=settings.risk_max_capital_per_trade,
        max_trades_per_day=settings.risk_max_trades_per_day,
        max_daily_loss=settings.risk_max_daily_loss,
        max_spread_width=settings.risk_max_spread_width,
        min_net_credit=settings.risk_min_net_credit,
        min_credit_to_width=settings.risk_min_credit_to_width,
        min_short_oi=settings.risk_min_short_oi,
        min_long_oi=settings.risk_min_long_oi,
        min_short_volume=settings.risk_min_short_volume,
        min_long_volume=settings.risk_min_long_volume,
        require_volume=settings.risk_require_volume,
        max_bid_ask_spread_pct=settings.risk_max_bid_ask_spread_pct,
        entry_start_time=settings.risk_entry_start_time,
        entry_end_time=settings.risk_entry_end_time,
        min_regime_confidence=settings.risk_min_regime_confidence,
        min_evidence_quality=settings.risk_min_evidence_quality,
        require_bid_ask=settings.risk_require_bid_ask,
        allow_ltp_estimate=settings.risk_allow_ltp_estimate,
        max_snapshot_age_seconds=settings.risk_max_snapshot_age_seconds,
        allow_expiry_day=settings.risk_allow_expiry_day,
        require_intraday_oi=settings.risk_require_intraday_oi,
        required_consecutive_directional_snapshots=settings.risk_required_consecutive_directional_snapshots,
        require_candidate_stability=settings.risk_require_candidate_stability,
        candidate_stability_snapshots=settings.risk_candidate_stability_snapshots,
    )


def build_and_store_risk(
    repository: RiskRepository,
    snapshot_id: int,
    config: RiskConfig,
    context: EvaluationContext,
    *,
    state_provider: RiskStateProvider | None = None,
    event_provider: MarketEventProvider | None = None,
) -> RiskEvaluationSet:
    loaded = repository.load_context(snapshot_id)
    if loaded is None:
        raise LookupError(
            f"raw, phase3_v1, phase4_v1, and phase6_v1 context for snapshot {snapshot_id} is required"
        )
    snapshot, feature, _, regime, candidate_set_id, candidate_set = loaded
    prior_regimes = repository.prior_regimes(
        feature.timestamp, max(0, config.required_consecutive_directional_snapshots - 1)
    )
    prior_keys = repository.prior_candidate_keys(
        feature.timestamp, max(0, config.candidate_stability_snapshots - 1)
    )
    result = evaluate_risk(
        candidate_set, candidate_set_id, snapshot, feature, regime, config, context,
        state_provider or ResearchRiskStateProvider(),
        event_provider or ConfiguredMarketEventProvider(),
        prior_regimes=prior_regimes,
        prior_candidate_keys=prior_keys,
    )
    repository.upsert(result, candidate_set_id)
    return result


def backfill_risk(
    repository: RiskRepository,
    config: RiskConfig,
    *,
    state_provider: RiskStateProvider | None = None,
    event_provider: MarketEventProvider | None = None,
) -> list[RiskEvaluationSet]:
    return [
        build_and_store_risk(
            repository, snapshot_id, config, EvaluationContext.HISTORICAL,
            state_provider=state_provider, event_provider=event_provider,
        )
        for snapshot_id in repository.context_snapshot_ids_chronological()
    ]
