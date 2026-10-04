from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.snapshots import router as snapshots_router
from app.api.features import router as features_router
from app.api.regime import router as regime_router
from app.api.ai_research import router as ai_research_router
from app.api.strategy_candidates import router as strategy_candidates_router
from app.api.risk import router as risk_router
from app.api.shadow import router as shadow_router
from app.api.pipeline import router as pipeline_router
from app.api.dashboard import router as dashboard_router
from app.api.system import router as system_router
from app.api.notifications import router as notifications_router
from app.api.alpha import router as alpha_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

_settings = get_settings()
configure_logging(
    _settings.log_level, _settings.log_dir, _settings.log_max_bytes,
    _settings.log_backup_count,
)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("APP_STARTED")
    yield


app = FastAPI(
    title="Nifty AI Credit Spread Research", version="0.3.0", lifespan=lifespan,
    docs_url="/docs" if _settings.enable_api_docs else None,
    redoc_url="/redoc" if _settings.enable_api_docs else None,
    openapi_url="/openapi.json" if _settings.enable_api_docs else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.configured_cors_origins),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(snapshots_router)
app.include_router(features_router)
app.include_router(regime_router)
app.include_router(ai_research_router)
app.include_router(strategy_candidates_router)
app.include_router(risk_router)
app.include_router(shadow_router)
app.include_router(pipeline_router)
app.include_router(dashboard_router)
app.include_router(system_router)
app.include_router(notifications_router)
app.include_router(alpha_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "phase": "continuous-raw-market-data-recorder"}
