"""Offline depth capture against the official Kotak v3 REST response schema."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.broker.kotak.depth import parse_quote_timestamp, quote_identity
from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.data.snapshot import build_market_snapshot


def quote(token="40611", **updates):
    return {"exchange": "nse_fo", "exchange_token": token,
            "lstup_time": "1782374657", "ltp": "9999", "open_int": "8888",
            "last_volume": "7777", "total_buy": "1000000", "total_sell": "2000000",
            "depth": {"buy": [{"price": "100.10", "quantity": "75", "orders": "2"},
                              {"price": "100.05", "quantity": "999"}],
                      "sell": [{"price": "100.20", "quantity": "150", "orders": "3"}]},
            **updates}


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def quotes(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        if callable(self.response):
            return self.response(kwargs)
        return self.response


def contract(market_snapshot, token="nse_fo|40611"):
    return market_snapshot.options[0].model_copy(update={"instrument_token": token})


def test_depth_preserves_chain_fields_and_persists_raw_quantities(market_snapshot, repository):
    item = contract(market_snapshot)
    original = item.model_dump()
    response = [quote()]
    original_response = deepcopy(response)
    client = Client(response)
    result = KotakMarketDataAdapter(client).enrich_option_quotes([item])[0]
    assert client.calls == [{"quote_type": "all", "instrument_tokens": [
        {"exchange_segment": "nse_fo", "instrument_token": "40611"}]}]
    assert (result.bid, result.ask, result.bid_quantity, result.ask_quantity) == (100.1, 100.2, 75, 150)
    assert result.depth_unit == "UNITS"
    assert result.source_market_timestamp.timestamp() == 1782374657 and result.tick_size is None
    changed = {"bid", "ask", "bid_quantity", "ask_quantity", "depth_unit", "source_market_timestamp"}
    assert {k: v for k, v in result.model_dump().items() if k not in changed} == {
        k: v for k, v in original.items() if k not in changed}
    assert item.model_dump() == original and response == original_response
    raw = market_snapshot.model_copy(update={"options": [result]})
    saved = repository.save_market_snapshot(raw, raw.timestamp_ist)
    stored = repository.get(saved.snapshot_id)["options"][0]
    assert (stored["bid"], stored["ask"]) == (Decimal("100.1"), Decimal("100.2"))
    assert (stored["bid_quantity"], stored["ask_quantity"], stored["depth_unit"]) == (75, 150, "UNITS")
    # SQLite retains the wall-clock components but not the offset; PostgreSQL
    # uses the existing timezone-aware column. No collector timestamp is substituted.
    assert stored["source_market_timestamp"] == result.source_market_timestamp.replace(tzinfo=None)
    assert stored["tick_size"] is None


@pytest.mark.parametrize("value", ["1757915078", 1757915078])
def test_documented_unix_seconds_are_timezone_aware(value):
    result = parse_quote_timestamp(value)
    assert result.tzinfo is not None
    assert result.astimezone(timezone.utc) == datetime.fromtimestamp(1757915078, timezone.utc)
    assert str(result.tzinfo) == "Asia/Kolkata"
    assert result.timestamp() == 1757915078


@pytest.mark.parametrize("value", [None, "", "0", 0, -1, "-2209008600", True,
                                    "1757915078000", "1757915078.5", "NaN", "secret", {},
                                    "2026-10-07T10:00:00+05:30", 1757915078.5])
def test_missing_invalid_and_wrong_encoding_timestamp_stays_null(value):
    assert parse_quote_timestamp(value) is None


def test_missing_timestamp_not_replaced_with_collection_time(market_snapshot):
    result = KotakMarketDataAdapter(Client([quote(lstup_time=None)])).enrich_option_quotes(
        [contract(market_snapshot)])[0]
    assert result.bid is not None and result.source_market_timestamp is None


def test_batch_limit_exact_identity_and_out_of_order_responses(market_snapshot, monkeypatch):
    monkeypatch.setattr("app.broker.kotak.market_data.sleep", lambda _: None)
    items = [contract(market_snapshot, f"nse_fo|{i}") for i in range(101)]
    client = Client(lambda args: [quote(row["instrument_token"]) for row in reversed(args["instrument_tokens"])])
    results = KotakMarketDataAdapter(client).enrich_option_quotes(items)
    assert [len(call["instrument_tokens"]) for call in client.calls] == [50, 50, 1]
    assert len(results) == 101 and all(item.bid == 100.1 for item in results)


def test_optional_batch_budget_keeps_remaining_chain_rows(market_snapshot, monkeypatch):
    clock = iter([0, 11, 11])
    monkeypatch.setattr("app.broker.kotak.market_data.monotonic", lambda: next(clock))
    client = Client(lambda args: [quote(row["instrument_token"]) for row in args["instrument_tokens"]])
    items = [contract(market_snapshot, str(i)) for i in range(51)]
    results = KotakMarketDataAdapter(client).enrich_option_quotes(items)
    assert len(client.calls) == 1 and len(results) == 51
    assert results[0].bid == 100.1 and results[-1] == items[-1]


def test_a_failed_later_observation_never_reuses_previous_depth(market_snapshot):
    client = Client([quote()])
    broker = KotakMarketDataAdapter(client)
    chain = [contract(market_snapshot)]
    assert broker.enrich_option_quotes(chain)[0].bid == 100.1
    client.response = {"error": "unavailable"}
    assert broker.enrich_option_quotes(chain)[0].bid is None


@pytest.mark.parametrize("response", [None, {}, [], {"error": "secret-token"},
    RuntimeError("secret-token"), [quote("other")], [quote(exchange="bse_fo")],
    [quote(), quote()], [{"depth": quote()["depth"]}]])
def test_missing_failed_mismatched_or_duplicate_quotes_stay_null(response, market_snapshot, caplog):
    item = contract(market_snapshot)
    result = KotakMarketDataAdapter(Client(response)).enrich_option_quotes([item])[0]
    assert result == item
    assert result.bid is None and result.ask_quantity is None
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("depth,expected", [
    (None, (None, None, None, None)),
    ({"buy": [], "sell": None}, (None, None, None, None)),
    ({"buy": [{"price": "100.10", "quantity": "0"}]}, (100.1, None, 0, None)),
    ({"buy": [{"price": "NaN", "quantity": "1.5"}],
      "sell": [{"price": "-2", "quantity": "-1"}]}, (None, None, None, None)),
    ({"buy": [{"price": True, "quantity": True}]}, (None, None, None, None)),
    ({"buy": [{"price": "0", "quantity": "20"}]}, (None, None, 20, None)),
])
def test_partial_or_malformed_depth_is_not_fabricated(depth, expected, market_snapshot):
    result = KotakMarketDataAdapter(Client([quote(depth=depth)])).enrich_option_quotes(
        [contract(market_snapshot)])[0]
    assert (result.bid, result.ask, result.bid_quantity, result.ask_quantity) == expected
    assert result.depth_unit == "UNITS"


def test_token_formats_do_not_guess_identity(market_snapshot):
    assert quote_identity(contract(market_snapshot, "40611")) == ("nse_fo", "40611")
    for token in (None, "", "bse_fo|40611", "nse_fo|", "nse_fo|40611|other"):
        assert quote_identity(contract(market_snapshot, token)) is None


def test_snapshot_builder_quotes_only_filtered_contracts(market_snapshot, monkeypatch):
    client = Client(lambda args: {"data": [quote(row["instrument_token"])
                                          for row in args["instrument_tokens"]]})
    broker = KotakMarketDataAdapter(client)
    monkeypatch.setattr(broker, "get_nifty_spot", lambda: market_snapshot.nifty_spot)
    monkeypatch.setattr(broker, "get_nifty_expiries", lambda: [market_snapshot.expiry])
    monkeypatch.setattr(broker, "get_nifty_option_chain", lambda _: market_snapshot.options)
    monkeypatch.setattr(broker, "get_india_vix", lambda: None)
    result = build_market_snapshot(broker, strikes_each_side=1)
    assert len(result.options) == 6
    assert len(client.calls) == 1 and len(client.calls[0]["instrument_tokens"]) == 6
    assert {item.strike for item in result.options} == {24950, 25000, 25050}
    assert all(item.bid == 100.1 for item in result.options)
