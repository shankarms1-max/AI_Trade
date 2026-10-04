from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.data.models import MarketSnapshot
from app.db.models import (
    CollectorRunRecord,
    MarketSnapshotRecord,
    OptionContractSnapshotRecord,
)

IST = ZoneInfo("Asia/Kolkata")


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class SaveResult:
    snapshot_id: int
    contracts: int
    duplicate: bool


class SnapshotRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create_collector_run(self, started_at: datetime) -> int:
        with self._sessions.begin() as session:
            run = CollectorRunRecord(
                started_at=started_at,
                status="STARTED",
                contracts_received=0,
            )
            session.add(run)
            session.flush()
            return run.id

    def mark_collector_failed(self, run_id: int, error: BaseException) -> None:
        with self._sessions.begin() as session:
            run = session.get(CollectorRunRecord, run_id)
            if run is None or run.status == "SUCCESS":
                return
            run.status = "FAILED"
            run.completed_at = datetime.now(IST)
            run.error_type = type(error).__name__[:120]
            # Exception strings can contain broker payloads or credentials.
            run.safe_error_message = "Market-data collection or persistence failed"

    def _mark_success(
        self,
        session: Session,
        run_id: int,
        snapshot_id: int,
        contracts: int,
    ) -> None:
        run = session.get(CollectorRunRecord, run_id)
        if run is None:
            raise LookupError(f"collector run {run_id} does not exist")
        run.status = "SUCCESS"
        run.completed_at = datetime.now(IST)
        run.contracts_received = contracts
        run.snapshot_id = snapshot_id
        run.error_type = None
        run.safe_error_message = None

    def save_market_snapshot(
        self,
        snapshot: MarketSnapshot,
        collection_bucket_ist: datetime,
        run_id: int | None = None,
    ) -> SaveResult:
        run_id = run_id or self.create_collector_run(snapshot.timestamp_ist)
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(MarketSnapshotRecord).where(
                        MarketSnapshotRecord.collection_bucket_ist
                        == collection_bucket_ist
                    )
                )
                if existing is not None:
                    count = session.scalar(
                        select(func.count(OptionContractSnapshotRecord.id)).where(
                            OptionContractSnapshotRecord.market_snapshot_id == existing.id
                        )
                    ) or 0
                    self._mark_success(session, run_id, existing.id, count)
                    return SaveResult(existing.id, count, True)

                record = MarketSnapshotRecord(
                    timestamp_ist=snapshot.timestamp_ist,
                    collection_bucket_ist=collection_bucket_ist,
                    nifty_spot=_decimal(snapshot.nifty_spot),
                    nifty_future=_decimal(snapshot.nifty_future),
                    future_instrument_id=snapshot.future_instrument_id,
                    future_expiry=snapshot.future_expiry,
                    source_market_timestamp=snapshot.source_market_timestamp,
                    request_started_at=snapshot.request_started_at,
                    response_received_at=snapshot.response_received_at,
                    india_vix=_decimal(snapshot.india_vix),
                    lot_size=snapshot.lot_size,
                    atm_strike=_decimal(snapshot.atm_strike),
                    expiry=snapshot.expiry,
                    source=snapshot.source,
                )
                session.add(record)
                session.flush()
                for contract in snapshot.options:
                    session.add(
                        OptionContractSnapshotRecord(
                            market_snapshot_id=record.id,
                            strike=_decimal(contract.strike),
                            option_type=contract.option_type.value,
                            expiry=contract.expiry,
                            trading_symbol=contract.trading_symbol,
                            instrument_token=contract.instrument_token,
                            exchange=contract.exchange,
                            source_market_timestamp=contract.source_market_timestamp,
                            bid_quantity=contract.bid_quantity,
                            ask_quantity=contract.ask_quantity,
                            ltp=_decimal(contract.ltp),
                            open_interest=contract.open_interest,
                            previous_open_interest=contract.previous_open_interest,
                            change_in_open_interest=contract.change_in_open_interest,
                            volume=contract.volume,
                            implied_volatility=_decimal(contract.implied_volatility),
                            bid=_decimal(contract.bid),
                            ask=_decimal(contract.ask),
                            delta=_decimal(contract.delta),
                            gamma=_decimal(contract.gamma),
                            theta=_decimal(contract.theta),
                            vega=_decimal(contract.vega),
                        )
                    )
                session.flush()
                self._mark_success(session, run_id, record.id, len(snapshot.options))
                return SaveResult(record.id, len(snapshot.options), False)
        except IntegrityError as exc:
            # A racing invocation may have committed the same bucket first.
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(MarketSnapshotRecord).where(
                        MarketSnapshotRecord.collection_bucket_ist
                        == collection_bucket_ist
                    )
                )
                if existing is not None:
                    count = session.scalar(
                        select(func.count(OptionContractSnapshotRecord.id)).where(
                            OptionContractSnapshotRecord.market_snapshot_id == existing.id
                        )
                    ) or 0
                    self._mark_success(session, run_id, existing.id, count)
                    return SaveResult(existing.id, count, True)
            self.mark_collector_failed(run_id, exc)
            raise
        except Exception as exc:
            self.mark_collector_failed(run_id, exc)
            raise

    @staticmethod
    def _query_with_options() -> Select[tuple[MarketSnapshotRecord]]:
        return select(MarketSnapshotRecord).options(
            selectinload(MarketSnapshotRecord.options)
        )

    def latest(self) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                self._query_with_options().order_by(
                    MarketSnapshotRecord.timestamp_ist.desc(),
                    MarketSnapshotRecord.id.desc(),
                ).limit(1)
            )
            return None if record is None else snapshot_to_dict(record, include_options=True)

    def list(self, limit: int) -> list[dict[str, Any]]:
        with self._sessions() as session:
            records = session.scalars(
                select(MarketSnapshotRecord)
                .order_by(
                    MarketSnapshotRecord.timestamp_ist.desc(),
                    MarketSnapshotRecord.id.desc(),
                )
                .limit(limit)
            ).all()
            return [snapshot_to_dict(record, include_options=False) for record in records]

    def get(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            record = session.scalar(
                self._query_with_options().where(MarketSnapshotRecord.id == snapshot_id)
            )
            return None if record is None else snapshot_to_dict(record, include_options=True)


def snapshot_to_dict(
    record: MarketSnapshotRecord, *, include_options: bool
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": record.id,
        "timestamp_ist": record.timestamp_ist,
        "nifty_spot": record.nifty_spot,
        "nifty_future": record.nifty_future,
        "future_instrument_id": record.future_instrument_id,
        "future_expiry": record.future_expiry,
        "source_market_timestamp": record.source_market_timestamp,
        "request_started_at": record.request_started_at,
        "response_received_at": record.response_received_at,
        "india_vix": record.india_vix,
        "lot_size": record.lot_size,
        "atm_strike": record.atm_strike,
        "expiry": record.expiry,
        "source": record.source,
        "created_at": record.created_at,
        "snapshot_persisted_at": record.created_at,
    }
    if include_options:
        result["options"] = [
            {
                "id": item.id,
                "strike": item.strike,
                "option_type": item.option_type,
                "expiry": item.expiry,
                "trading_symbol": item.trading_symbol,
                "instrument_token": item.instrument_token,
                "exchange": item.exchange,
                "source_market_timestamp": item.source_market_timestamp,
                "bid_quantity": item.bid_quantity,
                "ask_quantity": item.ask_quantity,
                "ltp": item.ltp,
                "open_interest": item.open_interest,
                "previous_open_interest": item.previous_open_interest,
                "change_in_open_interest": item.change_in_open_interest,
                "volume": item.volume,
                "implied_volatility": item.implied_volatility,
                "bid": item.bid,
                "ask": item.ask,
                "delta": item.delta,
                "gamma": item.gamma,
                "theta": item.theta,
                "vega": item.vega,
                "created_at": item.created_at,
            }
            for item in sorted(record.options, key=lambda value: (value.strike, value.option_type))
        ]
    return result
