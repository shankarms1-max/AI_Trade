from datetime import date, datetime
import json
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.broker.kotak.option_chain import (
    KotakOptionChainError,
    extract_option_chain_payload,
    normalize_option_chain,
    option_chain_data_quality,
    summarize_contract_structure,
    summarize_option_chain_response,
    summarize_option_chain_structure,
)
from app.data.models import MarketSnapshot, OptionContractSnapshot, OptionType
from app.data.snapshot import filter_contracts_around_atm

EXPIRY = date(2099, 10, 8)


def raw_contract(option_type: str = "CE") -> dict[str, object]:
    return {
        "instrument": {
            "neoSymbol": f"nse_fo|10{1 if option_type == 'CE' else 2}",
            "symbol": f"NIFTY99OCT25000{option_type}",
            "strikePrice": "25000",
        },
        "quote": {"ltp": "101.25", "volume": 500},
        "openInterest": {"current": 1000, "previous": 900, "change": 100},
    }


def live_contract(
    option_type: str = "CE",
    *,
    strike: object = "25000.00",
    expiry: object = "2099-10-08",
    ltp: object = "3277.05",
    volume: object = "456789",
    current_oi: object = "123456",
    previous_oi: object = "120000",
    oi_change: object = "3456",
) -> dict[str, object]:
    return {
        "inst": {
            "neoSymbol": "nse_fo|40611" if option_type in {"CE", "CALL", "C"} else "nse_fo|40612",
            "symbol": f"NIFTY99OCT25000{option_type}",
            "optType": option_type,
            "strkPrc": strike,
            "exp": expiry,
            "moneyness": "ATM",
        },
        "quote": {
            "ltp": ltp,
            "o": "3200.00",
            "h": "3300.00",
            "l": "3100.00",
            "c": "3277.05",
            "pc": "3000.00",
            "vol": volume,
        },
        "oi": {
            "cur": current_oi,
            "prev": previous_oi,
            "chg": oi_change,
            "chgPct": "2.88",
        },
    }


def contract(**overrides: object) -> OptionContractSnapshot:
    values: dict[str, object] = {
        "strike": 25000,
        "option_type": "CE",
        "expiry": EXPIRY,
        "trading_symbol": "NIFTY99OCT25000CE",
        "ltp": 100.5,
    }
    values.update(overrides)
    return OptionContractSnapshot(**values)


def test_market_snapshot_accepts_valid_data_and_serializes_json() -> None:
    snapshot = MarketSnapshot(
        timestamp_ist=datetime(2099, 10, 1, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata")),
        nifty_spot=25012.5,
        nifty_future=25040.0,
        india_vix=13.2,
        atm_strike=25000,
        expiry=EXPIRY,
        options=[contract()],
    )
    payload = snapshot.model_dump_json()
    assert '"nifty_spot":25012.5' in payload
    assert '"expiry":"2099-10-08"' in payload


def test_optional_unavailable_fields_can_be_none() -> None:
    item = contract(ltp=None)
    assert item.implied_volatility is None
    assert item.bid is None
    assert item.delta is None


def test_invalid_option_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="option_type"):
        contract(option_type="CALL")


def test_ce_and_pe_data_normalize_correctly() -> None:
    response = {
        "data": {
            "common_data": {"unlSymbol": "NIFTY"},
            "call": [
                {
                    "instrument": {
                        "neoSymbol": "nse_fo|101",
                        "symbol": "NIFTY99OCT25000CE",
                        "strikePrice": "25000",
                    },
                    "quote": {"ltp": "101.25", "volume": 500},
                    "openInterest": {"current": 1000, "previous": 900, "change": 100},
                }
            ],
            "put": [
                {
                    "instrument": {
                        "neoSymbol": "nse_fo|102",
                        "symbol": "NIFTY99OCT25000PE",
                        "strikePrice": "25000",
                    },
                    "quote": {"ltp": "88.50", "volume": 700},
                    "openInterest": {"current": 1200, "previous": 1300, "change": -100},
                }
            ],
        }
    }
    items = normalize_option_chain(response, EXPIRY)
    assert [item.option_type for item in items] == [OptionType.CE, OptionType.PE]
    assert items[0].instrument_token == "101"
    assert items[1].ltp == 88.5
    assert items[1].change_in_open_interest == -100


