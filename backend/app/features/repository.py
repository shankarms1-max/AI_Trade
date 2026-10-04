from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.db.models import MarketFeatureSnapshotRecord, MarketSnapshotRecord
from app.features.models import FEATURE_VERSION, MarketFeatureSnapshot

IST = ZoneInfo("Asia/Kolkata")


def _float(value) -> float | None:
    return None if value is None else float(value)


def raw_record_to_model(record: MarketSnapshotRecord) -> MarketSnapshot:
    timestamp = record.timestamp_ist
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)
    return MarketSnapshot(
        timestamp_ist=timestamp,
        nifty_spot=float(record.nifty_spot),
        nifty_future=_float(record.nifty_future),
        future_instrument_id=record.future_instrument_id,
        future_expiry=record.future_expiry,
        source_market_timestamp=record.source_market_timestamp,
        request_started_at=record.request_started_at,
        response_received_at=record.response_received_at,
        snapshot_persisted_at=record.created_at,
        india_vix=_float(record.india_vix),
        lot_size=record.lot_size,
        atm_strike=float(record.atm_strike),
        expiry=record.expiry,
        source=record.source,
        options=[
            OptionContractSnapshot(
                strike=float(item.strike),
                option_type=item.option_type,
                expiry=item.expiry,
                trading_symbol=item.trading_symbol,
                instrument_token=item.instrument_token,
                exchange=item.exchange,
                source_market_timestamp=item.source_market_timestamp,
                bid_quantity=item.bid_quantity,
                ask_quantity=item.ask_quantity,
                ltp=_float(item.ltp),
                open_interest=item.open_interest,
                previous_open_interest=item.previous_open_interest,
                change_in_open_interest=item.change_in_open_interest,
                volume=item.volume,
                implied_volatility=_float(item.implied_volatility),
                bid=_float(item.bid),
                ask=_float(item.ask),
                delta=_float(item.delta),
                gamma=_float(item.gamma),
                theta=_float(item.theta),
                vega=_float(item.vega),
            )
            for item in record.options
        ],
    )


class FeatureRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    @staticmethod
    def _raw_query():
        return select(MarketSnapshotRecord).options(selectinload(MarketSnapshotRecord.options))

    def load_raw(self, snapshot_id: int) -> MarketSnapshot | None:
        with self._sessions() as session:
            record = session.scalar(self._raw_query().where(MarketSnapshotRecord.id == snapshot_id))
            return None if record is None else raw_record_to_model(record)

    def latest_raw_id(self) -> int | None:
        with self._sessions() as session:
            return session.scalar(
                select(MarketSnapshotRecord.id)
                .order_by(MarketSnapshotRecord.timestamp_ist.desc(), MarketSnapshotRecord.id.desc())
                .limit(1)
            )

    def raw_ids_chronological(self) -> list[int]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(MarketSnapshotRecord.id).order_by(
                        MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id
                    )
                )
            )

    def load_history_before(self, timestamp: datetime) -> list[MarketSnapshot]:
        with self._sessions() as session:
            records = session.scalars(
                self._raw_query()
                .where(MarketSnapshotRecord.timestamp_ist < timestamp)
                .order_by(MarketSnapshotRecord.timestamp_ist)
            ).all()
            return [raw_record_to_model(record) for record in records]

    def upsert(self, feature: MarketFeatureSnapshot) -> int:
        payload = feature.model_dump(mode="json")
        supports = feature.support_resistance.potential_support_clusters
        resistances = feature.support_resistance.potential_resistance_clusters
        values = {
            "timestamp": feature.timestamp,
            "expiry": feature.expiry,
            "local_pcr_oi": _decimal(feature.pcr_features.local_pcr_oi),
            "local_pcr_oi_change": _decimal(feature.pcr_features.local_pcr_oi_change),
            "futures_basis": _decimal(feature.futures_features.futures_basis),
            "india_vix": _decimal(feature.volatility_features.india_vix),
            "vix_regime": feature.volatility_features.vix_regime,
            "nearest_support": _decimal(
                min(supports, key=lambda item: abs(item.center_strike - feature.spot)).center_strike
                if supports else None
            ),
            "nearest_resistance": _decimal(
                min(resistances, key=lambda item: abs(item.center_strike - feature.spot)).center_strike
                if resistances else None
            ),
            "feature_json": payload,
        }
        with self._sessions.begin() as session:
            record = session.scalar(
                select(MarketFeatureSnapshotRecord).where(
                    MarketFeatureSnapshotRecord.market_snapshot_id == feature.snapshot_id,
                    MarketFeatureSnapshotRecord.feature_version == feature.feature_version,
                )
            )
            if record is None:
                record = MarketFeatureSnapshotRecord(
                    market_snapshot_id=feature.snapshot_id,
                    feature_version=feature.feature_version,
                    **values,
                )
                session.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            session.flush()
            return record.id

    def get(self, snapshot_id: int, version: str = FEATURE_VERSION) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(MarketFeatureSnapshotRecord).where(
                    MarketFeatureSnapshotRecord.market_snapshot_id == snapshot_id,
                    MarketFeatureSnapshotRecord.feature_version == version,
                )
            )
            return None if record is None else _feature_dict(record)

    def latest(self, version: str = FEATURE_VERSION) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                select(MarketFeatureSnapshotRecord)
                .where(MarketFeatureSnapshotRecord.feature_version == version)
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketFeatureSnapshotRecord.id.desc())
                .limit(1)
            )
            return None if record is None else _feature_dict(record)

    def list(self, limit: int, version: str = FEATURE_VERSION) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(MarketFeatureSnapshotRecord)
                .where(MarketFeatureSnapshotRecord.feature_version == version)
                .order_by(MarketFeatureSnapshotRecord.timestamp.desc(), MarketFeatureSnapshotRecord.id.desc())
                .limit(limit)
            ).all()
            return [_feature_dict(record, full=False) for record in records]


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _feature_dict(record: MarketFeatureSnapshotRecord, full: bool = True) -> dict[str, Any]:
    if full:
        return record.feature_json
    return {
        "id": record.id,
        "snapshot_id": record.market_snapshot_id,
        "feature_version": record.feature_version,
        "timestamp": record.timestamp,
        "expiry": record.expiry,
        "local_pcr_oi": record.local_pcr_oi,
        "local_pcr_oi_change": record.local_pcr_oi_change,
        "futures_basis": record.futures_basis,
        "india_vix": record.india_vix,
        "vix_regime": record.vix_regime,
        "nearest_support": record.nearest_support,
        "nearest_resistance": record.nearest_resistance,
    }
