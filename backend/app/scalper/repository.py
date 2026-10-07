"""Persistence for the isolated fast snapshot stream and its paper journal."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.db.models import MarketRegimeSnapshotRecord, MarketSnapshotRecord
from app.research.manifest import canonical, digest, json_value
from app.scalper.models import (ScalperEvent, ScalperFeatures,
                                ScalperMarketSnapshot, ScalperMarketSnapshotRecord,
                                ScalperOptionQuote, ScalperOptionQuoteRecord,
                                ScalperSignal, ScalperTrade)


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


IST = ZoneInfo("Asia/Kolkata")


def _aware(value):
    return (value.replace(tzinfo=IST)
            if value is not None and value.tzinfo is None else value)


class ScalperRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def save_snapshot(self, snapshot: ScalperMarketSnapshot, features: ScalperFeatures,
                      signal: ScalperSignal,
                      context: dict[str, Any] | None = None) -> tuple[int, bool]:
        capture_key = digest(snapshot)
        try:
            with self.sessions.begin() as session:
                existing = session.scalar(select(ScalperMarketSnapshotRecord.id).where(
                    ScalperMarketSnapshotRecord.capture_key == capture_key))
                if existing is not None:
                    return existing, True
                row = ScalperMarketSnapshotRecord(
                    capture_key=capture_key, captured_at=snapshot.captured_at,
                    request_started_at=snapshot.request_started_at,
                    response_received_at=snapshot.response_received_at,
                    source_market_timestamp=snapshot.source_market_timestamp,
                    nifty_spot=_decimal(snapshot.nifty_spot),
                    nifty_future=_decimal(snapshot.nifty_future),
                    future_instrument_id=snapshot.future_instrument_id,
                    future_expiry=snapshot.future_expiry,
                    india_vix=_decimal(snapshot.india_vix), lot_size=snapshot.lot_size,
                    atm_strike=_decimal(snapshot.atm_strike), expiry=snapshot.expiry,
                    source=snapshot.source, feature_json="{}", signal_json="{}",
                    context_json=canonical(context) if context is not None else None)
                session.add(row)
                session.flush()
                features = features.model_copy(update={"snapshot_id": row.id})
                signal = signal.model_copy(update={"snapshot_id": row.id})
                row.feature_json = canonical(json_value(features))
                row.signal_json = canonical(json_value(signal))
                session.add_all([ScalperOptionQuoteRecord(
                    scalper_snapshot_id=row.id, expiry=item.expiry,
                    strike=_decimal(item.strike), option_type=item.option_type,
                    exchange=item.exchange, trading_symbol=item.trading_symbol,
                    instrument_token=item.instrument_token,
                    source_market_timestamp=item.source_market_timestamp,
                    bid=_decimal(item.bid), ask=_decimal(item.ask),
                    bid_quantity=item.bid_quantity, ask_quantity=item.ask_quantity,
                    depth_unit=item.depth_unit, tick_size=_decimal(item.tick_size),
                    ltp=_decimal(item.ltp), volume=item.volume,
                    open_interest=item.open_interest) for item in snapshot.quotes])
                session.flush()
                return row.id, False
        except IntegrityError:
            with self.sessions() as session:
                existing = session.scalar(select(ScalperMarketSnapshotRecord.id).where(
                    ScalperMarketSnapshotRecord.capture_key == capture_key))
                if existing is not None:
                    return existing, True
            raise

    def existing_snapshot_id(self, snapshot: ScalperMarketSnapshot) -> int | None:
        capture_key = digest(snapshot)
        with self.sessions() as session:
            return session.scalar(select(ScalperMarketSnapshotRecord.id).where(
                ScalperMarketSnapshotRecord.capture_key == capture_key))

    def latest_captured_at(self):
        with self.sessions() as session:
            value = session.scalar(select(ScalperMarketSnapshotRecord.captured_at).order_by(
                ScalperMarketSnapshotRecord.captured_at.desc(),
                ScalperMarketSnapshotRecord.id.desc()).limit(1))
            return _aware(value)

    @staticmethod
    def _row_to_snapshot(row: ScalperMarketSnapshotRecord) -> ScalperMarketSnapshot:
        return ScalperMarketSnapshot(
            captured_at=_aware(row.captured_at),
            request_started_at=_aware(row.request_started_at),
            response_received_at=_aware(row.response_received_at),
            source_market_timestamp=_aware(row.source_market_timestamp),
            nifty_spot=float(row.nifty_spot),
            nifty_future=None if row.nifty_future is None else float(row.nifty_future),
            future_instrument_id=row.future_instrument_id,
            future_expiry=row.future_expiry,
            india_vix=None if row.india_vix is None else float(row.india_vix),
            lot_size=row.lot_size, atm_strike=float(row.atm_strike), expiry=row.expiry,
            source=row.source, quotes=[ScalperOptionQuote(
                expiry=item.expiry, strike=float(item.strike),
                option_type=cast(Literal["CE", "PE"], item.option_type),
                exchange=item.exchange, trading_symbol=item.trading_symbol,
                instrument_token=item.instrument_token,
                source_market_timestamp=_aware(item.source_market_timestamp),
                bid=None if item.bid is None else float(item.bid),
                ask=None if item.ask is None else float(item.ask),
                bid_quantity=item.bid_quantity, ask_quantity=item.ask_quantity,
                depth_unit=cast(Literal["UNKNOWN", "UNITS", "LOTS"], item.depth_unit),
                tick_size=None if item.tick_size is None else float(item.tick_size),
                ltp=None if item.ltp is None else float(item.ltp),
                volume=item.volume, open_interest=item.open_interest)
                for item in sorted(
                    row.quotes,
                    key=lambda value: (value.strike, value.option_type)
                )])

    def get_snapshot(self, snapshot_id: int) -> ScalperMarketSnapshot | None:
        with self.sessions() as session:
            row = session.scalar(select(ScalperMarketSnapshotRecord).options(
                selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                    ScalperMarketSnapshotRecord.id == snapshot_id))
            return None if row is None else self._row_to_snapshot(row)

    def history(self, *, before=None, inclusive=True,
                limit: int = 1000) -> list[ScalperMarketSnapshot]:
        with self.sessions() as session:
            query = select(ScalperMarketSnapshotRecord).options(
                selectinload(ScalperMarketSnapshotRecord.quotes))
            if before is not None:
                comparison = (ScalperMarketSnapshotRecord.captured_at <= before if inclusive
                              else ScalperMarketSnapshotRecord.captured_at < before)
                query = query.where(comparison)
            rows = session.scalars(query.order_by(
                ScalperMarketSnapshotRecord.captured_at.desc(),
                ScalperMarketSnapshotRecord.id.desc()).limit(limit)).all()
            return [self._row_to_snapshot(row) for row in reversed(rows)]

    def signal_history(self, *, before, limit: int = 100) -> list[ScalperSignal]:
        with self.sessions() as session:
            rows = session.scalars(select(ScalperMarketSnapshotRecord).where(
                ScalperMarketSnapshotRecord.captured_at < before).order_by(
                    ScalperMarketSnapshotRecord.captured_at.desc()).limit(limit)).all()
            return [ScalperSignal.model_validate(json.loads(row.signal_json))
                    for row in reversed(rows)]

    def phase14_context_before(self, timestamp) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.scalar(select(MarketRegimeSnapshotRecord).join(
                MarketSnapshotRecord,
                MarketSnapshotRecord.id == MarketRegimeSnapshotRecord.market_snapshot_id
            ).where(MarketSnapshotRecord.timestamp_ist < timestamp).order_by(
                MarketSnapshotRecord.timestamp_ist.desc(),
                MarketRegimeSnapshotRecord.id.desc()).limit(1))
            if row is None:
                return None
            return {"snapshot_id": row.market_snapshot_id, "regime": row.regime,
                    "confidence": row.confidence, "evidence": row.result_json,
                    "usage": "OPTIONAL_CONTEXT_ONLY"}

    def dashboard(self, trading_date: date) -> dict[str, Any]:
        with self.sessions() as check:
            ready = inspect(check.connection()).has_table("scalper_market_snapshots")
        if not ready:
            return {"enabled": False, "mode": "PAPER", "schema_ready": False}
        with self.sessions() as session:
            latest = session.scalar(select(ScalperMarketSnapshotRecord).order_by(
                ScalperMarketSnapshotRecord.captured_at.desc(),
                ScalperMarketSnapshotRecord.id.desc()).limit(1))
            trades = session.scalars(select(ScalperTrade)).all()
            documents = [(row.state, json.loads(row.document)) for row in trades]
            today = [(state, doc) for state, doc in documents
                     if doc.get("decision_timestamp", "")[:10] == str(trading_date)]
            closed = [doc for state, doc in today if state == "CLOSED"]
            open_rows = [doc | {"state": state} for state, doc in documents
                         if state in {"OPEN", "UNRESOLVED"}]
            pending = [doc | {"state": state} for state, doc in documents
                       if state == "PENDING_ENTRY"]
            events = session.scalars(select(ScalperEvent).order_by(
                ScalperEvent.sequence.desc()).limit(60)).all()
            gross = sum((item.get("final_outcome") or {}).get("gross_rupees", 0)
                        for item in closed)
            wins = sum((item.get("final_outcome") or {}).get("gross_rupees", 0) > 0
                       for item in closed)
            return {
                "schema_ready": True,
                "latest_snapshot": None if latest is None else {
                    "id": latest.id, "timestamp": _aware(latest.captured_at).isoformat(),
                    "spot": float(latest.nifty_spot),
                    "signal": json.loads(latest.signal_json),
                    "features": json.loads(latest.feature_json)},
                "current_position": open_rows[-1] if open_rows else
                pending[-1] if pending else None,
                "today": {"trades": sum("entry_timestamp" in doc for _, doc in today),
                          "wins": wins, "losses": len(closed) - wins,
                          "gross_pnl": gross,
                          "rejections": sum(state == "ENTRY_REJECTED" for state, _ in today),
                          "cost_completeness": "GROSS_ONLY",
                          "net_pnl": None, "profitability_claim": False},
                "timeline": [json.loads(item.payload) for item in reversed(events)],
            }


def scalper_schema_ready(sessions) -> bool:
    with sessions() as session:
        return inspect(session.connection()).has_table("scalper_market_snapshots")


def scalper_summary(sessions, settings, trading_date: date) -> dict[str, Any]:
    result = ScalperRepository(sessions).dashboard(trading_date)
    result.update(enabled=settings.scalper_enabled, mode="PAPER",
                  interval_seconds=settings.scalper_interval_seconds,
                  kill_switch=settings.scalper_kill_switch,
                  score_threshold=settings.scalper_signal_min_score,
                  minimum_confirmations=settings.scalper_min_confirmations)
    return result