def test_documented_wrapped_option_chain_payload_is_accepted() -> None:
    payload = {"common_data": {}, "call": [raw_contract()], "put": []}
    response = {"data": payload}
    assert extract_option_chain_payload(response) is payload


def test_observed_live_top_level_option_chain_payload_is_accepted() -> None:
    response = {
        "common_data": {"unlSymbol": "NIFTY"},
        "call": [live_contract("CE")],
        "put": [live_contract("PE")],
        "future_contracts": [],
        "spot": {},
        "future": {},
    }
    assert extract_option_chain_payload(response) is response
    items = normalize_option_chain(response, EXPIRY)
    assert [item.option_type for item in items] == [OptionType.CE, OptionType.PE]


def test_live_nested_call_record_normalizes_exact_confirmed_fields() -> None:
    response = {"common_data": {}, "call": [live_contract("CE")], "put": []}
    item = normalize_option_chain(response, EXPIRY)[0]

    assert item.strike == 25000.0
    assert item.option_type == OptionType.CE
    assert item.expiry == EXPIRY
    assert item.trading_symbol == "NIFTY99OCT25000CE"
    assert item.instrument_token == "nse_fo|40611"
    assert item.ltp == 3277.05
    assert item.open_interest == 123456
    assert item.previous_open_interest == 120000
    assert item.change_in_open_interest == 3456
    assert item.volume == 456789
    assert item.implied_volatility is None
    assert item.bid is None
    assert item.ask is None
    assert item.delta is None
    assert item.gamma is None
    assert item.theta is None
    assert item.vega is None


def test_live_nested_put_record_normalizes() -> None:
    response = {"common_data": {}, "call": [], "put": [live_contract("PE")]}
    item = normalize_option_chain(response, EXPIRY)[0]
    assert item.option_type == OptionType.PE
    assert item.instrument_token == "nse_fo|40612"


@pytest.mark.parametrize("strike", [None, "", "-", "not-a-number", "0", 0])
def test_live_contract_with_malformed_strike_is_rejected(strike: object) -> None:
    response = {"common_data": {}, "call": [live_contract(strike=strike)]}
    with pytest.raises(KotakOptionChainError, match="invalid strike"):
        normalize_option_chain(response, EXPIRY)


@pytest.mark.parametrize("ltp", [None, "", "-", "not-a-number"])
def test_live_contract_with_unavailable_or_malformed_ltp_becomes_none(ltp: object) -> None:
    response = {"common_data": {}, "call": [live_contract(ltp=ltp)]}
    assert normalize_option_chain(response, EXPIRY)[0].ltp is None


def test_live_contract_option_type_must_match_container() -> None:
    response = {"common_data": {}, "call": [live_contract("PE")]}
    with pytest.raises(KotakOptionChainError, match="contradicts its container"):
        normalize_option_chain(response, EXPIRY)


def test_live_contract_expiry_must_match_requested_expiry() -> None:
    response = {
        "common_data": {},
        "call": [live_contract(expiry="2099-10-15")],
    }
    with pytest.raises(KotakOptionChainError, match="expiry does not match"):
        normalize_option_chain(response, EXPIRY)


def test_broker_oi_change_mismatch_is_preserved() -> None:
    response = {
        "common_data": {},
        "call": [live_contract(current_oi="100", previous_oi="80", oi_change="99")],
    }
    item = normalize_option_chain(response, EXPIRY)[0]
    assert item.change_in_open_interest == 99
    assert option_chain_data_quality([item])["oi_mismatch"] == 1


def test_option_chain_data_quality_counters_do_not_mutate_oi() -> None:
    contracts = [
        contract(
            open_interest=120,
            previous_open_interest=100,
            change_in_open_interest=20,
            volume=50,
        ),
        contract(
            option_type="PE",
            open_interest=90,
            previous_open_interest=100,
            change_in_open_interest=5,
            volume=0,
        ),
        contract(
            strike=25050,
            open_interest=100,
            previous_open_interest=100,
            change_in_open_interest=0,
            volume=None,
        ),
    ]
    quality = option_chain_data_quality(contracts)

    assert quality == {
        "contracts": 3,
        "nonzero_volume": 1,
        "nonzero_oi_change": 2,
        "oi_current_prev_different": 2,
        "oi_mismatch": 1,
    }
    assert contracts[1].change_in_open_interest == 5


