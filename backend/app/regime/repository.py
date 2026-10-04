from decimal import Decimal
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AlphaFeatureSnapshotRecord, MarketFeatureSnapshotRecord, MarketRegimeSnapshotRecord
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot
from app.features.models import MarketFeatureSnapshot
from app.regime.models import REGIME_VERSION, RegimeResult


class RegimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def load_feature(self, snapshot_id: int) -> tuple[int, MarketFeatureSnapshot] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(MarketFeatureSnapshotRecord).where(
                    MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                    MarketFeatureSnapshotRecord.feature_version == "phase3_v1",
                )
            )
            if record is None:
                return None
            return record.id, MarketFeatureSnapshot.model_validate(record.feature_json)

    def load_prior_feature(self, timestamp) -> MarketFeatureSnapshot | None:
        with self._sessions() as session:
            record = session.scalar(
                select(MarketFeatureSnapshotRecord)
                .where(
                    MarketFeatureSnapshotRecord.feature_version == "phase3_v1",
                    MarketFeatureSnapshotRecord.timestamp < timestamp,
                )
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketFeatureSnapshotRecord.id.desc())
                .limit(1)
            )
            return None if record is None else MarketFeatureSnapshot.model_validate(record.feature_json)

    def load_alpha(self, snapshot_id: int) -> AlphaFeatureSnapshot | None:
        with self._sessions() as session:
            record = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode != "RESEARCH_RECOMPUTE",
            ).order_by(case((AlphaFeatureSnapshotRecord.calculation_mode == "LIVE_ORIGINAL", 0), else_=1)))
            return None if record is None else AlphaFeatureSnapshot.model_validate(record.result_json)

    def latest_feature_snapshot_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(MarketFeatureSnapshotRecord.market_snapshot_id)
                .where(MarketFeatureSnapshotRecord.feature_version == "phase3_v1")
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketFeatureSnapshotRecord.id.desc())
                .limit(1)
            )

    def feature_snapshot_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(session.scalars(
                select(MarketFeatureSnapshotRecord.market_snapshot_id)
                .where(MarketFeatureSnapshotRecord.feature_version == "phase3_v1")
                .order_by(MarketFeatureSnapshotRecord.timestamp, MarketFeatureSnapshotRecord.id)
            ))

    def upsert(self, result: RegimeResult) -> int:
        payload = result.model_dump(mode="json")
        with self._sessions.begin() as session:
            record = session.scalar(
                select(MarketRegimeSnapshotRecord).where(
                    MarketRegimeSnapshotRecord.market_snapshot_id == result.snapshot_id,
                    MarketRegimeSnapshotRecord.regime_version == result.regime_version,
                )
            )
            values = {
                "feature_snapshot_id": result.feature_snapshot_id,
                "regime": result.regime.value,
                "confidence": Decimal(str(result.confidence)),
                "evidence_quality": result.evidence_quality.value,
                "bull_score": Decimal(str(result.bull_score)),
                "bear_score": Decimal(str(result.bear_score)),
                "range_score": Decimal(str(result.range_score)),
                "result_json": payload,
            }
            if record is None:
                record = MarketRegimeSnapshotRecord(
                    market_snapshot_id=result.snapshot_id,
                    regime_version=result.regime_version,
                    **values,
                )
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.flush()
            return record.id

    def get(self, snapshot_id: int, version: str = REGIME_VERSION) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketRegimeSnapshotRecord.regime_version == version,
            ))
            return None if record is None else record.result_json

    def latest(self, version: str = REGIME_VERSION) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(MarketRegimeSnapshotRecord)
                .join(
                    MarketFeatureSnapshotRecord,
                    MarketFeatureSnapshotRecord.id
                    == MarketRegimeSnapshotRecord.feature_snapshot_id,
                )
                .where(MarketRegimeSnapshotRecord.regime_version == version)
                .order_by(
                    MarketFeatureSnapshotRecord.timestamp.desc(),
                    MarketRegimeSnapshotRecord.id.desc(),
                )
                .limit(1)
            )
            return None if record is None else record.result_json

    def list(self, limit: int, version: str = REGIME_VERSION) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(MarketRegimeSnapshotRecord)
                .join(
                    MarketFeatureSnapshotRecord,
                    MarketFeatureSnapshotRecord.id
                    == MarketRegimeSnapshotRecord.feature_snapshot_id,
                )
                .where(MarketRegimeSnapshotRecord.regime_version == version)
                .order_by(
                    MarketFeatureSnapshotRecord.timestamp.desc(),
                    MarketRegimeSnapshotRecord.id.desc(),
                )
                .limit(limit)
            ).all()
            return [record.result_json for record in records]
