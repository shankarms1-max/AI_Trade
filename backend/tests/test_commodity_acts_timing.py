"""Observation-time ACTS state, honest processing-time Alpha, no execution."""
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import select

from app.commodity_acts.config import INSTRUMENTS, RESEARCH_SCOPE, STAGE
from app.commodity_acts.context import build_context
from app.commodity_acts.models import CommodityActsDecision, CommodityActsEvent
from app.commodity_acts.read import decisions, summary
from app.commodity_acts.repository import save_snapshot, verify_journal
from app.commodity_acts.service import CommodityResearch
from tests.test_commodity_acts import AT, captured, config, research_case
from tests.test_scalper import settings


@pytest.mark.parametrize("instrument", INSTRUMENTS)
@pytest.mark.parametrize("delay", [0, 1, 3600])
def test_context_clock_is_observation_time_with_separate_processing_provenance(instrument, delay):
    current = captured(instrument)
    processing = AT + timedelta(seconds=delay)
    ctx = build_context(current, [], config().specification(instrument), processing, 30)
    assert ctx["timestamp"] == ctx["observed_at"] == AT.isoformat()
    assert ctx["decided_at"] == processing.isoformat()
    assert ctx["reference_price"] == current.future_price


@pytest.mark.parametrize("instrument", INSTRUMENTS)
@pytest.mark.parametrize("recovered", [False, True], ids=["fresh", "restart"])
@pytest.mark.parametrize("gap,expected", [(30, "BULL_RESUMPTION"), (240, "STRONG_BULL")])
def test_watch_lifetime_uses_observations_not_restart_clock(
        research_case, monkeypatch, instrument, recovered, gap, expected):
    sessions, services, clock = research_case
    first, second = captured(instrument), captured(instrument, at=AT + timedelta(seconds=gap))
    import app.commodity_acts.service as module
    original_context, original_alpha = module.build_context, module.alpha_evidence
    contexts, known_at = [], []

    def directional_context(current, *args):
        ctx = original_context(current, *args)
        # Explicit synthetic setup inputs; use the real unchanged scorer/watch
        # and the production build_context timestamp semantics throughout.
        ctx["futures_returns_bps"] = {"1m": 1, "3m": -3 if current.timestamp_ist == AT else 4,
                                      "5m": 10, "15m": 40}
        ctx["structure"]["range_position"] = .9
        contexts.append(ctx.copy())
        return ctx

    def alpha_as_actually_known(alpha, at, *identity):
        known_at.append((alpha.feature_calculated_at, at))
        return original_alpha(alpha, at, *identity)

    monkeypatch.setattr(module, "build_context", directional_context)
    monkeypatch.setattr(module, "alpha_evidence", alpha_as_actually_known)
    if recovered:
        # Insert in reverse ID order to prove recovery sorts by observation,
        # not snapshot ID or a shared restart/processing timestamp.
        second_id, _ = save_snapshot(sessions, second, second.timestamp_ist + timedelta(microseconds=1))
        first_id, _ = save_snapshot(sessions, first, first.timestamp_ist + timedelta(microseconds=1))
        clock.at = AT + timedelta(hours=1)
        restarted = CommodityResearch(sessions, settings(), config(), config().specification(instrument),
                                      clock=lambda: clock.at)

        class Market:
            def capture(self, _spec):
                return second  # Existing bucket: no additional synthetic capture.

        assert restarted.capture_once(Market(), clock.at) == (second_id, "DUPLICATE")
    else:
        clock.at = AT + timedelta(seconds=1)
        first_id, _ = services[instrument].observe(first)
        clock.at = second.timestamp_ist + timedelta(seconds=1)
        second_id, _ = services[instrument].observe(second)

    with sessions() as session:
        documents = [json.loads(row.payload)["latest"] for row in session.scalars(
            select(CommodityActsEvent).where(CommodityActsEvent.instrument_id == instrument).order_by(
                CommodityActsEvent.sequence))]
    assert [doc["snapshot_id"] for doc in documents] == [first_id, second_id]
    assert [ctx["timestamp"] for ctx in contexts] == [first.timestamp_ist.isoformat(), second.timestamp_ist.isoformat()]
    assert [doc["acts"]["timestamp"] for doc in documents] == [ctx["timestamp"] for ctx in contexts]
    assert documents[0]["acts"]["market_state"] == "BULL_PULLBACK"
    assert documents[0]["acts"]["watch_state"]["started_at"] == first.timestamp_ist.isoformat()
    assert documents[1]["acts"]["market_state"] == expected
    assert bool(documents[1]["acts"]["watch_state"]) == (gap <= 180)
    for doc, (calculated_at, at) in zip(documents, known_at):
        observed = datetime.fromisoformat(doc["observed_at"])
        decided = datetime.fromisoformat(doc["decided_at"])
        assert decided == at and decided > observed
        assert datetime.fromisoformat(doc["alpha"]["feature_calculated_at"]) == calculated_at
        assert datetime.fromisoformat(doc["timestamp_provenance"]["feature_calculated_at"]) == calculated_at
        assert observed <= calculated_at <= decided or gap == 30 and calculated_at <= decided
        if recovered:
            assert decided == calculated_at == clock.at
            assert doc["acts"]["alpha"]["status"] != "AVAILABLE"
        assert doc["stage"] == STAGE and doc["research_scope"] == RESEARCH_SCOPE
        assert doc["acts"]["status"] != "ENTER"
        assert "NO_EXECUTABLE_CANDIDATE" in doc["acts"]["blockers"]
        assert doc["acts"]["score_components"]["execution"] == 0
        assert doc["maximum_score_without_execution"] == 86
        assert doc["acts"]["opportunity_score"] == pytest.approx(sum(doc["acts"]["score_components"].values()))
        assert doc["broker_orders_enabled"] is doc["spread_execution_enabled"] is False and doc["pnl"] is None
    verify_journal(sessions, instrument)
    # A further restart verifies immutable event/capture/decision projections.
    CommodityResearch(sessions, settings(), config(), config().specification(instrument), clock=lambda: clock.at)


def test_api_exposes_score_only_scope_and_keeps_unconfirmed_provenance(research_case):
    sessions, services, clock = research_case
    services["CRUDEOILM"].observe(captured())
    overview = summary(sessions, config(), clock.at)
    assert overview["stage"] == STAGE and overview["research_scope"] == RESEARCH_SCOPE
    row = decisions(sessions, "CRUDEOILM")["items"][0]
    assert row["stage"] == STAGE
    assert row["acts"]["timestamp"] == row["observed_at"] == AT.isoformat()
    assert row["decided_at"] == clock.at.isoformat()
    assert row["geometry"]["depth_unit"] == "UNKNOWN"
    assert row["data_provenance"]["option_future_link"].endswith("LINK_NOT_CONFIRMED")
    assert row["maximum_score_without_execution"] == 86
    assert row["broker_orders_enabled"] is row["spread_execution_enabled"] is False and row["pnl"] is None


def test_future_alpha_calculation_is_rejected_not_backdated(research_case, monkeypatch):
    sessions, services, clock = research_case
    research = services["CRUDEOILM"]
    original_alpha = research._alpha

    def not_yet_known(*args):
        return original_alpha(*args).model_copy(update={"feature_calculated_at": clock.at + timedelta(seconds=1)})

    monkeypatch.setattr(research, "_alpha", not_yet_known)
    with pytest.raises(ValueError, match="ALPHA_NOT_YET_KNOWN"):
        research.observe(captured())
    with sessions() as session:
        assert session.scalar(select(CommodityActsDecision.id)) is None
    verify_journal(sessions, "CRUDEOILM")