@pytest.mark.parametrize(("broker_value", "expected"), [("CE", "CE"), ("CALL", "CE"), ("C", "CE"), ("PE", "PE"), ("PUT", "PE"), ("P", "PE")])
def test_only_recognized_option_type_values_are_normalized(
    broker_value: str, expected: str
) -> None:
    side = "call" if expected == "CE" else "put"
    response = {"common_data": {}, side: [live_contract(broker_value)]}
    assert normalize_option_chain(response, EXPIRY)[0].option_type.value == expected


def test_atm_filtering_is_deterministic_for_61_calls_and_61_puts() -> None:
    strikes = [23500 + (index * 50) for index in range(61)]
    response = {
        "common_data": {},
        "call": [live_contract("CE", strike=str(strike)) for strike in reversed(strikes)],
        "put": [live_contract("PE", strike=str(strike)) for strike in strikes],
    }
    normalized = normalize_option_chain(response, EXPIRY)
    filtered = filter_contracts_around_atm(normalized, 25000, 10)

    assert len(normalized) == 122
    assert len(filtered) == 42
    assert min(item.strike for item in filtered) == 24500
    assert max(item.strike for item in filtered) == 25500


def test_empty_top_level_option_chain_response_is_rejected() -> None:
    with pytest.raises(KotakOptionChainError, match="neither a documented wrapped"):
        extract_option_chain_payload({})


def test_option_chain_error_response_is_rejected_and_preserved() -> None:
    response = {
        "stat": "Not_Ok",
        "stCode": 400,
        "errMsg": "invalid count",
        "desc": "validation failed",
    }
    with pytest.raises(KotakOptionChainError) as captured:
        extract_option_chain_payload(response)
    assert captured.value.response is response
    assert captured.value.stCode == 400


def test_call_only_option_chain_is_accepted_when_valid() -> None:
    response = {"common_data": [], "call": [raw_contract("CE")]}
    items = normalize_option_chain(response, EXPIRY)
    assert len(items) == 1
    assert items[0].option_type == OptionType.CE


def test_put_only_option_chain_is_accepted_when_valid() -> None:
    response = {"common_data": {}, "put": [raw_contract("PE")]}
    items = normalize_option_chain(response, EXPIRY)
    assert len(items) == 1
    assert items[0].option_type == OptionType.PE


@pytest.mark.parametrize(("field", "value"), [("call", {}), ("put", "invalid")])
def test_malformed_call_or_put_type_is_rejected(field: str, value: object) -> None:
    response = {"common_data": {}, field: value}
    with pytest.raises(KotakOptionChainError, match=f"{field} field is not a list"):
        extract_option_chain_payload(response)


def test_option_chain_with_empty_call_and_put_is_rejected() -> None:
    response = {"common_data": {}, "call": [], "put": []}
    with pytest.raises(KotakOptionChainError, match="contains no call or put contracts"):
        extract_option_chain_payload(response)


def test_option_chain_with_malformed_common_data_is_rejected() -> None:
    response = {"common_data": "invalid", "call": [raw_contract()]}
    with pytest.raises(KotakOptionChainError, match="common_data is invalid"):
        extract_option_chain_payload(response)


def test_oi_change_is_not_derived_when_broker_omits_it() -> None:
    item = contract(
        open_interest=1_250,
        previous_open_interest=1_000,
        change_in_open_interest=None,
    )
    assert item.change_in_open_interest is None


