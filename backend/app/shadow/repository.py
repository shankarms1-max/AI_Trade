from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import (
    MarketFeatureSnapshotRecord,
    AlphaFeatureSnapshotRecord,
    MarketRegimeSnapshotRecord,
    MarketSnapshotRecord,
    RiskDecisionRecord,
    ShadowTradeMarkRecord,
    ShadowTradeRecord,
    StrategyCandidateSetRecord,
)
from app.strategy.economics_models import CreditSpreadEconomics
from app.features.models import FEATURE_VERSION, MarketFeatureSnapshot
from app.features.repository import raw_record_to_model
from app.regime.models import REGIME_VERSION, RegimeResult
from app.risk.models import RISK_VERSION, RiskDecision
from app.shadow.models import SHADOW_VERSION, ShadowTrade, ShadowTradeMark
from app.strategy.models import STRATEGY_VERSION, StrategyCandidateSet
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class ShadowRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, regime_version: str = "phase4_v1", strategy_version: str = "phase6_v1") -> None:
        self._sessions = session_factory
        self.regime_version = regime_version
        self.strategy_version = strategy_version

    def load_entry_context(self, snapshot_id: int):
        with self._sessions() as session:
            raw = session.scalar(select(MarketSnapshotRecord).options(
                selectinload(MarketSnapshotRecord.options)
            ).where(MarketSnapshotRecord.id == snapshot_id))
            feature = session.scalar(select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketFeatureSnapshotRecord.feature_version == FEATURE_VERSION,
            ))
            regime = session.scalar(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketRegimeSnapshotRecord.regime_version == self.regime_version,
            ))
            candidates = session.scalar(select(StrategyCandidateSetRecord).where(
                StrategyCandidateSetRecord.market_snapshot_id == snapshot_id,
                StrategyCandidateSetRecord.strategy_version == self.strategy_version,
            ))
            risks = session.scalars(select(RiskDecisionRecord).where(
                RiskDecisionRecord.market_snapshot_id == snapshot_id,
                RiskDecisionRecord.risk_version == RISK_VERSION,
                RiskDecisionRecord.strategy_version == self.strategy_version,
                RiskDecisionRecord.decision == "APPROVED",
            )).all()
            alpha = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode != "RESEARCH_RECOMPUTE",
            ).order_by(case((AlphaFeatureSnapshotRecord.calculation_mode == "LIVE_ORIGINAL", 0), else_=1)))
            if raw is None or feature is None or regime is None or candidates is None:
                return None
            return (
                raw_record_to_model(raw),
                MarketFeatureSnapshot.model_validate(feature.feature_json),
                RegimeResult.model_validate(regime.result_json),
                StrategyCandidateSet.model_validate(candidates.result_json),
                [(item.id, RiskDecision.model_validate(item.result_json)) for item in risks],
                None if alpha is None else AlphaFeatureSnapshot.model_validate(alpha.result_json),
            )

    def latest_risk_snapshot_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(RiskDecisionRecord.market_snapshot_id)
                .join(MarketSnapshotRecord, MarketSnapshotRecord.id == RiskDecisionRecord.market_snapshot_id)
                .where(RiskDecisionRecord.risk_version == RISK_VERSION)
                .order_by(MarketSnapshotRecord.timestamp_ist.desc(), RiskDecisionRecord.id.desc())
                .limit(1)
            )

    def latest_snapshot_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), MarketSnapshotRecord.id.desc()
            ).limit(1))

    def count_entries_on(self, day: date) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                func.date(ShadowTradeRecord.entry_timestamp) == day,
                ShadowTradeRecord.shadow_version == SHADOW_VERSION,
            )) or 0

    def has_open_trade(self) -> bool:
        with self._sessions() as session:
            return bool(session.scalar(select(func.count(ShadowTradeRecord.id)).where(
                ShadowTradeRecord.status == "OPEN",
                ShadowTradeRecord.shadow_version == SHADOW_VERSION,
            )))

    def has_fingerprint(self, fingerprint: str) -> bool:
        with self._sessions() as session:
            return session.scalar(select(ShadowTradeRecord.id).where(
                ShadowTradeRecord.candidate_fingerprint == fingerprint,
                ShadowTradeRecord.shadow_version == SHADOW_VERSION,
            )) is not None

    def create_trade(self, trade: ShadowTrade) -> ShadowTrade:
        from app.research.isolation import reject_shared_research_write
        reject_shared_research_write(trade)
        with self._sessions.begin() as session:
            existing = session.scalar(select(ShadowTradeRecord).where(
                ShadowTradeRecord.candidate_fingerprint == trade.candidate_fingerprint,
                ShadowTradeRecord.shadow_version == trade.shadow_version,
            ))
            if existing is not None:
                return ShadowTrade.model_validate(existing.result_json)
            record = ShadowTradeRecord(
                strategy_logic_version=trade.strategy_logic_version,
                strategy_family=None if trade.strategy_family is None else trade.strategy_family.value,
                strategy_context_json=None if trade.strategy_logic_version is None else {
                    key: trade.model_dump(mode="json")[key] for key in CreditSpreadEconomics.model_fields},
                market_snapshot_id_entry=trade.market_snapshot_id_entry,
                risk_decision_id=trade.risk_decision_id,
                candidate_fingerprint=trade.candidate_fingerprint,
                shadow_version=trade.shadow_version,
                strategy_type=trade.strategy_type,
                expiry=trade.expiry,
                entry_timestamp=trade.entry_timestamp,
                entry_spot=_decimal(trade.entry_spot),
                entry_credit=_decimal(trade.entry_credit),
                entry_pricing_basis=trade.entry_pricing_basis.value,
                spread_width=_decimal(trade.spread_width),
                lot_size=trade.lot_size,
                max_profit_per_unit=_decimal(trade.max_profit_per_unit),
                max_loss_per_unit=_decimal(trade.max_loss_per_unit),
                max_profit_per_lot=_decimal(trade.max_profit_per_lot),
                max_loss_per_lot=_decimal(trade.max_loss_per_lot),
                status=trade.status.value,
                mae_per_unit=_decimal(trade.mae_per_unit),
                mfe_per_unit=_decimal(trade.mfe_per_unit),
                mae_per_lot=_decimal(trade.mae_per_lot),
                mfe_per_lot=_decimal(trade.mfe_per_lot),
                result_json={},
                created_at=trade.created_at,
                updated_at=trade.updated_at,
            )
            session.add(record)
            session.flush()
            stored = trade.model_copy(update={"id": record.id})
            record.result_json = stored.model_dump(mode="json")
            return stored

    def save_update(self, trade: ShadowTrade, mark: ShadowTradeMark | None) -> None:
        from app.research.isolation import reject_shared_research_write
        reject_shared_research_write(trade)
        if trade.id is None:
            raise ValueError("shadow trade must be persisted before update")
        with self._sessions.begin() as session:
            record = session.get(ShadowTradeRecord, trade.id)
            if record is None:
                raise LookupError(f"shadow trade {trade.id} not found")
            record.status = trade.status.value
            record.exit_market_snapshot_id = trade.exit_market_snapshot_id
            record.exit_timestamp = trade.exit_timestamp
            record.exit_reason = trade.exit_reason
            record.realized_pnl_per_unit = _decimal(trade.realized_pnl_per_unit)
            record.realized_pnl_per_lot = _decimal(trade.realized_pnl_per_lot)
            record.mae_per_unit = _decimal(trade.mae_per_unit)
            record.mfe_per_unit = _decimal(trade.mfe_per_unit)
            record.mae_per_lot = _decimal(trade.mae_per_lot)
            record.mfe_per_lot = _decimal(trade.mfe_per_lot)
            record.holding_minutes = _decimal(trade.holding_minutes)
            record.result_json = trade.model_dump(mode="json")
            record.updated_at = trade.updated_at
            if mark is not None:
                existing = session.scalar(select(ShadowTradeMarkRecord).where(
                    ShadowTradeMarkRecord.shadow_trade_id == trade.id,
                    ShadowTradeMarkRecord.market_snapshot_id == mark.market_snapshot_id,
                ))
                stored_mark = mark.model_copy(update={"shadow_trade_id": trade.id})
                values = {
                    "timestamp": mark.timestamp,
                    "short_price": _decimal(mark.short_price),
                    "long_price": _decimal(mark.long_price),
                    "valuation_basis": mark.valuation_basis.value,
                    "exit_debit": _decimal(mark.exit_debit),
                    "pnl_per_unit": _decimal(mark.pnl_per_unit),
                    "pnl_per_lot": _decimal(mark.pnl_per_lot),
                    "spot": _decimal(mark.spot),
                    "result_json": stored_mark.model_dump(mode="json"),
                    "created_at": mark.created_at,
                }
                if existing is None:
                    session.add(ShadowTradeMarkRecord(
                        shadow_trade_id=trade.id,
                        market_snapshot_id=mark.market_snapshot_id,
                        **values,
                    ))
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)

    def load_snapshot_context(self, snapshot_id: int):
        with self._sessions() as session:
            raw = session.scalar(select(MarketSnapshotRecord).options(
                selectinload(MarketSnapshotRecord.options)
            ).where(MarketSnapshotRecord.id == snapshot_id))
            regime = session.scalar(select(MarketRegimeSnapshotRecord).where(
                MarketRegimeSnapshotRecord.market_snapshot_id == snapshot_id,
                MarketRegimeSnapshotRecord.regime_version == self.regime_version,
            ))
            if raw is None:
                return None
            return (
                raw_record_to_model(raw),
                None if regime is None else RegimeResult.model_validate(regime.result_json),
            )

    def open_trades(self) -> list[ShadowTrade]:
        with self._sessions() as session:
            records = session.scalars(select(ShadowTradeRecord).where(
                ShadowTradeRecord.status == "OPEN",
                ShadowTradeRecord.shadow_version == SHADOW_VERSION,
            ).order_by(ShadowTradeRecord.entry_timestamp, ShadowTradeRecord.id)).all()
            return [ShadowTrade.model_validate(item.result_json) for item in records]

    def snapshot_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(session.scalars(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id
            )))

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.get(ShadowTradeRecord, trade_id)
            return None if record is None else record.result_json

    def latest(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(select(ShadowTradeRecord).order_by(
                ShadowTradeRecord.entry_timestamp.desc(), ShadowTradeRecord.id.desc()
            ).limit(1))
            return None if record is None else record.result_json

    def list_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(select(ShadowTradeRecord).order_by(
                ShadowTradeRecord.entry_timestamp.desc(), ShadowTradeRecord.id.desc()
            ).limit(limit)).all()
            return [item.result_json for item in records]

    def marks(self, trade_id: int) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(select(ShadowTradeMarkRecord).where(
                ShadowTradeMarkRecord.shadow_trade_id == trade_id
            ).order_by(ShadowTradeMarkRecord.timestamp, ShadowTradeMarkRecord.id)).all()
            return [item.result_json for item in records]

    def all_trades(self) -> list[ShadowTrade]:
        with self._sessions() as session:
            records = session.scalars(select(ShadowTradeRecord).order_by(
                ShadowTradeRecord.entry_timestamp, ShadowTradeRecord.id
            )).all()
            return [ShadowTrade.model_validate(item.result_json) for item in records]
