"""Health checks observe freshness without captures, orders or journal writes."""
from datetime import timedelta

from sqlalchemy import func, select

from app.commodity_acts.paper_models import CommodityPaperEvent
from tests import test_commodity_paper as fixtures
from tests.test_commodity_acts import AT
from worker.commodity_acts_health import check

case = fixtures.case
research_case = fixtures.research_case


def test_closed_session_is_healthy_without_capturing_or_mutating(case):
    sessions, research, _, _ = case
    with sessions() as session:
        before = session.scalar(select(func.count()).select_from(CommodityPaperEvent))
    for instrument, service in research.items():
        assert check(sessions, service.config, instrument, AT.replace(hour=23, minute=45)) == {
            "healthy": True, "reason": "SESSION_CLOSED"}
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperEvent)) == before


def test_missing_and_fresh_processing_are_distinguished(case):
    sessions, research, _, clock = case
    service = research["CRUDEOILM"]
    assert not check(sessions, service.config, "CRUDEOILM", AT)["healthy"]
    fixtures.step(case, 0)
    assert check(sessions, service.config, "CRUDEOILM", clock.at)["reason"] == "FRESH"
    assert not check(sessions, service.config, "CRUDEOILM", clock.at + timedelta(seconds=91))["healthy"]


def test_disabled_worker_is_not_reported_operational(case):
    sessions, research, _, _ = case
    cfg = research["CRUDEOILM"].config.model_copy(update={"trading_enabled": False})
    assert not check(sessions, cfg, "CRUDEOILM", AT)["healthy"]
