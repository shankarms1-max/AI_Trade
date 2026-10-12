"""Phase 15 local capture adapter for the unchanged Phase 14.1 Alpha service."""
from __future__ import annotations

from datetime import datetime
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.alpha.engine import AlphaConfig, config_from_settings
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot, CalculationMode
from app.alpha.repository import AlphaRepository
from app.alpha.service import build_and_store_alpha
from app.data.models import MarketSnapshot
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.repository import raw_record_to_model
from app.scalper.repository import IST

ACTS_ALPHA_SOURCE = "KOTAK_NEO_ACTS_PAPER"
ALPHA_CAPTURE_MINUTES = 3


def alpha_bucket(at: datetime) -> datetime:
    """Stable 3-minute key distinct from the existing collector's bucket."""
    local = at.astimezone(IST)
    return local.replace(minute=local.minute // ALPHA_CAPTURE_MINUTES
                         * ALPHA_CAPTURE_MINUTES, second=0, microsecond=1)


class ActsAlphaRepository(AlphaRepository):
    """Bounded, indexed ACTS-only reads; inherited LIVE_ORIGINAL upsert stays intact."""

    def __init__(self, sessions: sessionmaker[Session], config: AlphaConfig) -> None:
        super().__init__(sessions)
        self.sessions = sessions
        dependency = max(config.alpha1_lookback_minutes,
                         config.alpha2_lookback_minutes + config.volatility_lookback_minutes,
                         config.alpha2_lookback_minutes + config.volume_lookback_minutes)
        required_minutes = dependency + config.max_horizon_seconds / 60 + 2
        self.history_limit = max(500, ceil(required_minutes / ALPHA_CAPTURE_MINUTES) + 128)

    def load_history_before(self, timestamp: datetime) -> list[MarketSnapshot]:
        with self.sessions() as session:
            records = session.scalars(select(MarketSnapshotRecord).options(
                selectinload(MarketSnapshotRecord.options)).where(
                    MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE,
                    MarketSnapshotRecord.timestamp_ist < timestamp).order_by(
                        MarketSnapshotRecord.timestamp_ist.desc(),
                        MarketSnapshotRecord.id.desc()).limit(self.history_limit)).all()
            return [raw_record_to_model(row, normalize_research_timestamps=True)
                    for row in reversed(records)]

    def load_raw(self, snapshot_id: int) -> MarketSnapshot | None:
        with self.sessions() as session:
            record = session.scalar(select(MarketSnapshotRecord).options(
                selectinload(MarketSnapshotRecord.options)).where(
                    MarketSnapshotRecord.id == snapshot_id,
                    MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE))
            return (None if record is None else raw_record_to_model(
                record, normalize_research_timestamps=True))

    def load_alpha_before(self, timestamp: datetime, version: str = ALPHA_VERSION,
                          mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY
                          ) -> list[AlphaFeatureSnapshot]:
        with self.sessions() as session:
            records = session.scalars(select(AlphaFeatureSnapshotRecord).join(
                MarketSnapshotRecord,
                AlphaFeatureSnapshotRecord.market_snapshot_id == MarketSnapshotRecord.id
            ).where(MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE,
                    AlphaFeatureSnapshotRecord.alpha_version == version,
                    AlphaFeatureSnapshotRecord.calculation_mode == mode.value,
                    AlphaFeatureSnapshotRecord.timestamp < timestamp).order_by(
                        AlphaFeatureSnapshotRecord.timestamp.desc(),
                        AlphaFeatureSnapshotRecord.id.desc()).limit(300)).all()
            return [AlphaFeatureSnapshot.model_validate(row.result_json)
                    for row in reversed(records)]


class LocalActsAlpha:
    """Persist one exact Kotak market observation and Alpha result per 3-minute bucket."""

    def __init__(self, sessions: sessionmaker[Session], settings: object) -> None:
        self.sessions = sessions
        self.config = config_from_settings(settings)
        if self.config.price_source != "FUTURE":
            raise ValueError("ACTS_LOCAL_ALPHA_REQUIRES_FUTURE_PRICE_SOURCE")
        self.raw = SnapshotRepository(sessions)
        self.alpha = ActsAlphaRepository(sessions, self.config)

    def observe(self, market: MarketSnapshot) -> AlphaFeatureSnapshot:
        bucket = alpha_bucket(market.timestamp_ist)
        with self.sessions() as session:
            raw_result = session.execute(select(
                MarketSnapshotRecord.id, MarketSnapshotRecord.source).where(
                MarketSnapshotRecord.collection_bucket_ist == bucket))
            raw_row = raw_result.one_or_none()
        if raw_row is not None and raw_row.source != ACTS_ALPHA_SOURCE:
            raise ValueError("ACTS_ALPHA_BUCKET_OWNED_BY_ANOTHER_SOURCE")
        raw_id = None if raw_row is None else raw_row.id
        if raw_id is None:
            source = market.model_copy(update={"source": ACTS_ALPHA_SOURCE})
            raw_id = self.raw.save_market_snapshot(source, bucket).snapshot_id
        with self.sessions() as session:
            owner = session.scalar(select(MarketSnapshotRecord.source).where(
                MarketSnapshotRecord.id == raw_id))
        if owner != ACTS_ALPHA_SOURCE:
            raise ValueError("ACTS_ALPHA_BUCKET_OWNED_BY_ANOTHER_SOURCE")
        with self.sessions() as session:
            stored = session.scalar(select(AlphaFeatureSnapshotRecord.result_json).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == raw_id,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode == CalculationMode.LIVE_ORIGINAL.value))
        if stored is not None:
            return AlphaFeatureSnapshot.model_validate(stored)
        return build_and_store_alpha(self.alpha, raw_id, self.config,
                                     CalculationMode.LIVE_ORIGINAL)
