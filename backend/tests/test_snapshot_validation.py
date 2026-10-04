from datetime import date, datetime
import logging
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.broker.kotak.market_data import (
    KotakMarketDataAdapter,
    option_chain_request_count,
)
from app.broker.kotak.option_chain import KotakOptionChainError
from app.data.models import MarketSnapshot, OptionContractSnapshot
from app.data.snapshot import calculate_atm_strike, filter_contracts_around_atm

EXPIRY = date(2099, 10, 8)
NOW = datetime(2099, 10, 1, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))


def live_option_response(future: object = ...) -> dict[str, object]:
    response: dict[str, object] = {
        "common_data": {},
        "call": [
            {
                "inst": {
                    "neoSymbol": "nse_fo|40611",
                    "symbol": "NIFTY99OCT25000CE",
                    "optType": "CE",
                    "strkPrc": "25000.00",
                    "exp": EXPIRY.isoformat(),
                },
                "quote": {"ltp": "100.50", "vol": "20"},
                "oi": {"cur": "120", "prev": "100", "chg": "20"},
            }
        ],
        "put": [],
    }
    if future is ...:
        response["future"] = {
            "exchId": "nse_fo",
            "symbol": "NIFTY99OCTFUT",
            "ltp": "25123.45",
            "prevClose": "25000.00",
            "expiry": "2099-10-31",
        }
    elif future is not None:
        response["future"] = future
    return response


def option(strike: float, kind: str = "CE", ltp: float | None = 10.0) -> OptionContractSnapshot:
    return OptionContractSnapshot(
        strike=strike,
        option_type=kind,
        expiry=EXPIRY,
        trading_symbol=f"NIFTY-{strike}-{kind}",
        ltp=ltp,
    )


def test_snapshot_rejects_missing_nifty_spot() -> None:
    with pytest.raises(ValidationError, match="nifty_spot"):
        MarketSnapshot(
            timestamp_ist=NOW,
            nifty_spot=None,
            atm_strike=25000,
            expiry=EXPIRY,
            options=[option(25000)],
        )


def test_snapshot_rejects_empty_option_chain() -> None:
    with pytest.raises(ValidationError, match="option chain is empty"):
        MarketSnapshot(
            timestamp_ist=NOW,
            nifty_spot=25000,
            atm_strike=25000,
            expiry=EXPIRY,
            options=[],
        )


def test_snapshot_rejects_chain_when_all_ltps_are_missing() -> None:
    with pytest.raises(ValidationError, match="all option LTP values are missing"):
        MarketSnapshot(
            timestamp_ist=NOW,
            nifty_spot=25000,
            atm_strike=25000,
            expiry=EXPIRY,
            options=[option(25000, ltp=None)],
        )


@pytest.mark.parametrize(
    ("spot", "expected"),
    [(25024.99, 25000), (25025, 25050), (25076, 25100)],
)
def test_atm_strike_calculation_uses_half_up_rounding(spot: float, expected: float) -> None:
    assert calculate_atm_strike(spot, 50) == expected


def test_strike_filtering_keeps_atm_plus_minus_n_distinct_strikes() -> None:
    contracts = [
        option(strike, kind)
        for strike in range(24750, 25300, 50)
        for kind in ("CE", "PE")
    ]
    filtered = filter_contracts_around_atm(contracts, 25000, 2)
    assert sorted({item.strike for item in filtered}) == [24900, 24950, 25000, 25050, 25100]
    assert len(filtered) == 10


def test_phase_one_adapter_has_no_order_methods() -> None:
    forbidden = ("place_order", "modify_order", "cancel_order", "square_off")
    assert all(not hasattr(KotakMarketDataAdapter, name) for name in forbidden)


def test_kotak_quote_sdk_envelope_is_supported() -> None:
    class FakeClient:
        def quotes(self, **_: object) -> dict[str, object]:
            return {"stat": "Ok", "data": [{"ltp": "25012.50"}]}

    adapter = KotakMarketDataAdapter(FakeClient())  # type: ignore[arg-type]
    assert adapter.get_nifty_spot() == 25012.5


