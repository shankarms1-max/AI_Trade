"""Basket volume uses one exact REST schema; missing REST data never falls back to chain volume."""
from datetime import date, datetime, timedelta

import pytest

from app.broker.base import MarketDataError
from app.parallel_paper.capture import capture_market, make_snapshot_builder
from app.parallel_paper.config import CAPTURE_MODE
from app.parallel_paper.market import ParallelMarketRepository
from app.db.session import build_session_factory
from app.scalper.capture import to_scalper_snapshot
from app.scalper.research_capture import ResearchSnapshotRepository
from app.scalper.repository import IST
from tests.test_parallel_paper_service import raw
from tests.test_scalper import settings
from worker.parallel_paper_worker import paper_only_builder


class MarketOnlyBroker:
    def __init__(self, market, response="VALID"):
        self.market, self.response, self.requests = market, response, []

    def get_nifty_spot(self):
        return self.market.nifty_spot

    def get_nifty_expiries(self):
        return [self.market.expiry]

    def get_nifty_option_chain(self, expiry):
        return [quote.model_copy(update={"volume": 999999}) for quote in self.market.options]

    def enrich_option_quotes(self, quotes):
        return quotes

    def get_nifty_future_identity(self):
        return "NIFTY_FUT", date(2099, 10, 29)

    def get_nifty_future(self):
        return 25040

    def get_india_vix(self):
        return None

    def get_nifty_lot_size(self):
        return 65

    def capture_required_option_quotes(self, contracts):
        self.requests.append(contracts)
        if self.response == "FAILED":
            raise MarketDataError("fake unavailable volume response")
        result = [quote.model_copy(update={
            "volume": 100 + index * 100, "source_market_timestamp": datetime.now(IST) - timedelta(seconds=1)})
            for index, quote in enumerate(contracts)]
        return result[:1] if self.response == "PARTIAL" else result

    def place_order(self):
        raise AssertionError("paper capture attempted a live broker order")


def test_fixed_pair_rest_volume_is_fetched_even_inside_chain_and_timestamps_preserved(market_snapshot):
    broker = MarketOnlyBroker(market_snapshot)
    result = capture_market(broker, settings(), [], [])
    assert len(broker.requests) == 1 and len(broker.requests[0]) == 2
    pair = [quote for quote in result.options if quote.strike == result.atm_strike]
    assert sorted(quote.volume for quote in pair) == [100, 200]
    assert all(quote.source_market_timestamp <= result.response_received_at for quote in pair)
    assert all(quote.volume == 999999 for quote in result.options if quote.strike != result.atm_strike)
    assert result.future_instrument_id == "NIFTY_FUT"
    assert result.future_expiry == date(2099, 10, 29)


@pytest.mark.parametrize("response", ["PARTIAL", "FAILED"])
def test_missing_rest_volume_is_unavailable_without_chain_fallback(market_snapshot, response):
    result = capture_market(MarketOnlyBroker(market_snapshot, response), settings(), [], [])
    pair = [quote for quote in result.options if quote.strike == result.atm_strike]
    assert any(quote.volume is None for quote in pair)
    assert all(quote.volume != 999999 for quote in pair)
    assert any(quote.volume == 999999 for quote in result.options if quote.strike != result.atm_strike)


def test_new_builder_reuses_market_only_auth_and_returns_capability_guarded_closure(monkeypatch, market_snapshot):
    broker = MarketOnlyBroker(market_snapshot)
    monkeypatch.setattr("app.parallel_paper.capture.create_market_data_client", lambda _settings: object())
    monkeypatch.setattr("app.parallel_paper.capture.KotakMarketDataAdapter", lambda *_args, **_kwargs: broker)
    builder = make_snapshot_builder(settings(), lambda: [], lambda _at: [])
    paper_only_builder(builder)
    result = builder()
    assert to_scalper_snapshot(result).nifty_future == 25040
    assert len(broker.requests) == 1


def test_unverified_counter_provenance_cannot_qualify_volume(engine):
    observation = raw(0)
    ResearchSnapshotRepository(engine, mode=CAPTURE_MODE).save(observation)
    market = ParallelMarketRepository(build_session_factory(engine))
    points, evidence = market.series(observation.captured_at, 30)
    assert len(points) == 1 and points[0].volume is None
    assert points[0].volume_reason == "OPTION_VOLUME_PROVENANCE_UNAVAILABLE"
    assert evidence["volume_samples_valid"] == 0


def test_research_and_acts_capture_cannot_claim_parallel_basket_provenance(engine):
    with pytest.raises(ValueError, match="CAPTURE_EVIDENCE_INVALID"):
        ResearchSnapshotRepository(engine, mode="ACTS_PAPER", capture_evidence={
            "volume_capture": "KOTAK_REST_ALL_LAST_VOLUME"})
