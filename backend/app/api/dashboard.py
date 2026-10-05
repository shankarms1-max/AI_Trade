from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, sessionmaker
from typing import Annotated

from app.ai.repository import AIResearchRepository
from app.core.config import get_settings
from app.db.repositories import SnapshotRepository
from app.db.session import get_session_factory
from app.features.repository import FeatureRepository
from app.pipeline.repository import PipelineRepository
from app.regime.repository import RegimeRepository
from app.risk.repository import RiskRepository
from app.shadow.analytics import breakdown, performance
from app.shadow.repository import ShadowRepository
from app.shadow.exits import pnl_thresholds
from app.shadow.service import config_from_settings as shadow_config
from app.strategy.repository import StrategyRepository
from app.alpha.repository import AlphaRepository

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
IST = ZoneInfo("Asia/Kolkata")


SessionFactoryDependency = Annotated[sessionmaker[Session], Depends(get_session_factory)]


@router.get("/latest")
def latest_dashboard(sessions: SessionFactoryDependency) -> dict[str, Any]:
    """Compose existing read models; no research or trading logic runs here."""
    settings = get_settings()
    snapshots = SnapshotRepository(sessions)
    latest = snapshots.latest()
    snapshot_id = None if latest is None else latest["id"]
    shadow_repository = ShadowRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    all_trades = shadow_repository.all_trades()
    open_trades = [item for item in all_trades if item.status.value == "OPEN"]
    open_trade = open_trades[-1].model_dump(mode="json") if open_trades else None
    open_marks = [] if open_trade is None else shadow_repository.marks(open_trade["id"])
    pipeline_repository = PipelineRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    pipeline_runs = pipeline_repository.list(500)
    trading_date = (
        datetime.now(IST).date()
        if latest is None
        else latest["timestamp_ist"].astimezone(IST).date()
    )
    alpha_repository = AlphaRepository(sessions)
    open_thresholds = None
    if open_trades:
        target, stop = pnl_thresholds(open_trades[-1], shadow_config(settings))
        open_thresholds = {
            "profit_target_pnl_per_unit": target,
            "stop_loss_pnl_per_unit": stop,
            "force_exit_time": settings.shadow_force_exit_time.isoformat(),
        }
    return {
        "snapshot": latest,
        "features": None if snapshot_id is None else FeatureRepository(sessions).get(snapshot_id),
        "alpha": None if snapshot_id is None else alpha_repository.get(snapshot_id),
        "alpha_history": alpha_repository.history(trading_date),
        "regime": None if snapshot_id is None else RegimeRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version).get(snapshot_id),
        "ai_research": None if snapshot_id is None else AIResearchRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version).get(snapshot_id),
        "candidates": None if snapshot_id is None else StrategyRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version).get(snapshot_id),
        "risk": None if snapshot_id is None else RiskRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version).get(snapshot_id),
        "open_shadow_trade": open_trade,
        "open_shadow_marks": open_marks,
        "open_shadow_thresholds": open_thresholds,
        "shadow_trades": [item.model_dump(mode="json") for item in all_trades[-200:]],
        "pipeline": None if snapshot_id is None else pipeline_repository.get(snapshot_id),
        "pipeline_health": {
            "last_success": next((item for item in pipeline_runs if item["status"] == "SUCCESS"), None),
            "last_partial": next((item for item in pipeline_runs if item["status"] == "PARTIAL"), None),
            "last_failed": next((item for item in pipeline_runs if item["status"] == "FAILED"), None),
        },
        "daily_summary": pipeline_repository.daily_summary(trading_date),
        "regime_history": [
            item for item in RegimeRepository(sessions, regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version).list(200)
            if datetime.fromisoformat(item["timestamp"]).astimezone(IST).date() == trading_date
        ],
        "shadow_performance": performance(all_trades),
        "shadow_breakdown": breakdown(all_trades),
        "system": {
            "database_api": "CONNECTED",
            "collector_interval_minutes": settings.collector_interval_minutes,
            "collector_start_time": settings.collector_start_time.isoformat(),
            "collector_end_time": settings.collector_end_time.isoformat(),
            "ai_research_configured": bool(settings.openai_api_key and settings.ai_research_model),
            "server_time_ist": datetime.now(IST).isoformat(),
        },
    }
