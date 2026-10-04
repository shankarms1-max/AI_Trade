from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot, CalculationMode
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.features.repository import raw_record_to_model


def _decimal(value: float | None):
    return None if value is None else Decimal(str(value))


class AlphaRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _raw_query():
        return select(MarketSnapshotRecord).options(selectinload(MarketSnapshotRecord.options))

    def load_raw(self, snapshot_id: int):
        with self._sessions() as session:
            record = session.scalar(self._raw_query().where(MarketSnapshotRecord.id == snapshot_id))
            return None if record is None else raw_record_to_model(record)

    def load_history_before(self, timestamp):
        with self._sessions() as session:
            records = session.scalars(
                self._raw_query().where(MarketSnapshotRecord.timestamp_ist < timestamp)
                .order_by(MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id)
            ).all()
            return [raw_record_to_model(item) for item in records]

    def load_alpha_before(self, timestamp, version: str = ALPHA_VERSION,
                          mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY):
        with self._sessions() as session:
            records = session.scalars(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.alpha_version == version,
                AlphaFeatureSnapshotRecord.calculation_mode == mode.value,
                AlphaFeatureSnapshotRecord.timestamp < timestamp,
            ).order_by(AlphaFeatureSnapshotRecord.timestamp, AlphaFeatureSnapshotRecord.id)).all()
            return [AlphaFeatureSnapshot.model_validate(item.result_json) for item in records]

    def raw_ids_chronological(self):
        with self._sessions() as session:
            return list(session.scalars(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id
            )))

    def latest_raw_id(self):
        with self._sessions() as session:
            return session.scalar(select(MarketSnapshotRecord.id).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(), MarketSnapshotRecord.id.desc()
            ).limit(1))

    def upsert(self, alpha: AlphaFeatureSnapshot) -> int:
        payload = alpha.model_dump(mode="json")
        values = {
            "calculation_mode": alpha.calculation_mode.value,
            "timestamp": alpha.timestamp, "expiry": alpha.expiry,
            "session_id": alpha.session_id, "session_date": alpha.session_date,
            "lookback_clock_mode": alpha.lookback_clock_mode,
            "actual_horizon_seconds": alpha.actual_horizon_seconds,
            "signed_log_return": _decimal(alpha.signed_log_return),
            "hypothesis_type": alpha.hypothesis_type.value,
            "validity_state": alpha.validity_state.value,
            "participation_state": alpha.participation_state.value,
            "underlying_horizon_volatility": _decimal(alpha.underlying_horizon_volatility),
            "confirmation_reset_reason": alpha.confirmation_reset_reason,
            "source_market_timestamp": alpha.source_market_timestamp,
            "response_received_at": alpha.response_received_at,
            "feature_calculated_at": alpha.feature_calculated_at,
            "price_source": alpha.price_source, "price_return": _decimal(alpha.price_return),
            "alpha_1": _decimal(alpha.alpha_1), "atm_strike": _decimal(alpha.atm_strike),
            "atm_ce_token": alpha.atm_ce_token, "atm_pe_token": alpha.atm_pe_token,
            "atm_ce_interval_volume": alpha.atm_ce_interval_volume,
            "atm_pe_interval_volume": alpha.atm_pe_interval_volume,
            "ce_volume_ratio": _decimal(alpha.ce_volume_ratio),
            "pe_volume_ratio": _decimal(alpha.pe_volume_ratio),
            "atm_volume_activity": _decimal(alpha.atm_volume_activity),
            "ce_observed_volatility": _decimal(alpha.ce_observed_volatility),
            "pe_observed_volatility": _decimal(alpha.pe_observed_volatility),
            "atm_option_volatility": _decimal(alpha.atm_option_volatility),
            "directional_impulse_raw": _decimal(alpha.directional_impulse_raw),
            "alpha_2": _decimal(alpha.alpha_2),
            "alpha_1_direction": alpha.alpha_1_direction.value,
            "alpha_2_direction": alpha.alpha_2_direction.value,
            "joint_alpha_direction": alpha.joint_alpha_direction.value,
            "consecutive_confirmation_count": alpha.consecutive_confirmation_count,
            "evidence_quality": alpha.evidence_quality.value,
            "warnings_json": alpha.warnings, "result_json": payload,
        }
        with self._sessions.begin() as session:
            record = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == alpha.snapshot_id,
                AlphaFeatureSnapshotRecord.alpha_version == alpha.alpha_version,
                AlphaFeatureSnapshotRecord.calculation_mode == alpha.calculation_mode.value,
            ))
            if record is None:
                record = AlphaFeatureSnapshotRecord(
                    market_snapshot_id=alpha.snapshot_id,
                    alpha_version=alpha.alpha_version,
                    **values,
                )
                session.add(record)
            else:
                if alpha.calculation_mode == CalculationMode.RESEARCH_RECOMPUTE:
                    for key, value in values.items():
                        setattr(record, key, value)
            session.flush()
            return record.id

    @staticmethod
    def _payload(record):
        return record.result_json

    def get(self, snapshot_id: int, version: str = ALPHA_VERSION):
        with self._sessions() as session:
            record = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                AlphaFeatureSnapshotRecord.alpha_version == version,
                AlphaFeatureSnapshotRecord.calculation_mode != CalculationMode.RESEARCH_RECOMPUTE.value,
            ).order_by(case((AlphaFeatureSnapshotRecord.calculation_mode == "LIVE_ORIGINAL", 0), else_=1)))
            return None if record is None else self._payload(record)

    def latest(self, version: str = ALPHA_VERSION):
        with self._sessions() as session:
            record = session.scalar(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.alpha_version == version,
                AlphaFeatureSnapshotRecord.calculation_mode != CalculationMode.RESEARCH_RECOMPUTE.value,
            ).order_by(AlphaFeatureSnapshotRecord.timestamp.desc(),
                       case((AlphaFeatureSnapshotRecord.calculation_mode == "LIVE_ORIGINAL", 0), else_=1),
                       AlphaFeatureSnapshotRecord.id.desc()).limit(1))
            return None if record is None else self._payload(record)

    def list(self, limit: int, version: str = ALPHA_VERSION):
        with self._sessions() as session:
            records = session.scalars(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.alpha_version == version,
                AlphaFeatureSnapshotRecord.calculation_mode != CalculationMode.RESEARCH_RECOMPUTE.value,
            ).order_by(AlphaFeatureSnapshotRecord.timestamp.desc(), AlphaFeatureSnapshotRecord.id.desc()).limit(limit)).all()
            return [self._payload(item) for item in records]

    def history(self, trading_date: date, version: str = ALPHA_VERSION):
        with self._sessions() as session:
            records = session.scalars(select(AlphaFeatureSnapshotRecord).where(
                AlphaFeatureSnapshotRecord.alpha_version == version,
                AlphaFeatureSnapshotRecord.calculation_mode != CalculationMode.RESEARCH_RECOMPUTE.value,
                func.date(AlphaFeatureSnapshotRecord.timestamp) == trading_date,
            ).order_by(AlphaFeatureSnapshotRecord.timestamp, AlphaFeatureSnapshotRecord.id)).all()
            return [self._payload(item) for item in records]
