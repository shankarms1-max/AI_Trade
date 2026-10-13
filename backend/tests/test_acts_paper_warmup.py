"""Causal, read-only Phase 15 history for original ACTS Alpha warm-up."""
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.acts_paper.alpha import (ActsAlphaRepository, LocalActsAlpha, alpha_bucket,
                                  is_alpha_warmup_eligible,
                                  research_to_market_snapshot)
from app.acts_paper.diagnostics import alpha_readiness, regime_readiness
from app.alpha.models import ALPHA_VERSION, AlphaValidity, CalculationMode
from app.alpha.volume_alpha import interval_volume
from app.data.models import MarketSnapshot
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.db.session import build_session_factory
from app.research.manifest import canonical, digest
from app.scalper.capture import to_scalper_snapshot
from app.scalper.models import ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord
from app.scalper.research_capture import (ACTS_PAPER_MODE, MODE,
                                          ResearchSnapshotRepository, capture_bucket)
from app.scalper_lab import acts
from app.scalper_lab.context import ResearchObservation
from tests.test_acts_paper import _capture, _persist_alpha, _service
from tests.test_acts_paper_local_alpha import _market
from tests.test_scalper import settings
from tests.test_scalper_acts import acts_context, qualified_rows


def _at(market: MarketSnapshot, at: datetime, *, volume_extra=0) -> MarketSnapshot:
    options = [quote.model_copy(update={
        "source_market_timestamp": at - timedelta(seconds=2),
        "volume": quote.volume + volume_extra if quote.volume is not None else None,
    }) for quote in market.options]
    return market.model_copy(update={
        "timestamp_ist": at, "request_started_at": at - timedelta(seconds=1),
        "response_received_at": at, "source_market_timestamp": at - timedelta(seconds=2),
        "options": options,
    })


def _save(engine, market: MarketSnapshot, mode: str = MODE) -> int:
    return ResearchSnapshotRepository(engine, mode=mode).save(
        to_scalper_snapshot(market)).snapshot_id


def _legacy(engine, snapshot_id: int, at: datetime, *, trusted=True) -> None:
    context = {"mode": "LEGACY_RESEARCH", "status": "CAPTURED",
               "bucket": capture_bucket(at).isoformat(),
               "origin": "KOTAK_NEO_READ_ONLY" if trusted else "UNKNOWN"}
    with Session(engine) as session:
        session.execute(update(ScalperMarketSnapshotRecord).where(
            ScalperMarketSnapshotRecord.id == snapshot_id).values(
                context_json=canonical(context),
                capture_key=digest({"mode": "LEGACY_RESEARCH",
                                    "bucket": capture_bucket(at).isoformat()})))
        session.commit()


def test_research_adapter_preserves_exact_inputs_without_fabrication(
        engine, market_snapshot):
    base = _market(market_snapshot)
    market = _at(base, base.timestamp_ist)
    snapshot_id = _save(engine, market)
    with Session(engine) as session:
        record = session.scalar(select(ScalperMarketSnapshotRecord).options(
            selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                ScalperMarketSnapshotRecord.id == snapshot_id))
        assert record is not None
        assert is_alpha_warmup_eligible(record) == MODE
        converted = research_to_market_snapshot(record)
    assert isinstance(converted, MarketSnapshot)
    assert converted.timestamp_ist == market.timestamp_ist
    assert converted.request_started_at == market.request_started_at
    assert converted.response_received_at == market.response_received_at
    assert converted.source_market_timestamp == market.source_market_timestamp
    assert converted.nifty_spot == market.nifty_spot
    assert converted.nifty_future == market.nifty_future
    assert converted.future_instrument_id == market.future_instrument_id
    assert converted.future_expiry == market.future_expiry
    assert converted.expiry == market.expiry
    assert converted.lot_size == market.lot_size
    assert converted.india_vix == market.india_vix
    original, quote = market.options[0], converted.options[0]
    assert (quote.expiry, quote.strike, quote.option_type, quote.exchange,
            quote.trading_symbol, quote.instrument_token) == (
                original.expiry, original.strike, original.option_type,
                original.exchange, original.trading_symbol, original.instrument_token)
    assert (quote.ltp, quote.volume, quote.open_interest, quote.bid, quote.ask,
            quote.bid_quantity, quote.ask_quantity, quote.depth_unit,
            quote.tick_size, quote.source_market_timestamp) == (
                original.ltp, original.volume, original.open_interest,
                original.bid, original.ask, original.bid_quantity,
                original.ask_quantity, original.depth_unit, original.tick_size,
                original.source_market_timestamp)
    assert quote.previous_open_interest is None
    assert quote.change_in_open_interest is None
    assert quote.delta is None and quote.gamma is None


