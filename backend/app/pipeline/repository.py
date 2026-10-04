from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    AIResearchSnapshotRecord,
    AlphaFeatureSnapshotRecord,
    MarketFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
    MarketSnapshotRecord,
    PipelineRunRecord,
    RiskDecisionRecord,
    ShadowTradeRecord,
    StrategyCandidateSetRecord,
)
from app.pipeline.models import PIPELINE_VERSION, PipelineRun


class PipelineRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def raw_exists(self, snapshot_id: int) -> bool:
        with self._sessions() as session:
            return session.get(MarketSnapshotRecord, snapshot_id) is not None

    def raw_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(session.scalars(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id
            )))

    def latest_raw_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), MarketSnapshotRecord.id.desc()
            ).limit(1))

    def save(self, run: PipelineRun) -> PipelineRun:
        values = run.model_dump(exclude={"id"})
        for key in ("status", "feature_status", "alpha_status", "regime_status", "ai_status",
                    "strategy_status", "risk_status", "shadow_status"):
            values[key] = values[key].value
        with self._sessions.begin() as session:
            record = session.scalar(select(PipelineRunRecord).where(
                PipelineRunRecord.market_snapshot_id == run.market_snapshot_id,
                PipelineRunRecord.pipeline_version == run.pipeline_version,
            ))
            if record is None:
                record = PipelineRunRecord(**values)
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.flush()
            return run.model_copy(update={"id": record.id})

    def related_ids(self, snapshot_id: int) -> dict[str, int | None]:
        with self._sessions() as session:
            def scalar(model, condition):
                return session.scalar(select(model.id).where(condition).order_by(model.id.desc()).limit(1))
            return {
                "feature_snapshot_id": scalar(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id),
                "alpha_feature_snapshot_id": scalar(AlphaFeatureSnapshotRecord, AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id),
                "regime_snapshot_id": scalar(MarketRegimeSnapshotRecord, MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id),
                "ai_research_id": scalar(AIResearchSnapshotRecord, AIResearchSnapshotRecord.market_snapshot_id == snapshot_id),
                "strategy_candidate_set_id": scalar(StrategyCandidateSetRecord, StrategyCandidateSetRecord.market_snapshot_id == snapshot_id),
            }

    @staticmethod
    def _dict(record: PipelineRunRecord) -> dict[str, Any]:
        return {column.name: getattr(record, column.name) for column in record.__table__.columns}

    def get(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(PipelineRunRecord).where(
                PipelineRunRecord.market_snapshot_id == snapshot_id,
                PipelineRunRecord.pipeline_version == PIPELINE_VERSION,
            ))
            return None if record is None else self._dict(record)

    def latest(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(PipelineRunRecord).join(
                MarketSnapshotRecord, MarketSnapshotRecord.id == PipelineRunRecord.market_snapshot_id
            ).where(PipelineRunRecord.pipeline_version == PIPELINE_VERSION).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), PipelineRunRecord.id.desc()
            ).limit(1))
            return None if record is None else self._dict(record)

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(select(PipelineRunRecord).join(
                MarketSnapshotRecord, MarketSnapshotRecord.id == PipelineRunRecord.market_snapshot_id
            ).where(PipelineRunRecord.pipeline_version == PIPELINE_VERSION).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), PipelineRunRecord.id.desc()
            ).limit(limit)).all()
            return [self._dict(item) for item in records]

    def daily_summary(self, trading_date: date) -> dict[str, Any]:
        with self._sessions() as session:
            snapshot_ids = list(session.scalars(select(MarketSnapshotRecord.id).where(
                func.date(MarketSnapshotRecord.timestamp_ist) == trading_date
            )))
            if not snapshot_ids:
                snapshot_ids = [-1]
            features = session.scalars(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id.in_(snapshot_ids)
            )).all()
            regimes = session.scalars(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id.in_(snapshot_ids)
            )).all()
            regime_counts = {name: 0 for name in ("BULLISH", "BEARISH", "RANGE", "NO_TRADE")}
            for item in regimes:
                regime_counts[item.regime] += 1
            risk_rows = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id.in_(snapshot_ids)
            )).all()
            entries = session.scalars(select(ShadowTradeRecord).where(
                func.date(ShadowTradeRecord.entry_timestamp) == trading_date
            )).all()
            exits = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.status == "CLOSED",
                func.date(ShadowTradeRecord.exit_timestamp) == trading_date,
            )).all()
            ai_rows = session.scalars(select(AIResearchSnapshotRecord).where(
                AIResearchSnapshotRecord.market_snapshot_id.in_(snapshot_ids),
                AIResearchSnapshotRecord.status == "SUCCESS",
            )).all()
            return {
                "date": trading_date.isoformat(),
                "snapshots_collected": len([i for i in snapshot_ids if i != -1]),
                "snapshots_with_usable_oi": sum(
                    bool(item.feature_json.get("data_quality", {}).get("intraday_oi_usable"))
                    for item in features
                ),
                "regime_counts": regime_counts,
                "candidate_sets_created": session.scalar(select(func.count(StrategyCandidateSetRecord.id)).where(
                    StrategyCandidateSetRecord.market_snapshot_id.in_(snapshot_ids)
                )) or 0,
                "approved_risk_decisions": sum(item.decision == "APPROVED" for item in risk_rows),
                "rejected_risk_decisions": sum(item.decision == "REJECTED" for item in risk_rows),
                "shadow_entries": len(entries),
                "shadow_exits": len(exits),
                "shadow_realized_pnl": sum(float(item.realized_pnl_per_lot) for item in exits if item.realized_pnl_per_lot is not None),
                "open_shadow_trades": session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                    ShadowTradeRecord.status == "OPEN"
                )) or 0,
                "ai_calls": len(ai_rows),
                "ai_cost": sum(float(item.estimated_cost_usd) for item in ai_rows if item.estimated_cost_usd is not None),
            }
