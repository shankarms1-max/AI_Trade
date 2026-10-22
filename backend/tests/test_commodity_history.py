"""Synthetic causal history regression; no broker or production database."""
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, time, timedelta
from math import sin
from types import SimpleNamespace
import json

import pytest
from pydantic import ValidationError

from app.alpha.engine import config_from_settings
from app.commodity_acts.config import CommoditySettings
from app.commodity_acts.context import build_context
from app.commodity_acts.history import select_history
from app.commodity_acts.models import CommodityMarketSnapshotRecord
from app.commodity_acts.paper_coordinator import CommodityPaperCoordinator
from app.commodity_acts.service import CommodityResearch
from tests.test_commodity_acts import AT
from tests import test_commodity_acts as fixtures

from tests.test_commodity_paper import raw
from tests.test_scalper import settings

research_case = fixtures.research_case


def make_history(count, cadence=15, instrument="CRUDEOILM"):
    original = raw(instrument)
    start = AT.replace(hour=9, minute=0)
    rows = []
    for index in range(count + 1):
        days, seconds = divmod(index * cadence, 870 * 60)
        day = start.date()
        for _ in range(days):
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
        at = datetime.combine(day, time(9), start.tzinfo) + timedelta(seconds=seconds)
        if count * cadence == 870 * 60 and index == count:
            at = start + timedelta(seconds=index * cadence)
        price = 5000 + 25 * sin(index / 500) + 5 * sin(index / 13)
        options = [q.model_copy(update={
            "source_market_timestamp": at - timedelta(seconds=1), "volume": 1000 + index * 100 + j,
            "open_interest": 5000 + index + j, "ltp": q.ltp * (1 + .01 * sin(index / 12 + j)),
        }) for j, q in enumerate(original.options)]
        rows.append(original.model_copy(update={
            "timestamp_ist": at, "request_started_at": at - timedelta(milliseconds=10),
            "response_received_at": at, "snapshot_persisted_at": at + timedelta(seconds=1),
            "source_market_timestamp": at - timedelta(seconds=1), "future_price": price,
            "reference_price": price, "session_id": f"MCX_{instrument}_{at.date()}", "options": options,
        }))
    return rows[:-1], rows[-1]


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
@pytest.mark.parametrize("count,cadence", [(1000, 15), (3480, 15), (3264, 15), (1001, 180)])
def test_full_alpha_and_entire_acts_context_are_identical(monkeypatch, count, cadence, instrument):
    history, current = make_history(count, cadence, instrument)
    cfg = CommoditySettings(_env_file=None, crudeoilm_session_start=time(9),
        crudeoilm_session_end=time(23, 30), crudeoilm_forced_exit=time(22, 30),
        crudeoilm_session_source="SYNTHETIC_TEST_ONLY", natgasmini_session_start=time(9),
        natgasmini_session_end=time(23, 30), natgasmini_forced_exit=time(22, 30),
        natgasmini_session_source="SYNTHETIC_TEST_ONLY")
    service = object.__new__(CommodityResearch)
    service.spec = cfg.specification(instrument)
    service.alpha_config = replace(config_from_settings(settings()), price_source="FUTURE")
    import app.alpha.engine as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = current.timestamp_ist + timedelta(seconds=1)
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    selected = select_history(iter(reversed(history)), current, service.spec, service.alpha_config)
    assert len(selected) <= 1000
    assert service._alpha(1, current, history, {}).model_dump() == service._alpha(1, current, selected, {}).model_dump()
    assert build_context(current, history, service.spec, Clock.now(), 30) == build_context(
        current, selected, service.spec, Clock.now(), 30)
    assert all(row.timestamp_ist < current.timestamp_ist for row in selected)
    if count == 3480:
        short = build_context(current, history[-1000:], service.spec, Clock.now(), 30)
        assert build_context(current, history, service.spec, Clock.now(), 30)["opening_high"] is not None
        assert short["opening_high"] is None
        assert short["structure"] != build_context(current, history, service.spec, Clock.now(), 30)["structure"]


def test_wrong_contract_and_future_persistence_are_excluded(research_case):
    service = research_case[1]["CRUDEOILM"]
    current = raw()
    at = current.timestamp_ist - timedelta(seconds=15)
    bad = [current.model_copy(update={"timestamp_ist": at, "snapshot_persisted_at": at, "future_token": "WRONG"}),
           current.model_copy(update={"timestamp_ist": at, "snapshot_persisted_at": current.timestamp_ist + timedelta(seconds=1)})]
    assert select_history(iter(bad), current, service.spec, service.alpha_config) == []
    with pytest.raises(ValueError, match="NONCAUSAL_HISTORY"):
        select_history(iter([current]), current, service.spec, service.alpha_config)


def test_malformed_unselected_source_is_still_validated(research_case):
    sessions, services, _ = research_case
    with sessions.begin() as session:
        session.add(CommodityMarketSnapshotRecord(instrument_id="CRUDEOILM", capture_key="malformed",
            captured_at=AT - timedelta(hours=1), snapshot_json=json.dumps({"instrument_id": "CRUDEOILM"})))
    with pytest.raises(ValidationError):
        services["CRUDEOILM"]._history(raw())


def test_shared_history_is_observation_scoped_and_cleared_after_failure(monkeypatch):
    coordinator = object.__new__(CommodityPaperCoordinator)
    coordinator._observation_history = ContextVar("test_observation", default=None)
    point = raw()
    calls = []
    monkeypatch.setattr(CommodityResearch, "_history", lambda self, row: calls.append(row) or [row])
    monkeypatch.setattr(CommodityResearch, "process_snapshot", lambda self, sid: self._history(point))
    coordinator.paper = SimpleNamespace(process_snapshot=lambda sid: coordinator._history(point))
    coordinator.process_snapshot(1)
    assert len(calls) == 1 and coordinator._observation_history.get() is None
    coordinator.process_snapshot(2)
    assert len(calls) == 2

    def fail(sid):
        coordinator._history(point)
        raise RuntimeError("SYNTHETIC_FAILURE")

    coordinator.paper.process_snapshot = fail
    with pytest.raises(RuntimeError, match="SYNTHETIC_FAILURE"):
        coordinator.process_snapshot(3)
    assert coordinator._observation_history.get() is None
    coordinator._history(point)
    assert len(calls) == 4
