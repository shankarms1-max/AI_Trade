"""Phase 15 local capture adapter for the unchanged Phase 14.1 Alpha service."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from math import ceil
from typing import Any, Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.alpha.clock import window_coverage, LookbackClockMode
from app.alpha.engine import AlphaConfig, config_from_settings
from app.alpha.models import ALPHA_VERSION, AlphaFeatureSnapshot, CalculationMode
from app.alpha.repository import AlphaRepository
from app.alpha.service import build_and_store_alpha
from app.core.logging import get_logger
from app.data.models import MarketSnapshot, OptionContractSnapshot, OptionType
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.features.repository import raw_record_to_model
from app.scalper.repository import IST
from app.scalper.models import ScalperMarketSnapshotRecord
from app.scalper.research_capture import ACTS_PAPER_MODE, MODE, capture_bucket
from app.research.manifest import digest

ACTS_ALPHA_SOURCE = "KOTAK_NEO_ACTS_PAPER"
ALPHA_CAPTURE_MINUTES = 3
LEGACY_RESEARCH_MODE = "LEGACY_RESEARCH"
PROVENANCE = ("ACTS_PAPER", "RESEARCH_ONLY", "LEGACY_RESEARCH", "LOCAL_ALPHA")
HistoryRecord = ScalperMarketSnapshotRecord | MarketSnapshotRecord
logger = get_logger(__name__)


def alpha_bucket(at: datetime) -> datetime:
    """Stable 3-minute key distinct from the existing collector's bucket."""
    local = at.astimezone(IST)
    return local.replace(minute=local.minute // ALPHA_CAPTURE_MINUTES
                         * ALPHA_CAPTURE_MINUTES, second=0, microsecond=1)


def _aware(value: datetime | None, *, utc: bool = False) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc if utc else IST)
    return value.astimezone(IST)


