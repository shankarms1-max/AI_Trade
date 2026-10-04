from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    MarketFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
    MarketSnapshotRecord,
    RiskDecisionRecord,
    StrategyCandidateSetRecord,
)
from app.features.models import FEATURE_VERSION, MarketFeatureSnapshot
from app.features.repository import raw_record_to_model
from app.regime.models import REGIME_VERSION, RegimeResult
from app.risk.candidate_checks import strategy_fingerprint
from app.risk.models import RISK_VERSION, RiskDecision, RiskDecisionType, RiskEvaluationSet
from app.strategy.models import STRATEGY_VERSION, StrategyCandidateSet


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class RiskRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

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
                MarketRegimeSnapshotRecord.regime_version == REGIME_VERSION,
            ))
            candidates = session.scalar(select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.market_snapshot_id == snapshot_id,
                StrategyCandidateSetRecord.strategy_version == STRATEGY_VERSION,
            ))
            if raw is None or feature is None or regime is None or candidates is None:
                return None
            return (
                raw_record_to_model(raw),
                MarketFeatureSnapshot.model_validate(feature.feature_json),
                regime.id,
                RegimeResult.model_validate(regime.result_json),
                candidates.id,
                StrategyCandidateSet.model_validate(candidates.result_json),
            )

    def latest_context_snapshot_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(StrategyCandidateSetRecord.market_snapshot_id)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == StrategyCandidateSetRecord.market_snapshot_id)
                .where(StrategyCandidateSetRecord.strategy_version == STRATEGY_VERSION)
                .order_by(MarketSnapshotRecord.timestamp_ist.desc(), StrategyCandidateSetRecord.id.desc())
                .limit(1)
            )

    def context_snapshot_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(session.scalars(
                select(StrategyCandidateSetRecord.market_snapshot_id)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == StrategyCandidateSetRecord.market_snapshot_id)
                .where(StrategyCandidateSetRecord.strategy_version == STRATEGY_VERSION)
                .order_by(MarketSnapshotRecord.timestamp_ist, StrategyCandidateSetRecord.id)
            ))

    def prior_regimes(self, timestamp, limit: int) -> list[RegimeResult]:
        if limit <= 0:
            return []
        with self._sessions() as session:
            records = session.scalars(
                select(MarketRegimeSnapshotRecord)
                .join(MarketFeatureSnapshotRecord, MarketFeatureSnapshotRecord.id == MarketRegimeSnapshotRecord.feature_snapshot_id)
                .where(
                    MarketRegimeSnapshotRecord.regime_version == REGIME_VERSION,
                    MarketFeatureSnapshotRecord.timestamp < timestamp,
                )
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketRegimeSnapshotRecord.id.desc())
                .limit(limit)
            ).all()
            return [RegimeResult.model_validate(item.result_json) for item in records]

    def prior_candidate_keys(self, timestamp, limit: int) -> list[set[str]]:
        if limit <= 0:
            return []
        with self._sessions() as session:
            records = session.execute(
                select(StrategyCandidateSetRecord, MarketSnapshotRecord.timestamp_ist)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == StrategyCandidateSetRecord.market_snapshot_id)
                .where(
                    StrategyCandidateSetRecord.strategy_version == STRATEGY_VERSION,
                    MarketSnapshotRecord.timestamp_ist < timestamp,
                )
                .order_by(MarketSnapshotRecord.timestamp_ist.desc(), StrategyCandidateSetRecord.id.desc())
                .limit(limit)
            ).all()
            values: list[set[str]] = []
            for record, observed_at in records:
                candidate_set = StrategyCandidateSet.model_validate(record.result_json)
                values.append({
                    strategy_fingerprint(candidate, observed_at.date())
                    for candidate in candidate_set.candidates
                })
            return values

    def upsert(self, result: RiskEvaluationSet, candidate_set_id: int) -> None:
        fingerprints = {
            item.candidate_fingerprint or "NO_CANDIDATE" for item in result.decisions
        }
        with self._sessions.begin() as session:
            existing = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == result.snapshot_id,
                RiskDecisionRecord.strategy_version == result.strategy_version,
                RiskDecisionRecord.risk_version == result.risk_version,
            )).all()
            for decision in result.decisions:
                fingerprint = decision.candidate_fingerprint or "NO_CANDIDATE"
                record = next((item for item in existing if item.candidate_fingerprint == fingerprint), None)
                values = {
                    "regime_snapshot_id": decision.regime_snapshot_id,
                    "strategy_candidate_set_id": candidate_set_id,
                    "decision": decision.decision.value,
                    "strategy_type": decision.candidate_strategy,
                    "max_profit_per_unit": _decimal(decision.max_profit_per_unit),
                    "max_loss_per_unit": _decimal(decision.max_loss_per_unit),
                    "lot_size": decision.lot_size,
                    "max_profit_per_lot": _decimal(decision.max_profit_per_lot),
                    "max_loss_per_lot": _decimal(decision.max_loss_per_lot),
                    "failed_check_count": len(decision.failed_checks),
                    "warning_count": len(decision.warnings),
                    "result_json": decision.model_dump(mode="json"),
                    "created_at": decision.created_at,
                }
                if record is None:
                    session.add(RiskDecisionRecord(
                        market_snapshot_id=result.snapshot_id,
                        strategy_version=result.strategy_version,
                        risk_version=result.risk_version,
                        candidate_fingerprint=fingerprint,
                        **values,
                    ))
                else:
                    for key, value in values.items():
                        setattr(record, key, value)
            session.execute(delete(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == result.snapshot_id,
                RiskDecisionRecord.strategy_version == result.strategy_version,
                RiskDecisionRecord.risk_version == result.risk_version,
                RiskDecisionRecord.candidate_fingerprint.not_in(fingerprints),
            ))

    @staticmethod
    def _evaluation(records: list[RiskDecisionRecord]) -> dict[str, Any] | None:
        if not records:
            return None
        decisions = [RiskDecision.model_validate(item.result_json) for item in records]
        approved = [item for item in decisions if item.decision == RiskDecisionType.APPROVED]
        result = RiskEvaluationSet(
            snapshot_id=records[0].market_snapshot_id,
            strategy_version=records[0].strategy_version,
            risk_version=records[0].risk_version,
            candidate_count=sum(item.decision != RiskDecisionType.NOT_APPLICABLE for item in decisions),
            approved_count=len(approved),
            rejected_count=sum(item.decision == RiskDecisionType.REJECTED for item in decisions),
            not_applicable=any(item.decision == RiskDecisionType.NOT_APPLICABLE for item in decisions),
            decisions=decisions,
            best_approved_candidate=(
                max(approved, key=lambda item: item.selection_score or 0).candidate_reference
                if approved else None
            ),
            created_at=max(item.created_at for item in decisions),
        )
        return result.model_dump(mode="json")

    def get(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            records = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == snapshot_id,
                RiskDecisionRecord.risk_version == RISK_VERSION,
            ).order_by(RiskDecisionRecord.id)).all()
            return self._evaluation(list(records))

    def latest(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            snapshot_id = session.scalar(
                select(RiskDecisionRecord.market_snapshot_id)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == RiskDecisionRecord.market_snapshot_id)
                .where(RiskDecisionRecord.risk_version == RISK_VERSION)
                .order_by(MarketSnapshotRecord.timestamp_ist.desc(), RiskDecisionRecord.id.desc())
                .limit(1)
            )
        return None if snapshot_id is None else self.get(snapshot_id)

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self._sessions() as session:
            ids = list(session.scalars(
                select(RiskDecisionRecord.market_snapshot_id)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == RiskDecisionRecord.market_snapshot_id)
                .where(RiskDecisionRecord.risk_version == RISK_VERSION)
                .group_by(RiskDecisionRecord.market_snapshot_id, MarketSnapshotRecord.timestamp_ist)
                .order_by(MarketSnapshotRecord.timestamp_ist.desc())
                .limit(limit)
            ))
        return [item for snapshot_id in ids if (item := self.get(snapshot_id)) is not None]