def test_option_chain_diagnostic_is_allowlisted_and_redacted() -> None:
    response = {
        "stat": "Not_Ok",
        "stCode": 400,
        "errMsg": "invalid token=do-not-print mobile=919999999999",
        "desc": "validation failed",
        "error": {
            "access_token": "do-not-print-either",
            "reason": "consumer_key=also-secret",
        },
        "consumer_key": "top-level-secret",
    }
    summary = summarize_option_chain_response(
        response,
        expiry="2099-10-08",
        exchange="nse_fo",
        underlying="NIFTY",
        instrument_type="option",
        count=30,
    )
    rendered = json.dumps(summary)

    assert set(summary) == {
        "response_type",
        "top_level_keys",
        "stat",
        "stCode",
        "errMsg",
        "desc",
        "error",
        "data_present",
        "data_keys",
        "expiry",
        "exchange",
        "underlying",
        "instrument_type",
        "count",
    }
    assert summary["response_type"] == "dict"
    assert summary["data_present"] is False
    assert summary["data_keys"] is None
    assert "do-not-print" not in rendered
    assert "919999999999" not in rendered
    assert "top-level-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_option_chain_error_string_never_contains_preserved_response_secret() -> None:
    response = {"consumer_key": "never-log-this"}
    error = KotakOptionChainError(
        stat="Not_Ok",
        stCode=400,
        errMsg="invalid request",
        desc="response has no data object",
        response=response,
    )
    assert error.response is response
    assert "never-log-this" not in str(error)
    assert "stCode=400" in str(error)


def test_contract_structure_summary_does_not_leak_sensitive_or_unknown_values() -> None:
    item = {
        "consumer_key": "consumer-secret",
        "session_token": "session-secret",
        "unknown": "unknown-secret",
        "token": "safe-contract-token",
        "quote": {
            "ltp": "101.25",
            "volume": 500,
            "auth_token": "nested-session-secret",
            "unrecognized": "nested-unknown-secret",
        },
    }
    summary = summarize_contract_structure(item)
    rendered = json.dumps(summary)

    assert "consumer-secret" not in rendered
    assert "session-secret" not in rendered
    assert "unknown-secret" not in rendered
    assert "nested-session-secret" not in rendered
    assert "nested-unknown-secret" not in rendered
    assert summary["safe_sample"]["unknown"] == "<str>"
    assert summary["safe_sample"]["token"] == "safe-contract-token"
    assert summary["nested_safe_samples"]["quote"]["ltp"] == "101.25"
    assert summary["nested_safe_samples"]["quote"]["volume"] == 500
    assert "[REDACTED]" in rendered


def test_nested_contract_keys_are_summarized_without_dumping_values() -> None:
    item = {
        "instrument": {
            "neoSymbol": "nse_fo|12345",
            "symbol": "must-not-be-dumped",
            "strikePrice": "25000",
        },
        "quote": {"ltp": "100.50", "depth": {"private": "value"}},
    }
    summary = summarize_contract_structure(item)

    assert summary["nested_dicts"]["instrument"]["keys"] == [
        "neoSymbol",
        "symbol",
        "strikePrice",
    ]
    assert summary["nested_dicts"]["instrument"]["value_types"] == {
        "neoSymbol": "str",
        "symbol": "str",
        "strikePrice": "str",
    }
    assert summary["nested_safe_samples"]["instrument"] == {
        "neoSymbol": "nse_fo|12345",
        "symbol": "<str>",
        "strikePrice": "25000",
    }
    assert summary["nested_safe_samples"]["quote"]["depth"] == "<dict>"
    assert "must-not-be-dumped" not in json.dumps(summary)
    assert '"private": "value"' not in json.dumps(summary)


@pytest.mark.parametrize(
    ("item", "expected_type"),
    [(None, "NoneType"), ([], "list"), ({}, "dict")],
)
def test_contract_structure_handles_none_list_and_dict_shapes(
    item: object, expected_type: str
) -> None:
    summary = summarize_contract_structure(item)
    assert summary["type"] == expected_type
    json.dumps(summary)


def test_option_chain_structure_summarizes_all_requested_sections() -> None:
    payload = {
        "common_data": {"expiry": "2099-10-08", "lot": 65},
        "spot": {"ltp": "25000.0", "name": "not-a-sample-value"},
        "future": None,
        "call": [raw_contract("CE")],
        "put": [raw_contract("PE")],
    }
    summary = summarize_option_chain_structure(payload)

    assert summary["payload_type"] == "dict"
    assert summary["common_data"]["type"] == "dict"
    assert summary["common_data"]["value_types"] == {"expiry": "str", "lot": "int"}
    assert summary["spot"]["value_types"] == {"ltp": "str", "name": "str"}
    assert summary["future"]["type"] == "NoneType"
    assert summary["call"]["type"] == "list"
    assert summary["call"]["length"] == 1
    assert summary["put"]["length"] == 1

