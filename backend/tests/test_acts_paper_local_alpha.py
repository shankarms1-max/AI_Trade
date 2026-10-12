"""Local Phase 15 captures reuse the unchanged Phase 14.1 Alpha service."""
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.acts_paper.alpha import (ACTS_ALPHA_SOURCE, ActsAlphaRepository,
                                  LocalActsAlpha, alpha_bucket)
from app.alpha.models import ALPHA_VERSION, AlphaValidity, CalculationMode
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.db.repositories import SnapshotRepository
from app.db.session import build_session_factory
from tests.test_scalper import settings


def _market(market_snapshot):
    return market_snapshot.model_copy(update={
        "future_instrument_id": "nse_fo|NIFTY_FUT",
        "future_expiry": date(2099, 10, 29), "lot_size": 65,
        "request_started_at": market_snapshot.timestamp_ist - timedelta(seconds=1),
        "response_received_at": market_snapshot.timestamp_ist})


def test_local_alpha_generation_is_original_future_and_idempotent(
        engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    market = _market(market_snapshot)

    first = producer.observe(market)
    repeated = producer.observe(market.model_copy(update={
        "timestamp_ist": market.timestamp_ist + timedelta(seconds=15)}))

    assert first.snapshot_id == repeated.snapshot_id
    assert first.alpha_version == ALPHA_VERSION
    assert first.calculation_mode == CalculationMode.LIVE_ORIGINAL
    assert first.price_source == "FUTURE"
    assert first.reference_instrument_id == market.future_instrument_id
    assert first.reference_expiry == market.future_expiry
    assert first.expiry == market.expiry
    assert first.validity_state != AlphaValidity.VALID  # 800/300-minute warm-up
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(
            AlphaFeatureSnapshotRecord)) == 1
        raw = session.get(MarketSnapshotRecord, first.snapshot_id)
        assert raw is not None
        assert raw.source == ACTS_ALPHA_SOURCE
        assert raw.collection_bucket_ist.replace(tzinfo=market.timestamp_ist.tzinfo) == (
            alpha_bucket(market.timestamp_ist))


def test_missing_futures_identity_remains_invalid(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    market = _market(market_snapshot).model_copy(update={
        "nifty_future": None, "future_instrument_id": None, "future_expiry": None})

    result = producer.observe(market)

    assert result.price_source == "FUTURE"
    assert result.validity_state == AlphaValidity.INVALID
    assert result.alpha_1 is None and result.alpha_2 is None


def test_local_alpha_refuses_a_bucket_owned_by_another_capture(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    market = _market(market_snapshot)
    SnapshotRepository(sessions).save_market_snapshot(market, alpha_bucket(
        market.timestamp_ist))

    with pytest.raises(ValueError, match="ACTS_ALPHA_BUCKET_OWNED"):
        producer.observe(market)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(
            AlphaFeatureSnapshotRecord)) == 0


def test_local_alpha_history_excludes_future_market_rows(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    first = _market(market_snapshot)
    later = first.model_copy(update={
        "timestamp_ist": first.timestamp_ist + timedelta(minutes=6),
        "response_received_at": first.timestamp_ist + timedelta(minutes=6)})
    producer.observe(first)
    producer.observe(later)

    repository = ActsAlphaRepository(sessions, producer.config)
    history = repository.load_history_before(later.timestamp_ist)

    assert [row.timestamp_ist for row in history] == [first.timestamp_ist]


def test_local_market_history_can_produce_valid_original_alpha(engine, market_snapshot):
    sessions = build_session_factory(engine)
    config = settings(
        acts_paper_enabled=True,
        alpha1_lookback_minutes=30,
        alpha2_lookback_minutes=30,
        alpha_volume_lookback_minutes=30,
        alpha_volatility_lookback_minutes=30,
        alpha_min_rank_observations=2,
        alpha_min_volume_observations=1,
        alpha_min_volatility_returns=2,
    )
    producer = LocalActsAlpha(sessions, config)
    base = _market(market_snapshot)
    results = []
    for index in range(20):
        at = base.timestamp_ist + timedelta(minutes=index * 3)
        options = [quote.model_copy(update={
            "ltp": quote.ltp + ((index * (2 if quote.option_type.value == "CE" else 3)) % 7),
            "volume": quote.volume + index * 100,
        }) for quote in base.options]
        current = base.model_copy(update={
            "timestamp_ist": at,
            "request_started_at": at - timedelta(seconds=1),
            "response_received_at": at,
            "nifty_future": 25040 + (index % 5) * 1.5 + index * 0.1,
            "options": options,
        })
        results.append(producer.observe(current))

    assert any(row.validity_state == AlphaValidity.VALID for row in results)
    assert all(row.alpha_version == ALPHA_VERSION for row in results)
    assert all(row.calculation_mode == CalculationMode.LIVE_ORIGINAL for row in results)
    assert all(row.price_source == "FUTURE" for row in results)
