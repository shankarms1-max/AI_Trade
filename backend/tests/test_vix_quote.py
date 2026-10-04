import json

from app.broker.kotak.vix import (
    INDIA_VIX_IDENTIFIER,
    parse_vix_ltp,
    summarize_vix_quote,
)


def test_exact_india_vix_identifier() -> None:
    assert INDIA_VIX_IDENTIFIER == "INDIA VIX"


def test_successful_vix_numeric_parsing() -> None:
    response = {
        "stat": "Ok",
        "data": [
            {
                "instrument_token": "INDIA VIX",
                "trading_symbol": "INDIA VIX",
                "ltp": "13.825",
            }
        ],
    }
    assert parse_vix_ltp(response) == 13.825
    summary = summarize_vix_quote(response, method="REST")
    assert summary["success"] is True
    assert summary["returned_token"] == "INDIA VIX"


def test_failed_vix_response_returns_none() -> None:
    assert parse_vix_ltp({"stat": "Not_Ok", "errMsg": "Invalid neosymbol"}) is None


def test_vix_diagnostic_redacts_credentials() -> None:
    response = {
        "errMsg": "consumer_key=do-not-print token words are harmless",
        "authorization": "secret-header",
    }
    rendered = json.dumps(summarize_vix_quote(response, method="REST"))
    assert "do-not-print" not in rendered
    assert "secret-header" not in rendered
    assert "[REDACTED]" in rendered
