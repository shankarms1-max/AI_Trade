"""Research-only Phase 15 market capture; no strategy or paper dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import threading
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger
from app.data.models import MarketSnapshot
from app.research.manifest import canonical, digest
from app.scalper.capture import to_scalper_snapshot
from app.scalper.models import (ScalperMarketSnapshot, ScalperMarketSnapshotRecord,
                                ScalperOptionQuoteRecord)

IST = ZoneInfo("Asia/Kolkata")
MODE = "RESEARCH_ONLY"
ACTS_PAPER_MODE = "ACTS_PAPER"
INTERVAL_SECONDS = 15
logger = get_logger(__name__)


def capture_allowed(now: datetime, start: time, end: time,
                    holidays: frozenset[date]) -> bool:
    """Include the configured forced-exit bucket for causal exit research."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("SCALPER_RESEARCH_AWARE_CLOCK_REQUIRED")
    local = now.astimezone(IST)
    clock = capture_bucket(local).time().replace(tzinfo=None)
    return (local.weekday() < 5 and local.date() not in holidays
            and start <= clock <= end)


def capture_bucket(timestamp: datetime) -> datetime:
    local = timestamp.astimezone(IST)
    second = local.second // INTERVAL_SECONDS * INTERVAL_SECONDS
    return local.replace(second=second, microsecond=0)


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@dataclass(frozen=True)
class ResearchCaptureResult:
    snapshot_id: int
    quote_count: int
    duplicate: bool
    mode: str = MODE


class ResearchSnapshotRepository:
    """Core inserts are restricted to the two Phase 15 evidence tables."""

    def __init__(self, engine: Engine, *, mode: str = MODE) -> None:
        if mode not in {MODE, ACTS_PAPER_MODE}:
            raise ValueError("SCALPER_RESEARCH_CAPTURE_MODE_INVALID")
        self.engine = engine
        self.mode = mode

    def save(self, snapshot: ScalperMarketSnapshot) -> ResearchCaptureResult:
        bucket = capture_bucket(snapshot.captured_at)
        key = digest({"mode": self.mode, "bucket": bucket.isoformat()})
        try:
            with self.engine.begin() as connection:
                existing = connection.scalar(select(ScalperMarketSnapshotRecord.id).where(
                    ScalperMarketSnapshotRecord.capture_key == key))
                if existing is not None:
                    return ResearchCaptureResult(existing, len(snapshot.quotes), True,
                                                 self.mode)
                values = {
                    "capture_key": key,
                    "captured_at": snapshot.captured_at,
                    "request_started_at": snapshot.request_started_at,
                    "response_received_at": snapshot.response_received_at,
                    "source_market_timestamp": snapshot.source_market_timestamp,
                    "nifty_spot": _decimal(snapshot.nifty_spot),
                    "nifty_future": _decimal(snapshot.nifty_future),
                    "future_instrument_id": snapshot.future_instrument_id,
                    "future_expiry": snapshot.future_expiry,
                    "india_vix": _decimal(snapshot.india_vix),
                    "lot_size": snapshot.lot_size,
                    "atm_strike": _decimal(snapshot.atm_strike),
                    "expiry": snapshot.expiry,
                    "source": snapshot.source,
                    # These required columns are intentionally unevaluated.
                    "feature_json": "{}",
                    "signal_json": "{}",
                    "context_json": canonical({"mode": self.mode, "status": "CAPTURED",
                                               "bucket": bucket.isoformat()}),
                }
                inserted = connection.execute(insert(ScalperMarketSnapshotRecord).values(
                    **values))
                primary_key = inserted.inserted_primary_key
                if primary_key is None:
                    raise RuntimeError("SCALPER_RESEARCH_SNAPSHOT_ID_MISSING")
                snapshot_id = primary_key[0]
                connection.execute(insert(ScalperOptionQuoteRecord), [
                    {
                        "scalper_snapshot_id": snapshot_id,
                        "expiry": quote.expiry,
                        "strike": _decimal(quote.strike),
                        "option_type": quote.option_type,
                        "exchange": quote.exchange,
                        "trading_symbol": quote.trading_symbol,
                        "instrument_token": quote.instrument_token,
                        "source_market_timestamp": quote.source_market_timestamp,
                        "bid": _decimal(quote.bid),
                        "ask": _decimal(quote.ask),
                        "bid_quantity": quote.bid_quantity,
                        "ask_quantity": quote.ask_quantity,
                        "depth_unit": quote.depth_unit,
                        "tick_size": _decimal(quote.tick_size),
                        "ltp": _decimal(quote.ltp),
                        "volume": quote.volume,
                        "open_interest": quote.open_interest,
                    } for quote in snapshot.quotes])
                return ResearchCaptureResult(snapshot_id, len(snapshot.quotes), False,
                                             self.mode)
        except IntegrityError:
            # Another research process may have won this 15-second bucket.
            with self.engine.connect() as connection:
                existing = connection.scalar(select(ScalperMarketSnapshotRecord.id).where(
                    ScalperMarketSnapshotRecord.capture_key == key))
            if existing is None:
                raise
            return ResearchCaptureResult(existing, len(snapshot.quotes), True, self.mode)


class ResearchSnapshotCollector:
    """Capture a broker snapshot only; never instantiate the paper service."""

    def __init__(self, repository: ResearchSnapshotRepository,
                 snapshot_builder: Callable[[], MarketSnapshot], *,
                 start: time, end: time,
                 holidays: frozenset[date] = frozenset()) -> None:
        self.repository = repository
        self.snapshot_builder = snapshot_builder
        self.start = start
        self.end = end
        self.holidays = holidays
        self._lock = threading.Lock()

    def capture_once(self, now: datetime | None = None) -> ResearchCaptureResult | None:
        if not capture_allowed(now or datetime.now(IST), self.start,
                               self.end, self.holidays):
            return None
        if not self._lock.acquire(blocking=False):
            logger.warning("SCALPER_RESEARCH_CAPTURE_SKIPPED reason=overlap mode=%s", MODE)
            return None
        try:
            market = self.snapshot_builder()
            if market.required_contracts is not None:
                # Supplement only this research stream with exact held contracts.
                # The live scalper adapter and its execution path stay untouched.
                extra = [quote for quote in market.required_contracts.quotes
                         if quote.expiry == market.expiry]
                market = market.model_copy(update={
                    "options": [*market.options, *extra]})
            snapshot = to_scalper_snapshot(market)
            result = self.repository.save(snapshot)
            logger.info("SCALPER_RESEARCH_CAPTURE status=%s mode=%s snapshot_id=%d "
                        "quotes=%d duplicate=%s", "CAPTURED", result.mode,
                        result.snapshot_id, result.quote_count,
                        str(result.duplicate).lower())
            return result
        except Exception as exc:
            logger.error("SCALPER_RESEARCH_CAPTURE status=FAILED mode=%s error_type=%s",
                         MODE, type(exc).__name__)
            raise
        finally:
            self._lock.release()