def test_default_option_chain_call_matches_current_sdk_contract() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.arguments: dict[str, object] = {}

        def option_chain(self, **kwargs: object) -> dict[str, object]:
            self.arguments = kwargs
            return {
                "stat": "Not_Ok",
                "stCode": 400,
                "errMsg": "test validation response",
                "desc": "test only",
            }

    client = FakeClient()
    adapter = KotakMarketDataAdapter(client, strike_range=10)  # type: ignore[arg-type]
    with pytest.raises(KotakOptionChainError) as captured:
        adapter.get_nifty_option_chain(EXPIRY)

    assert client.arguments == {
        "exchange": "nse_fo",
        "underlying": "NIFTY",
        "expiry": "2099-10-08",
        "instrument_type": "option",
        "count": 30,
    }
    assert captured.value.response is not None


@pytest.mark.parametrize("strike_range", [0, 1, 10, 14, 50])
def test_option_chain_count_satisfies_documented_multiple_of_ten(
    strike_range: int,
) -> None:
    count = option_chain_request_count(strike_range)
    assert count >= (strike_range * 2) + 1
    assert count % 10 == 0


def test_future_ltp_comes_from_already_fetched_option_chain() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.option_chain_calls = 0

        def option_chain(self, **_: object) -> dict[str, object]:
            self.option_chain_calls += 1
            return live_option_response()

    client = FakeClient()
    adapter = KotakMarketDataAdapter(client)  # type: ignore[arg-type]
    adapter.get_nifty_option_chain(EXPIRY)

    assert adapter.get_nifty_future() == 25123.45
    assert client.option_chain_calls == 1


def test_missing_future_stays_none_without_synthetic_value() -> None:
    class FakeClient:
        def option_chain(self, **_: object) -> dict[str, object]:
            return live_option_response(future=None)

    adapter = KotakMarketDataAdapter(FakeClient())  # type: ignore[arg-type]
    adapter.get_nifty_option_chain(EXPIRY)
    assert adapter.get_nifty_future() is None


@pytest.mark.parametrize("future_expiry", ["invalid", "2000-01-01", None])
def test_future_expiry_must_be_valid_and_unexpired(future_expiry: object) -> None:
    class FakeClient:
        def option_chain(self, **_: object) -> dict[str, object]:
            return live_option_response(
                future={
                    "symbol": "NIFTY99OCTFUT",
                    "ltp": "25123.45",
                    "expiry": future_expiry,
                }
            )

    adapter = KotakMarketDataAdapter(FakeClient())  # type: ignore[arg-type]
    adapter.get_nifty_option_chain(EXPIRY)
    assert adapter.get_nifty_future() is None


def test_exact_india_vix_identifier_is_quoted_and_parsed() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.quote_calls: list[list[dict[str, str]]] = []

        def quotes(self, **kwargs: object) -> dict[str, object]:
            tokens = kwargs["instrument_tokens"]
            self.quote_calls.append(tokens)  # type: ignore[arg-type]
            return {"stat": "Ok", "data": [{"ltp": "13.75"}]}

    client = FakeClient()
    adapter = KotakMarketDataAdapter(client)  # type: ignore[arg-type]

    assert adapter.get_india_vix() == 13.75
    assert len(client.quote_calls) == 1
    assert client.quote_calls[0] == [
        {"instrument_token": "INDIA VIX", "exchange_segment": "nse_cm"}
    ]


def test_failed_india_vix_quote_leaves_value_null() -> None:
    class FakeClient:
        def quotes(self, **_: object) -> dict[str, object]:
            return {"stat": "Not_Ok", "errMsg": "Invalid neosymbol values"}

    client = FakeClient()
    adapter = KotakMarketDataAdapter(client)  # type: ignore[arg-type]
    assert adapter.get_india_vix() is None


def test_missing_india_vix_ltp_is_handled_without_synthetic_value() -> None:
    class FakeClient:
        def quotes(self, **_: object) -> list[dict[str, object]]:
            return []

    adapter = KotakMarketDataAdapter(FakeClient())  # type: ignore[arg-type]
    assert adapter.get_india_vix() is None


def test_india_vix_failure_does_not_log_credentials(caplog: pytest.LogCaptureFixture) -> None:
    class FakeClient:
        def quotes(self, **_: object) -> dict[str, object]:
            return {
                "stat": "Not_Ok",
                "errMsg": "consumer_key=do-not-log token=do-not-log-either",
            }

    with caplog.at_level(logging.WARNING):
        assert KotakMarketDataAdapter(FakeClient()).get_india_vix() is None  # type: ignore[arg-type]
    assert "do-not-log" not in caplog.text
    assert "do-not-log-either" not in caplog.text