def _as_ist(value: datetime) -> datetime:
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def _research_provenance(record: ScalperMarketSnapshotRecord) -> str | None:
    """Accept only captures with a verifiable, read-only capture-mode key."""
    try:
        context = json.loads(record.context_json or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(context, dict) or context.get("status") != "CAPTURED":
        return None
    mode = context.get("mode")
    if mode not in {ACTS_PAPER_MODE, MODE, LEGACY_RESEARCH_MODE}:
        return None
    captured = _as_ist(record.captured_at)
    expected = digest({"mode": mode, "bucket": capture_bucket(captured).isoformat()})
    if record.capture_key != expected:
        return None
    if mode == LEGACY_RESEARCH_MODE and context.get("origin") != "KOTAK_NEO_READ_ONLY":
        return None
    return mode


def is_alpha_warmup_eligible(record: ScalperMarketSnapshotRecord) -> str | None:
    """Validate capture provenance and the fields needed by original Alpha."""
    provenance = _research_provenance(record)
    captured = _as_ist(record.captured_at)
    request = _aware(record.request_started_at)
    received = _aware(record.response_received_at)
    source = _aware(record.source_market_timestamp)
    if (provenance is None or record.source != "KOTAK_NEO"
            or request is None or received is None
            or request > received or received > captured
            or source is not None and source > captured
            or record.nifty_spot is None or record.nifty_spot <= 0
            or record.nifty_future is None or record.nifty_future <= 0
            or not record.future_instrument_id or record.future_expiry is None
            or record.atm_strike is None or record.atm_strike <= 0
            or record.india_vix is not None and record.india_vix < 0
            or record.lot_size is None or record.lot_size <= 0):
        return None
    quotes = record.quotes
    if not quotes:
        return None
    identities = [(q.exchange, q.instrument_token, q.expiry, q.strike,
                   q.option_type, q.trading_symbol) for q in quotes]
    if len(identities) != len(set(identities)):
        return None
    pairs: dict[Any, set[str]] = {}
    for quote in quotes:
        quote_source = _aware(quote.source_market_timestamp)
        if (quote.expiry != record.expiry or quote.option_type not in {"CE", "PE"}
                or not quote.exchange or not quote.instrument_token
                or not quote.trading_symbol or quote.strike is None or quote.strike <= 0
                or quote.depth_unit not in {"UNKNOWN", "UNITS", "LOTS"}
                or quote_source is not None and quote_source > captured):
            return None
        complete = (quote.ltp is not None and quote.ltp > 0
                    and quote.volume is not None and quote.volume >= 0
                    and quote.open_interest is not None and quote.open_interest >= 0)
        if provenance == LEGACY_RESEARCH_MODE and (not complete or quote_source is None):
            return None
        if complete:
            pairs.setdefault(quote.strike, set()).add(quote.option_type)
    return provenance if any(sides == {"CE", "PE"} for sides in pairs.values()) else None


def research_to_market_snapshot(record: ScalperMarketSnapshotRecord) -> MarketSnapshot:
    """Convert immutable Phase 15 capture fields without synthesizing Alpha inputs."""
    return MarketSnapshot(
        timestamp_ist=_as_ist(record.captured_at),
        request_started_at=_aware(record.request_started_at),
        response_received_at=_aware(record.response_received_at),
        # Phase 15's aggregate timestamp is the latest OPTION quote timestamp
        # (to_scalper_snapshot), not the futures LTP's market timestamp. Passing
        # it as the futures reference age makes Phase 14.1 reject valid returns.
        # The broker supplies no futures source timestamp; leave it unavailable.
        # Each option keeps its own exact source timestamp below.
        source_market_timestamp=None,
        snapshot_persisted_at=_aware(record.created_at, utc=True),
        nifty_spot=float(record.nifty_spot),
        nifty_future=None if record.nifty_future is None else float(record.nifty_future),
        future_instrument_id=record.future_instrument_id,
        future_expiry=record.future_expiry,
        india_vix=None if record.india_vix is None else float(record.india_vix),
        lot_size=record.lot_size, atm_strike=float(record.atm_strike),
        expiry=record.expiry, source=record.source,
        options=[OptionContractSnapshot(
            expiry=q.expiry, strike=float(q.strike), option_type=OptionType(q.option_type),
            exchange=q.exchange, trading_symbol=q.trading_symbol,
            instrument_token=q.instrument_token,
            source_market_timestamp=_aware(q.source_market_timestamp),
            bid=None if q.bid is None else float(q.bid),
            ask=None if q.ask is None else float(q.ask),
            bid_quantity=q.bid_quantity, ask_quantity=q.ask_quantity,
            depth_unit=cast(Literal["UNKNOWN", "UNITS", "LOTS"], q.depth_unit),
            tick_size=None if q.tick_size is None else float(q.tick_size),
            ltp=None if q.ltp is None else float(q.ltp),
            volume=q.volume, open_interest=q.open_interest,
            previous_open_interest=None, change_in_open_interest=None,
            implied_volatility=None, delta=None, gamma=None, theta=None, vega=None)
            for q in sorted(record.quotes, key=lambda item: (
                item.strike, item.option_type, item.instrument_token))],
    )


class ActsAlphaRepository(AlphaRepository):
    """Bounded causal research reads; inherited LIVE_ORIGINAL upsert stays intact."""

    def __init__(self, sessions: sessionmaker[Session], config: AlphaConfig) -> None:
        super().__init__(sessions)
        self.sessions = sessions
        dependency = max(config.alpha1_lookback_minutes,
                         config.alpha2_lookback_minutes + config.volatility_lookback_minutes,
                         config.alpha2_lookback_minutes + config.volume_lookback_minutes)
        required_minutes = dependency + config.max_horizon_seconds / 60 + 2
        self.history_limit = max(500, ceil(required_minutes / ALPHA_CAPTURE_MINUTES) + 128)
        # Up to three overlapping 15-second research streams; only scalar rows
        # are scanned. Quote rows are loaded for selected 3-minute buckets.
        self.research_scan_limit = self.history_limit * 12 * 3 + 256
        self.config = config
        self.last_history_report: dict[str, Any] = {}
        # Retain provenance for read-only before/after diagnostics. These option
        # timestamps must never become futures reference timestamps in Alpha.
        self.last_history_option_aggregate_times: dict[datetime, datetime | None] = {}

    def _capture_candidates(self, session: Session, timestamp: datetime
                            ) -> dict[datetime, dict[str, HistoryRecord]]:
        record = ScalperMarketSnapshotRecord
        modes = (ACTS_PAPER_MODE, MODE, LEGACY_RESEARCH_MODE)
        rows = session.scalars(select(record).where(
            record.captured_at < timestamp, record.source == "KOTAK_NEO",
            or_(*(record.context_json.like(f'%"mode":"{mode}"%')
                  for mode in modes))).order_by(
                      record.captured_at.desc(), record.id.desc()
                  ).limit(self.research_scan_limit)).all()
        current_bucket = alpha_bucket(timestamp)
        choices: dict[datetime, dict[str, HistoryRecord]] = {}
        for row in rows:
            mode = _research_provenance(row)
            bucket = alpha_bucket(_as_ist(row.captured_at))
            persisted = _aware(row.created_at, utc=True)
            if (mode is not None and bucket < current_bucket
                    and persisted is not None and persisted <= timestamp):
                # Descending scan means replacement leaves the first capture
                # in that mode/bucket. This rule is stable across restarts.
                choices.setdefault(bucket, {})[mode] = row
        return choices

    @staticmethod
    def _load_research_rows(session: Session, ids: list[int]
                            ) -> dict[int, ScalperMarketSnapshotRecord]:
        if not ids:
            return {}
        record = ScalperMarketSnapshotRecord
        rows = session.scalars(select(record).options(
            selectinload(record.quotes)).where(record.id.in_(ids))).all()
        return {row.id: row for row in rows}

    def load_history_before(self, timestamp: datetime) -> list[MarketSnapshot]:
        with self.sessions() as session:
            local_rows = session.scalars(select(MarketSnapshotRecord).where(
                    MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE,
                    MarketSnapshotRecord.timestamp_ist < timestamp).order_by(
                        MarketSnapshotRecord.timestamp_ist.desc(),
                        MarketSnapshotRecord.id.desc()).limit(self.history_limit)).all()
            current_bucket = alpha_bucket(timestamp)
            choices = self._capture_candidates(session, timestamp)
            for row in local_rows:
                bucket = alpha_bucket(_as_ist(row.timestamp_ist))
                persisted = _aware(row.created_at, utc=True)
                if (bucket < current_bucket and persisted is not None
                        and persisted <= timestamp):
                    choices.setdefault(bucket, {})["LOCAL_ALPHA"] = row
            buckets = sorted(choices, reverse=True)[:self.history_limit]
            chosen: dict[datetime, tuple[str, HistoryRecord]] = {}
            unresolved = set(buckets)
            for mode in ("LOCAL_ALPHA", ACTS_PAPER_MODE, MODE,
                         LEGACY_RESEARCH_MODE):
                candidates = {bucket: choices[bucket][mode] for bucket in unresolved
                              if mode in choices[bucket]}
                if not candidates:
                    continue
                if mode == "LOCAL_ALPHA":
                    record = MarketSnapshotRecord
                    local_ids = [item.id for item in candidates.values()
                                 if isinstance(item, MarketSnapshotRecord)]
                    local_found = {row.id: row for row in session.scalars(select(record).options(
                        selectinload(record.options)).where(record.id.in_(
                            local_ids))).all()}
                    for bucket, item in candidates.items():
                        if item.id in local_found:
                            chosen[bucket] = (mode, local_found[item.id])
                            unresolved.remove(bucket)
                    continue
                research_ids = [item.id for item in candidates.values()
                                if isinstance(item, ScalperMarketSnapshotRecord)]
                research_found = self._load_research_rows(session, research_ids)
                for bucket, item in candidates.items():
                    research_row = research_found.get(item.id)
                    if (research_row is not None
                            and is_alpha_warmup_eligible(research_row) == mode):
                        chosen[bucket] = (mode, research_row)
                        unresolved.remove(bucket)
            history: list[MarketSnapshot] = []
            split: Counter[str] = Counter()
            option_aggregate_times: dict[datetime, datetime | None] = {}
            for bucket in sorted(chosen):
                mode, selected_row = chosen[bucket]
                if isinstance(selected_row, MarketSnapshotRecord):
                    converted = raw_record_to_model(
                        selected_row, normalize_research_timestamps=True)
                else:
                    converted = research_to_market_snapshot(selected_row)
                if converted.timestamp_ist < timestamp:
                    history.append(converted)
                    split[mode] += 1
                    if isinstance(selected_row, ScalperMarketSnapshotRecord):
                        option_aggregate_times[converted.timestamp_ist] = _aware(
                            selected_row.source_market_timestamp)
            self.last_history_option_aggregate_times = option_aggregate_times
            coverage, _, _ = window_coverage(
                history, timestamp, LookbackClockMode.TRADING_MINUTES,
                holidays=self.config.holiday_dates)
            self.last_history_report = {
                "history_first_at": history[0].timestamp_ist.isoformat() if history else None,
                "history_last_at": history[-1].timestamp_ist.isoformat() if history else None,
                "history_rows_loaded": len(history),
                "history_trading_minutes_covered": coverage,
                "history_provenance_split": {name: split[name] for name in PROVENANCE},
                "alpha1_required_minutes": self.config.alpha1_lookback_minutes,
                "alpha2_required_minutes": self.config.alpha2_lookback_minutes,
            }
            return history

    def load_raw(self, snapshot_id: int) -> MarketSnapshot | None:
        with self.sessions() as session:
            record = session.scalar(select(MarketSnapshotRecord).options(
                selectinload(MarketSnapshotRecord.options)).where(
                    MarketSnapshotRecord.id == snapshot_id,
                    MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE))
            return (None if record is None else raw_record_to_model(
                record, normalize_research_timestamps=True))

    def load_alpha_before(self, timestamp: datetime, version: str = ALPHA_VERSION,
                          mode: CalculationMode = CalculationMode.HISTORICAL_REPLAY
                          ) -> list[AlphaFeatureSnapshot]:
        with self.sessions() as session:
            records = session.scalars(select(AlphaFeatureSnapshotRecord).join(
                MarketSnapshotRecord,
                AlphaFeatureSnapshotRecord.market_snapshot_id == MarketSnapshotRecord.id
            ).where(MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE,
                    AlphaFeatureSnapshotRecord.alpha_version == version,
                    AlphaFeatureSnapshotRecord.calculation_mode == mode.value,
                    AlphaFeatureSnapshotRecord.timestamp < timestamp).order_by(
                        AlphaFeatureSnapshotRecord.timestamp.desc(),
                        AlphaFeatureSnapshotRecord.id.desc()).limit(300)).all()
            return [AlphaFeatureSnapshot.model_validate(row.result_json)
                    for row in reversed(records)]


class LocalActsAlpha:
    """Persist one exact Kotak market observation and Alpha result per 3-minute bucket."""

    def __init__(self, sessions: sessionmaker[Session], settings: object) -> None:
        self.sessions = sessions
        self.config = config_from_settings(settings)
        if self.config.price_source != "FUTURE":
            raise ValueError("ACTS_LOCAL_ALPHA_REQUIRES_FUTURE_PRICE_SOURCE")
        self.raw = SnapshotRepository(sessions)
        self.alpha = ActsAlphaRepository(sessions, self.config)

    def observe(self, market: MarketSnapshot) -> AlphaFeatureSnapshot:
        bucket = alpha_bucket(market.timestamp_ist)
        with self.sessions() as session:
            raw_result = session.execute(select(
                MarketSnapshotRecord.id, MarketSnapshotRecord.source).where(
                MarketSnapshotRecord.collection_bucket_ist == bucket))
            raw_row = raw_result.one_or_none()
        if raw_row is not None and raw_row.source != ACTS_ALPHA_SOURCE:
            raise ValueError("ACTS_ALPHA_BUCKET_OWNED_BY_ANOTHER_SOURCE")
        raw_id = None if raw_row is None else raw_row.id
        if raw_id is None:
            source = market.model_copy(update={"source": ACTS_ALPHA_SOURCE})
            raw_id = self.raw.save_market_snapshot(source, bucket).snapshot_id
        with self.sessions() as session:
            owner = session.scalar(select(MarketSnapshotRecord.source).where(
                MarketSnapshotRecord.id == raw_id))
        if owner != ACTS_ALPHA_SOURCE:
            raise ValueError("ACTS_ALPHA_BUCKET_OWNED_BY_ANOTHER_SOURCE")
        with self.sessions() as session:
            stored = session.scalar(select(AlphaFeatureSnapshotRecord.result_json).where(
                AlphaFeatureSnapshotRecord.market_snapshot_id == raw_id,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode == CalculationMode.LIVE_ORIGINAL.value))
        if stored is not None:
            return AlphaFeatureSnapshot.model_validate(stored)
        result = build_and_store_alpha(self.alpha, raw_id, self.config,
                                       CalculationMode.LIVE_ORIGINAL)
        logger.info("ACTS_LOCAL_ALPHA_CALCULATED snapshot_id=%d status=%s "
                    "validity=%s alpha1_rank=%d alpha2_rank=%d "
                    "history_rows=%d history_minutes=%.1f provenance=%s warnings=%s",
                    raw_id, result.status.value, result.validity_state.value,
                    result.rank_observations_alpha1, result.rank_observations_alpha2,
                    self.alpha.last_history_report.get("history_rows_loaded", 0),
                    self.alpha.last_history_report.get(
                        "history_trading_minutes_covered", 0.0),
                    self.alpha.last_history_report.get("history_provenance_split", {}),
                    result.warnings)
        return result