def test_volume_interval_survives_research_conversion(engine, market_snapshot):
    base = _market(market_snapshot)
    first = _at(base, base.timestamp_ist)
    later = _at(base, base.timestamp_ist + timedelta(minutes=3), volume_extra=20)
    _save(engine, first)
    _save(engine, later)
    history = ActsAlphaRepository(build_session_factory(engine),
                                  LocalActsAlpha(build_session_factory(engine),
                                                 settings(acts_paper_enabled=True)).config
                                  ).load_history_before(later.timestamp_ist + timedelta(minutes=3))
    assert len(history) == 2
    interval = interval_volume(history[1].options[0], history[0], history[1])
    assert interval.value == 20


def test_history_priority_causality_and_restart(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    base = _market(market_snapshot)
    initial = base.timestamp_ist.replace(minute=15, second=0, microsecond=0)
    prior = _at(base, initial)
    current_bucket = _at(base, initial + timedelta(minutes=3))
    future = _at(base, initial + timedelta(minutes=6))
    research_id = _save(engine, prior, MODE)
    acts_id = _save(engine, prior.model_copy(update={"nifty_spot": prior.nifty_spot + 1}),
                    ACTS_PAPER_MODE)
    backfilled = _at(base, initial - timedelta(minutes=3))
    backfilled_id = _save(engine, backfilled, MODE)
    _save(engine, current_bucket, ACTS_PAPER_MODE)
    _save(engine, future, ACTS_PAPER_MODE)
    current = current_bucket.model_copy(update={
        "timestamp_ist": current_bucket.timestamp_ist + timedelta(seconds=5),
        "response_received_at": current_bucket.response_received_at + timedelta(seconds=5)})
    with Session(engine) as session:
        session.execute(update(ScalperMarketSnapshotRecord).where(
            ScalperMarketSnapshotRecord.id == backfilled_id).values(
                created_at=current.timestamp_ist + timedelta(minutes=1)))
        session.commit()
    before = (research_id, acts_id)
    repository = producer.alpha
    history = repository.load_history_before(current.timestamp_ist)
    restarted = ActsAlphaRepository(sessions, producer.config)
    repeated = restarted.load_history_before(current.timestamp_ist)
    assert len(history) == len(repeated) == 1
    assert history[0].nifty_spot == prior.nifty_spot + 1
    assert [row.timestamp_ist for row in history] == [prior.timestamp_ist]
    assert repository.last_history_report["history_provenance_split"]["ACTS_PAPER"] == 1
    assert restarted.last_history_report == repository.last_history_report
    with Session(engine) as session:
        assert tuple(session.scalars(select(ScalperMarketSnapshotRecord.id).where(
            ScalperMarketSnapshotRecord.id.in_(before)).order_by(
                ScalperMarketSnapshotRecord.id))) == before
        assert session.scalar(select(func.count()).select_from(
            AlphaFeatureSnapshotRecord)) == 0


def test_research_only_and_verified_legacy_are_explicitly_eligible(
        engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    base = _market(market_snapshot)
    start = base.timestamp_ist.replace(minute=15, second=0, microsecond=0)
    research = _at(base, start)
    legacy = _at(base, start + timedelta(minutes=3))
    bad = _at(base, start + timedelta(minutes=6))
    incomplete = _at(base, start + timedelta(minutes=9))
    _save(engine, research, MODE)
    legacy_id = _save(engine, legacy, MODE)
    bad_id = _save(engine, bad, MODE)
    incomplete_id = _save(engine, incomplete, MODE)
    _legacy(engine, legacy_id, legacy.timestamp_ist)
    _legacy(engine, bad_id, bad.timestamp_ist, trusted=False)
    _legacy(engine, incomplete_id, incomplete.timestamp_ist)
    with Session(engine) as session:
        session.execute(update(ScalperOptionQuoteRecord).where(
            ScalperOptionQuoteRecord.scalper_snapshot_id == incomplete_id).values(
                volume=None))
        session.commit()
    history = producer.alpha.load_history_before(
        start + timedelta(minutes=12))
    assert [row.timestamp_ist for row in history] == [
        research.timestamp_ist, legacy.timestamp_ist]
    assert producer.alpha.last_history_report["history_provenance_split"] == {
        "ACTS_PAPER": 0, "RESEARCH_ONLY": 1,
        "LEGACY_RESEARCH": 1, "LOCAL_ALPHA": 0}


def test_existing_local_raw_wins_completed_bucket_without_double_counting(
        engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    base = _market(market_snapshot)
    first = _at(base, base.timestamp_ist.replace(minute=15, second=0, microsecond=0))
    _save(engine, first, ACTS_PAPER_MODE)
    producer.observe(first.model_copy(update={"nifty_spot": first.nifty_spot + 2}))
    at = first.timestamp_ist + timedelta(minutes=3)
    history = producer.alpha.load_history_before(at)
    assert len(history) == 1
    assert history[0].nifty_spot == first.nifty_spot + 2
    assert producer.alpha.last_history_report["history_provenance_split"]["LOCAL_ALPHA"] == 1
    assert alpha_bucket(history[0].timestamp_ist) < alpha_bucket(at)


def test_warmup_never_mutates_research_evidence(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    base = _market(market_snapshot)
    first = _at(base, base.timestamp_ist.replace(minute=15, second=0, microsecond=0))
    snapshot_id = _save(engine, first, MODE)

    def evidence():
        with Session(engine) as session:
            record = session.scalar(select(ScalperMarketSnapshotRecord).options(
                selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                    ScalperMarketSnapshotRecord.id == snapshot_id))
            assert record is not None
            return (record.capture_key, record.context_json, record.feature_json,
                    record.signal_json, [(q.id, q.ltp, q.volume, q.open_interest)
                                         for q in record.quotes])

    before = evidence()
    producer.observe(_at(base, first.timestamp_ist + timedelta(minutes=3)))
    assert evidence() == before


def test_original_alpha_warms_across_300_and_800_trading_minutes(
        engine, market_snapshot):
    """Full configured windows work from persisted research without a raw backfill."""
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    assert producer.config.alpha1_lookback_minutes == 800
    assert producer.config.alpha2_lookback_minutes == 300
    assert producer.config.volume_lookback_minutes == 300
    assert producer.config.volatility_lookback_minutes == 300
    base = _market(market_snapshot)
    pair = [quote for quote in base.options if quote.strike == 25050]
    assert len(pair) == 2
    base = base.model_copy(update={"options": pair})
    points = []
    absolute = 0
    for day in (1, 2, 5):
        opening = datetime(2099, 10, day, 9, 15, tzinfo=base.timestamp_ist.tzinfo)
        for index in range(120 if day != 5 else 60):
            at = opening + timedelta(minutes=index * 3)
            quotes = [quote.model_copy(update={
                "ltp": ((100 if quote.option_type.value == "CE" else 95)
                        + ((absolute * (7 if quote.option_type.value == "CE" else 5)) % 13)),
                "volume": 500 + index * 30,
                "open_interest": 1000 + index,
                "source_market_timestamp": at - timedelta(seconds=2),
            }) for quote in pair]
            market = base.model_copy(update={
                "timestamp_ist": at,
                "request_started_at": at - timedelta(seconds=1),
                "response_received_at": at,
                "source_market_timestamp": at - timedelta(seconds=2),
                "nifty_future": 25040 + (absolute % 7) * 2 + absolute * 0.02,
                "options": quotes,
            })
            _save(engine, market, ACTS_PAPER_MODE if day == 5 else MODE)
            points.append(market)
            absolute += 1
    friday = next(point for point in points if point.timestamp_ist.day == 2
                  and point.timestamp_ist.hour == 14
                  and point.timestamp_ist.minute == 15)
    monday = points[-1].model_copy(update={
        "timestamp_ist": points[-1].timestamp_ist + timedelta(minutes=3),
        "request_started_at": points[-1].timestamp_ist + timedelta(minutes=3),
        "response_received_at": points[-1].timestamp_ist + timedelta(minutes=3),
        "source_market_timestamp": points[-1].timestamp_ist + timedelta(minutes=3),
        "options": [quote.model_copy(update={
            "ltp": quote.ltp + 2, "volume": quote.volume + 30,
            "source_market_timestamp": points[-1].timestamp_ist + timedelta(minutes=3)})
            for quote in points[-1].options],
    })
    friday_result = producer.observe(friday)
    monday_result = producer.observe(monday)
    assert friday_result.alpha_2 is not None
    assert monday_result.alpha_1 is not None
    assert monday_result.alpha_2 is not None
    assert monday_result.validity_state == AlphaValidity.VALID
    assert monday_result.alpha_version == ALPHA_VERSION
    assert monday_result.calculation_mode == CalculationMode.LIVE_ORIGINAL
    assert monday_result.price_source == "FUTURE"
    assert producer.alpha.last_history_report["history_trading_minutes_covered"] >= 800
    friday_details = alpha_readiness(
        ResearchObservation(-1, to_scalper_snapshot(friday), alpha=friday_result),
        sessions, producer.config)
    monday_details = alpha_readiness(
        ResearchObservation(-1, to_scalper_snapshot(monday), alpha=monday_result),
        sessions, producer.config)
    assert friday_details["alpha2_ready"]
    assert not friday_details["alpha1_ready"]
    assert monday_details["alpha1_ready"] and monday_details["alpha2_ready"]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(
            MarketSnapshotRecord)) == 2
        assert session.scalar(select(func.count()).select_from(
            AlphaFeatureSnapshotRecord)) == 2


def test_status_reports_alpha_and_exact_regime_inputs_without_changing_decision(engine):
    service = _service(engine)
    row = qualified_rows(-1)[0]
    snapshot_id = _capture(engine, row.snapshot)
    expected = service.process_snapshot(snapshot_id)
    status = service.status(row.snapshot.captured_at)
    latest = status["latest_observation"]
    assert latest["alpha_diagnostics"]["alpha_status"] == "ALPHA_WARMING_UP"
    assert latest["regime_diagnostics"]["spot"] == row.snapshot.nifty_spot
    assert latest["regime_diagnostics"]["future"] == row.snapshot.nifty_future
    assert set(latest["regime_diagnostics"]["futures_returns_bps"]) == {
        "3m", "5m", "15m"}
    assert latest["regime_diagnostics"]["direction"] is None
    assert latest["regime_diagnostics"]["regime"] == "RANGE"
    assert latest["regime_diagnostics"]["primary_failure_reason"] is not None
    assert expected == "NO_SETUP"
    assert service.process_snapshot(snapshot_id) == "DUPLICATE"


def test_alpha_readiness_distinguishes_available_stale_and_identity(engine):
    sessions = build_session_factory(engine)
    config = LocalActsAlpha(sessions, settings(acts_paper_enabled=True)).config
    row = qualified_rows()[0]
    _persist_alpha(engine, row, "BEAR")
    fresh = ResearchObservation(1, row.snapshot.model_copy(update={
        "captured_at": row.snapshot.captured_at + timedelta(seconds=15)}))
    selected = _service(engine)._alpha(fresh)
    assert alpha_readiness(selected, sessions, config)["alpha_status"] == "ALPHA_AVAILABLE"
    stale = ResearchObservation(2, row.snapshot.model_copy(update={
        "captured_at": row.snapshot.captured_at + timedelta(seconds=181)}))
    assert alpha_readiness(stale, sessions, config)["alpha_status"] == "ALPHA_STALE"
    wrong = ResearchObservation(3, fresh.snapshot.model_copy(update={
        "future_instrument_id": "OTHER_FUTURE"}))
    assert alpha_readiness(wrong, sessions, config)[
        "alpha_status"] == "ALPHA_IDENTITY_MISMATCH"


def test_first_local_alpha_result_reports_warmup_reason(engine, market_snapshot):
    sessions = build_session_factory(engine)
    producer = LocalActsAlpha(sessions, settings(acts_paper_enabled=True))
    market = _market(market_snapshot)
    produced = producer.observe(market)
    observation = ResearchObservation(
        -1, to_scalper_snapshot(market), alpha=None)
    detail = alpha_readiness(observation, sessions, producer.config)
    assert produced.status.value == "WARMING_UP"
    assert detail["alpha_status"] == "ALPHA_WARMING_UP"
    assert detail["alpha_validity_state"] == produced.validity_state.value
    assert "INSUFFICIENT_ALPHA_HISTORY" in detail["warnings"]


def test_regime_diagnostics_reflect_frozen_gates_without_affecting_acts():
    ctx = acts_context(qualified_rows(-1))
    before = acts.decide(ctx, {})
    detail = regime_readiness(ctx)
    after = acts.decide(ctx, {})
    assert before == after
    assert detail["futures_returns_bps"]["5m"] == ctx["futures_returns_bps"]["5m"]
    assert detail["futures_returns_bps"]["15m"] == ctx["futures_returns_bps"]["15m"]
    assert detail["range_position"] == ctx["structure"]["range_position"]
    assert detail["opening_range_complete"] == ctx["structure"]["opening_range_complete"]
    assert detail["vwap"]["status"] == ctx["vwap"]["status"]
    assert detail["direction"] == ctx["acts"]["direction"]
    assert detail["direction_tests"]["BEAR"]["final_pass"]
    assert detail["direction_tests"]["BEAR"]["vwap_alignment_pass"]
    assert detail["direction_tests"]["BEAR"]["vwap_gate_applied"] == (
        ctx["vwap"]["status"] == "AVAILABLE")
