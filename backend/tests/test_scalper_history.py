"""Bounded quote reads preserve full-session, causal features without old books."""
from datetime import timedelta

import pytest
from sqlalchemy import event, insert

from app.scalper.features import build_features
from app.scalper.models import (ScalperMarketSnapshotRecord, ScalperOptionQuoteRecord,
                                ScalperPriceObservation)
from app.scalper.repository import ScalperRepository
from tests.test_scalper_trend import trend_history


def test_projected_session_history_matches_full_history_after_1000_samples(session_factory, engine):
    template = trend_history(count=1)[0]
    observations, metadata, quotes = [], [], []
    for index in range(1301):
        at = template.captured_at + timedelta(seconds=index * 15)
        spot = 25000 + index % 41
        # The final row is future evidence with an extreme price and must be excluded.
        if index == 1300:
            spot = 90000
        raw = template.model_copy(update={
            "captured_at": at, "request_started_at": at - timedelta(seconds=1),
            "response_received_at": at, "source_market_timestamp": at,
            "nifty_spot": spot, "nifty_future": spot + 25})
        observations.append(raw)
        values = raw.model_dump(exclude={"quotes"})
        metadata.append(dict(values, id=index + 1, capture_key=str(index).zfill(64),
                             feature_json="{}", signal_json="{}"))
        # Deliberately persist no older books: none is needed by the optimized read.
        if index >= 1288:
            quotes.extend(dict(quote.model_dump(), scalper_snapshot_id=index + 1)
                          for quote in raw.quotes)
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), metadata)
        connection.execute(insert(ScalperOptionQuoteRecord), quotes)
    repo = ScalperRepository(session_factory)
    current = observations[1299]
    queries = []

    def capture_query(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture_query)
    try:
        prices = repo.price_history(since=template.captured_at, before=current.captured_at)
        assert len(queries) == 1
        projection = queries[0].split("FROM")[0]
        assert "quotes" not in projection and "feature_json" not in projection
        assert "signal_json" not in projection
        prior = repo.history(since=template.captured_at, before=current.captured_at,
                             inclusive=False, limit=11)
    finally:
        event.remove(engine, "before_cursor_execute", capture_query)
    assert len(prices) == 1299 and prices[0].captured_at == template.captured_at
    assert all(item.captured_at < current.captured_at for item in prices)
    assert len(prior) == 11 and sum(len(item.quotes) for item in prior) == 550
    assert any("LIMIT" in query for query in queries)
    expected = build_features(observations[:1300], expected_interval_seconds=15)
    actual = build_features([*prior, current], expected_interval_seconds=15,
                            context_history=prices)
    assert actual == expected
    assert actual.intraday.session_open == observations[0].nifty_spot
    assert actual.intraday.session_high < 90000


@pytest.mark.parametrize("offset", [0, 15, -24 * 60 * 60])
def test_projected_context_rejects_current_future_or_other_session_rows(offset):
    history = trend_history(count=2)
    current = history[-1]
    invalid = ScalperPriceObservation(
        captured_at=current.captured_at + timedelta(seconds=offset), nifty_spot=90000,
        nifty_future=None, future_instrument_id=None, future_expiry=None)
    with pytest.raises(ValueError, match="SCALPER_CONTEXT_HISTORY_NOT_STRICT"):
        build_features(history, context_history=[invalid])
