from decimal import Decimal
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.models import AI_VERSION, PROMPT_VERSION, AIResearchResult
from app.db.models import (
    AIResearchSnapshotRecord,
    AlphaFeatureSnapshotRecord,
    MarketFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
)
from app.features.models import MarketFeatureSnapshot
from app.regime.models import RegimeResult
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot


class AIResearchRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, regime_version="phase4_v1", strategy_version="phase6_v1") -> None:
        self._sessions = session_factory
        self.regime_version = regime_version
        self.strategy_version = strategy_version
        self.ai_version = "phase14_2_ai_v1" if regime_version == "phase14_2_v1" else AI_VERSION
        self.prompt_version = "phase14_2_prompt_v1" if regime_version == "phase14_2_v1" else PROMPT_VERSION

    def load_context(
        self, snapshot_id: int
    ) -> tuple[int, MarketFeatureSnapshot, int, RegimeResult] | None:
        with self._sessions() as session:
            feature = session.scalar(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketFeatureSnapshotRecord.feature_version == "phase3_v1",
            ))
            regime = session.scalar(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketRegimeSnapshotRecord.regime_version == self.regime_version,
                MarketRegimeSnapshotRecord.result_json.is_not(None),
            ))
            if feature is None or regime is None:
                return None
            return (
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
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc())
                .limit(1)
            )

    def load_candidates(self, snapshot_id):
        from app.strategy.repository import StrategyRepository
        from app.strategy.models import StrategyCandidateSet
        payload = StrategyRepository(self._sessions, regime_version=self.regime_version, strategy_version=self.strategy_version).get(snapshot_id)
        return None if payload is None else StrategyCandidateSet.model_validate(payload)

    def load_alpha(self, snapshot_id: int) -> AlphaFeatureSnapshot | None:
        with self._sessions() as session:
            record = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode != "RESEARCH_RECOMPUTE",
            ).order_by(case((AlphaFeatureSnapshotRecord.calculation_mode == "LIVE_ORIGINAL", 0), else_=1)))
            return None if record is None else AlphaFeatureSnapshot.model_validate(record.result_json)

    def existing_success(self, snapshot_id: int) -> AIResearchResult | None:
        with self._sessions() as session:
            record = session.scalar(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id == snapshot_id,
                AIResearchSnapshotRecord.ai_version == self.ai_version,
                AIResearchSnapshotRecord.status == "SUCCESS",
            ))
            return None if record is None else AIResearchResult.model_validate(record.result_json)

    def start(
        self, snapshot_id: int, feature_id: int, regime_id: int, provider: str, model: str
    ) -> None:
        with self._sessions.begin() as session:
            record = session.scalar(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id == snapshot_id,
                AIResearchSnapshotRecord.ai_version == self.ai_version,
            ))
            values = {
                "feature_snapshot_id": feature_id,
                "regime_snapshot_id": regime_id,
                "prompt_version": self.prompt_version,
                "provider": provider,
                "model": model,
                "status": "STARTED",
                "result_json": None,
                "error_type": None,
                "safe_error_message": None,
                "market_view": None,
                "confidence": None,
                "original_ai_confidence": None,
                "confidence_capped": False,
                "agreement_status": None,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "latency_ms": None,
                "estimated_cost_usd": None,
            }
            if record is None:
                session.add(AIResearchSnapshotRecord(
                    market_snapshot_id=snapshot_id, ai_version=self.ai_version, **values
                ))
            else:
                for key, value in values.items():
                    setattr(record, key, value)

    def complete(self, result: AIResearchResult) -> None:
        with self._sessions.begin() as session:
            record = session.scalar(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id == result.snapshot_id,
                AIResearchSnapshotRecord.ai_version == result.ai_version,
            ))
            if record is None:
                raise LookupError("AI research run was not started")
            record.status = "SUCCESS"
            record.market_view = result.market_view.value
            record.confidence = Decimal(str(result.confidence))
            record.original_ai_confidence = Decimal(str(result.original_ai_confidence))
            record.confidence_capped = result.confidence_capped
            record.agreement_status = result.agreement_status.value
            record.result_json = result.model_dump(mode="json")
            record.input_tokens = result.input_tokens
            record.output_tokens = result.output_tokens
            record.total_tokens = result.total_tokens
            record.latency_ms = result.latency_ms
            record.estimated_cost_usd = (
                None if result.estimated_cost_usd is None
                else Decimal(str(result.estimated_cost_usd))
            )

    def fail(self, snapshot_id: int, error: BaseException) -> None:
        with self._sessions.begin() as session:
            record = session.scalar(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id == snapshot_id,
                AIResearchSnapshotRecord.ai_version == self.ai_version,
            ))
            if record is None:
                return
            record.status = "FAILED"
            record.error_type = type(error).__name__[:120]
            record.safe_error_message = "AI research generation or validation failed"

    def _serialize(self, record: AIResearchSnapshotRecord) -> dict[str, Any]:
        return record.result_json or {
            "snapshot_id": record.market_snapshot_id,
            "ai_version": record.ai_version,
            "prompt_version": record.prompt_version,
            "provider": record.provider,
            "model": record.model,
            "status": record.status,
            "error_type": record.error_type,
            "safe_error_message": record.safe_error_message,
        }

    def get(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id == snapshot_id,
                AIResearchSnapshotRecord.ai_version == self.ai_version,
            ))
            return None if record is None else self._serialize(record)

    def latest(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(AIResearchSnapshotRecord)
                .join(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.id == AIResearchSnapshotRecord.feature_snapshot_id)
                .where(AIResearchSnapshotRecord.ai_version == self.ai_version)
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc())
                .limit(1)
            )
            return None if record is None else self._serialize(record)

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(AIResearchSnapshotRecord)
                .join(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.id == AIResearchSnapshotRecord.feature_snapshot_id)
                .where(AIResearchSnapshotRecord.ai_version == self.ai_version)
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc())
                .limit(limit)
            ).all()
            return [self._serialize(record) for record in records]
