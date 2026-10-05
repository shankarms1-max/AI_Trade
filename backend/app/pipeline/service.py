from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.ai.openai_provider import OpenAIResearchProvider
from app.ai.repository import AIResearchRepository
from app.ai.service import build_and_store_ai_research, config_from_settings as ai_config
from app.alpha.engine import config_from_settings as alpha_config
from app.alpha.models import CalculationMode
from app.alpha.repository import AlphaRepository
from app.alpha.service import build_and_store_alpha
from app.features.engine import config_from_settings as feature_config
from app.features.repository import FeatureRepository
from app.features.service import build_and_store_features
from app.pipeline.orchestrator import PipelineSteps, ResearchPipelineOrchestrator
from app.pipeline.repository import PipelineRepository
from app.regime.engine import config_from_settings as regime_config
from app.regime.repository import RegimeRepository
from app.regime.service import build_and_store_regime
from app.risk.event_checks import ConfiguredMarketEventProvider
from app.risk.models import EvaluationContext
from app.risk.repository import RiskRepository
from app.risk.service import build_and_store_risk, config_from_settings as risk_config
from app.shadow.repository import ShadowRepository
from app.shadow.risk_state import ShadowRiskStateProvider
from app.shadow.service import (
    build_shadow_entry,
    config_from_settings as shadow_config,
    update_open_trades,
)
from app.strategy.repository import StrategyRepository
from app.strategy.service import build_and_store_candidates, config_from_settings as strategy_config
from app.observability.repository import ObservabilityRepository
from app.core.logging import get_logger

logger = get_logger(__name__)


def shadow_risk_config_from_settings(settings):
    """Build research-only shadow limits without changing future live limits."""
    return replace(
        risk_config(settings),
        capital_base=settings.shadow_risk_capital_base,
        max_loss_per_trade=settings.shadow_risk_max_loss_per_trade,
        max_capital_per_trade=settings.shadow_risk_max_capital_per_trade,
        max_daily_loss=settings.shadow_risk_max_daily_loss,
        max_trades_per_day=settings.shadow_risk_max_trades_per_day,
    )


def build_pipeline_orchestrator(
    session_factory: sessionmaker[Session], settings: Any, *, enable_ai: bool = False
) -> ResearchPipelineOrchestrator:
    feature_repository = FeatureRepository(session_factory)
    regime_repository = RegimeRepository(session_factory, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    strategy_repository = StrategyRepository(session_factory, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    risk_repository = RiskRepository(session_factory, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    shadow_repository = ShadowRepository(session_factory, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    alpha_repository = AlphaRepository(session_factory)

    ai_step = None
    if enable_ai:
        def run_ai(snapshot_id: int):
            if not settings.openai_api_key or not settings.ai_research_model:
                raise RuntimeError("OpenAI research settings are not configured")
            provider = OpenAIResearchProvider(
                settings.openai_api_key.get_secret_value(),
                settings.ai_research_model,
                settings.ai_max_retries,
            )
            candidates = None
            if settings.phase14_2_strategy_logic_enabled:
                from app.strategy.candidate_engine import generate_candidates
                raw, _, feature, regime_id, regime = strategy_repository.load_context(snapshot_id)
                candidates = generate_candidates(raw, feature, regime, regime_id, strategy_config(settings))
            return build_and_store_ai_research(
                AIResearchRepository(session_factory, regime_version=settings.active_regime_version,
                                     strategy_version=settings.active_strategy_version),
                snapshot_id, provider, ai_config(settings), candidate_set=candidates
            )
        ai_step = run_ai

    steps = PipelineSteps(
        shadow_update=lambda snapshot_id: update_open_trades(
            shadow_repository, snapshot_id, shadow_config(settings)
        ),
        features=lambda snapshot_id: build_and_store_features(
            feature_repository, snapshot_id, feature_config(settings)
        ),
        alpha=(
            lambda snapshot_id: build_and_store_alpha(
                alpha_repository, snapshot_id, alpha_config(settings), CalculationMode.LIVE_ORIGINAL
            )
        ) if settings.alpha_engine_enabled else None,
        regime=lambda snapshot_id: build_and_store_regime(
            regime_repository, snapshot_id, regime_config(settings)
        ),
        ai=ai_step,
        strategy=lambda snapshot_id: build_and_store_candidates(
            strategy_repository, snapshot_id, strategy_config(settings), enforce_freshness=False
        ),
        risk=lambda snapshot_id: build_and_store_risk(
            risk_repository,
            snapshot_id,
            shadow_risk_config_from_settings(settings),
            EvaluationContext.SHADOW,
            state_provider=ShadowRiskStateProvider(session_factory),
            event_provider=ConfiguredMarketEventProvider.from_json(settings.risk_market_events_json) if settings.phase14_2_strategy_logic_enabled else None,
        ),
        shadow_entry=lambda snapshot_id: build_shadow_entry(
            shadow_repository, snapshot_id, shadow_config(settings)
        ),
    )
    return ResearchPipelineOrchestrator(
        PipelineRepository(session_factory, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version), steps,
        ObservabilityRepository(session_factory),
    )


def get_daily_research_summary(
    session_factory: sessionmaker[Session], trading_date
) -> dict[str, Any]:
    return PipelineRepository(session_factory).daily_summary(trading_date)


def make_after_snapshot_callback(session_factory, settings, notification_router=None):
    orchestrator = build_pipeline_orchestrator(
        session_factory, settings, enable_ai=settings.pipeline_run_ai_research
    )
    if notification_router is None and settings.telegram_enabled:
        from app.notifications.router import NotificationRouter
        notification_router = NotificationRouter(session_factory, settings)

    def after_snapshot(snapshot_id):
        result = orchestrator.run(snapshot_id, run_ai=settings.pipeline_run_ai_research)
        if notification_router is not None:
            try:
                notification_router.notify_research(snapshot_id)
                notification_router.route_operational_events()
            except Exception as exc:
                # Notification failure must never change the persisted pipeline result.
                logger.error("NOTIFICATION_ROUTING_FAILED error_type=%s", type(exc).__name__)
        return result

    return after_snapshot
