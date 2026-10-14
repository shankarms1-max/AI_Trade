"""Indexed scalar prices and two exact option-volume contracts, never a drifting chain sum."""
from __future__ import annotations

from datetime import datetime
import json

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.data.models import OptionContractSnapshot, OptionType
from app.parallel_paper.bars import MarketPoint, session_start
from app.parallel_paper.config import CAPTURE_MODE
from app.scalper.models import ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.repository import ScalperRepository, _aware


class ParallelMarketRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.snapshots = ScalperRepository(sessions)

    def basket(self, at: datetime) -> list[OptionContractSnapshot]:
        record = ScalperMarketSnapshotRecord
        with self.sessions() as session:
            first_id = session.scalar(select(record.id).where(
                record.captured_at >= session_start(at), record.captured_at <= at,
                record.context_json.like(f'%"mode":"{CAPTURE_MODE}"%')).order_by(
                    record.captured_at, record.id).limit(1))
        first = self.snapshots.get_snapshot(first_id) if first_id is not None else None
        if first is None:
            return []
        result = []
        for side in ("CE", "PE"):
            matches = [quote for quote in first.quotes if quote.strike == first.atm_strike
                       and quote.option_type == side and quote.expiry == first.expiry]
            if len(matches) != 1:
                return []
            quote = matches[0]
            result.append(OptionContractSnapshot(
                expiry=quote.expiry, strike=quote.strike, option_type=OptionType(side),
                exchange=quote.exchange, instrument_token=quote.instrument_token,
                trading_symbol=quote.trading_symbol))
        return result

    def series(self, at: datetime, max_quote_age: int) -> tuple[list[MarketPoint], dict]:
        basket = self.basket(at)
        record, quote = ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
        query = select(record.id, record.captured_at, record.nifty_future,
                       record.future_instrument_id, record.future_expiry, record.context_json).where(
            record.captured_at >= session_start(at), record.captured_at <= at,
            record.context_json.like(f'%"mode":"{CAPTURE_MODE}"%')).order_by(
                record.captured_at, record.id)
        samples: dict[int, list] = {}
        with self.sessions() as session:
            rows = session.execute(query).all()
            if basket:
                identities = [and_(quote.exchange == leg.exchange,
                                   quote.instrument_token == leg.instrument_token,
                                   quote.trading_symbol == leg.trading_symbol,
                                   quote.expiry == leg.expiry, quote.strike == leg.strike,
                                   quote.option_type == leg.option_type.value) for leg in basket]
                quotes = session.execute(select(
                    quote.scalper_snapshot_id, quote.volume, quote.source_market_timestamp, quote.option_type
                ).join(record, record.id == quote.scalper_snapshot_id).where(
                    record.captured_at >= session_start(at), record.captured_at <= at,
                    record.context_json.like(f'%"mode":"{CAPTURE_MODE}"%'),
                    or_(*identities))).all()
                for item in quotes:
                    samples.setdefault(item.scalper_snapshot_id, []).append(item)
        points = []
        for row in rows:
            observed = _aware(row.captured_at)
            matched = sorted(samples.get(row.id, []), key=lambda item: item.option_type)
            reason: str | None = "FIXED_OPTION_BASKET_MISSING"
            volume = None
            counters = None
            if json.loads(row.context_json or "{}").get("volume_capture") != "KOTAK_REST_ALL_LAST_VOLUME":
                reason = "OPTION_VOLUME_PROVENANCE_UNAVAILABLE"
            elif len(matched) == 2:
                if any(item.volume is None or item.volume < 0 for item in matched):
                    reason = "OPTION_VOLUME_UNAVAILABLE"
                elif any(item.source_market_timestamp is None or not (
                        0 <= (observed - _aware(item.source_market_timestamp)).total_seconds()
                        <= max_quote_age) for item in matched):
                    reason = "OPTION_VOLUME_SOURCE_TIMESTAMP_INVALID"
                else:
                    counters = tuple(item.volume for item in matched)
                    volume, reason = sum(counters), None
            points.append(MarketPoint(observed,
                                      float(row.nifty_future) if row.nifty_future is not None else None,
                                      row.future_instrument_id, row.future_expiry, volume, reason, counters))
        return points, {"price_source": "NIFTY_FUTURE_CAPTURED_SAMPLES",
                        "futures_volume_status": "UNAVAILABLE_IN_EXISTING_CAPTURE",
                        "volume_source": "FIXED_SESSION_ATM_CE_PE_CUMULATIVE_DELTA_V1",
                        "volume_capture": "KOTAK_REST_ALL_LAST_VOLUME",
                        "basket": [leg.model_dump(mode="json") for leg in basket],
                        "volume_samples_valid": sum(point.volume is not None for point in points),
                        "volume_samples_missing": sum(point.volume is None for point in points)}
