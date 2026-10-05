from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    MarketFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
    MarketSnapshotRecord,
    StrategyCandidateSetRecord,
)
from app.features.models import FEATURE_VERSION, MarketFeatureSnapshot
from app.features.repository import raw_record_to_model
from app.regime.models import REGIME_VERSION, RegimeResult
from app.strategy.models import STRATEGY_VERSION, StrategyCandidateSet


class StrategyRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, regime_version: str = "phase4_v1", strategy_version: str = "phase6_v1") -> None:
        self._sessions = session_factory
        self.regime_version = regime_version
        self.strategy_version = strategy_version

    def load_context(self, snapshot_id: int):
        with self._sessions() as session:
            raw = session.scalar(
                select(MarketSnapshotRecord)
                .options(selectinload(MarketSnapshotRecord.options))
                .where(MarketSnapshotRecord.id == snapshot_id)
            )
            feature = session.scalar(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketFeatureSnapshotRecord.feature_version == FEATURE_VERSION,
            ))
            regime = session.scalar(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketRegimeSnapshotRecord.regime_version == self.regime_version,
            ))
            if raw is None or feature is None or regime is None:
                return None
            return (
                raw_record_to_model(raw),
                feature.id,
                MarketFeatureSnapshot.model_validate(feature.feature_json),
                regime.id,
                RegimeResult.model_validate(regime.result_json),
            )

    def latest_context_snapshot_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(MarketRegimeSnapshotRecord.market_snapshot_id)
                .join(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.id == MarketRegimeSnapshotRecord.feature_snapshot_id)
                .where(MarketRegimeSnapshotRecord.regime_version == self.regime_version)
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketRegimeSnapshotRecord.id.desc())
                .limit(1)
            )

    def context_snapshot_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(session.scalars(
                select(MarketRegimeSnapshotRecord.market_snapshot_id)
                .join(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.id == MarketRegimeSnapshotRecord.feature_snapshot_id)
                .where(MarketRegimeSnapshotRecord.regime_version == self.regime_version)
                .order_by(MarketFeatureSnapshotRecord.timestamp, MarketRegimeSnapshotRecord.id)
            ))

    def upsert(self, result: StrategyCandidateSet) -> int:
        from app.research.isolation import reject_shared_research_write
        reject_shared_research_write(result)
        for candidate in result.candidates:
            reject_shared_research_write(candidate)
        values = {
            "strategy_logic_version": result.strategy_logic_version,
            "strategy_family": None if result.strategy_family is None else result.strategy_family.value,
            "strategy_context_json": None if result.strategy_logic_version is None else result.model_dump(mode="json"),
            "regime_snapshot_id": result.regime_snapshot_id,
            "regime": result.regime,
            "strategy_type": result.strategy_type.value,
            "eligible": result.eligible,
            "candidate_count": result.candidate_count,
            "result_json": result.model_dump(mode="json"),
            "created_at": result.created_at,
        }
        with self._sessions.begin() as session:
            record = session.scalar(select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.market_snapshot_id == result.snapshot_id,
                StrategyCandidateSetRecord.strategy_version == result.strategy_version,
            ))
            if record is None:
                record = StrategyCandidateSetRecord(
                    market_snapshot_id=result.snapshot_id,
                    strategy_version=result.strategy_version,
                    **values,
                )
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.flush()
            return record.id

    @staticmethod
    def _serialize(record: StrategyCandidateSetRecord, full: bool = True) -> dict[str, Any]:
        if full:
            return record.result_json
        return {
            "snapshot_id": record.market_snapshot_id,
            "regime_snapshot_id": record.regime_snapshot_id,
            "strategy_version": record.strategy_version,
            "regime": record.regime,
            "strategy_type": record.strategy_type,
            "eligible": record.eligible,
            "candidate_count": record.candidate_count,
            "created_at": record.created_at,
        }

    def get(self, snapshot_id: int, version: str | None = None) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.market_snapshot_id == snapshot_id,
                StrategyCandidateSetRecord.strategy_version == (version or self.strategy_version),
            ))
            return None if record is None else self._serialize(record)

    def latest(self, version: str | None = None) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(StrategyCandidateSetRecord)
                .where(StrategyCandidateSetRecord.strategy_version == (version or self.strategy_version))
                .order_by(StrategyCandidateSetRecord.created_at.desc(), StrategyCandidateSetRecord.id.desc())
                .limit(1)
            )
            return None if record is None else self._serialize(record)

    def list(self, limit: int, version: str | None = None) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(StrategyCandidateSetRecord)
                .where(StrategyCandidateSetRecord.strategy_version == (version or self.strategy_version))
                .order_by(StrategyCandidateSetRecord.created_at.desc(), StrategyCandidateSetRecord.id.desc())
                .limit(limit)
            ).all()
            return [self._serialize(record, full=False) for record in records]
