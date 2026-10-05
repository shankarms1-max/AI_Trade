import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def diagnostic():
    path = Path(__file__).resolve().parents[2] / "scripts/diagnose_kotak_option_quotes.py"
    spec = importlib.util.spec_from_file_location("quote_diagnostic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(token="40611"):
    return {"exchange": "nse_fo", "exchange_token": token,
            "display_symbol": "NIFTY99OCT25000CE", "lstup_time": "1782374657",
            "depth": {"buy": [{"price": "100.10", "quantity": "75"}],
                      "sell": [{"price": "100.20", "quantity": "150"}]}}


def test_raw_timestamp_and_quantities_are_not_converted(diagnostic):
    data = row()
    data.update({"last_update_time": "1782374657000", "market_lot": "75",
                 "meta": {"quantity_unit": "CONTRACTS", "exchange_timestamp": "2026-10-07T10:00:00+05:30"}})
    result = diagnostic.summarize_quote(data)
    assert result["lstup_time"] == "1782374657"
    assert result["other_timestamps"] == {"last_update_time": "1782374657000",
                                         "meta.exchange_timestamp": "2026-10-07T10:00:00+05:30"}
    assert result["quantity_metadata"] == {"market_lot": "75", "meta.quantity_unit": "CONTRACTS"}
    assert result["best_bid_quantity"] == "75" and result["best_ask_price"] == "100.20"
    assert "source_market_timestamp" not in result and "depth_unit" not in result


def test_sensitive_and_unknown_objects_never_escape(diagnostic):
    data = row()
    data.update({"consumer_key": "SECRET", "session_timestamp": "SECRET", "sid": "SECRET",
                 "auth": {"timestamp": "SECRET"}, "other": {"session_token": "SECRET"},
                 "last_update_time": {"consumer_key": "SECRET"},
                 "market_lot": "SECRET", "lstup_time": "SECRET"})
    data["depth"]["buy"][0].update({"quantity": {"password": "SECRET"}, "auth": "SECRET"})
    result = diagnostic.summarize_quote(data)
    rendered = json.dumps(result)
    assert "SECRET" not in rendered and "consumer_key" not in rendered and "session_timestamp" not in rendered
    assert result["lstup_time"] == "<redacted>"
    assert result["other_timestamps"]["last_update_time"] == "<redacted>"


def test_cli_is_one_batch_quiet_and_closes_transport(diagnostic, capsys, monkeypatch):
    calls, closed = [], []
    class Client:
        api_client = SimpleNamespace(rest_client=SimpleNamespace(close=lambda: closed.append(True)))
        def quotes(self, **kwargs):
            calls.append(kwargs)
            print("SECRET noisy sdk")
            return [row("40612"), row()]
    monkeypatch.setenv("NEO_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("NEO_LOG_FILE_ENABLED", "true")
    assert diagnostic.main(["--instrument-token", "40611", "--instrument-token", "40612"],
                           client_factory=Client) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "SECRET" not in captured.out
    assert [r["exchange_token"] for r in json.loads(captured.out)] == ["40611", "40612"]
    assert calls == [{"quote_type": "all", "instrument_tokens": [
        {"exchange_segment": "nse_fo", "instrument_token": "40611"},
        {"exchange_segment": "nse_fo", "instrument_token": "40612"}]}]
    assert closed == [True]


def test_cli_failure_does_not_print_exception_or_credentials(diagnostic, capsys):
    def failed():
        print("SECRET")
        raise RuntimeError("postgresql://SECRET@host/db")
    assert diagnostic.main([], client_factory=failed) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "SECRET" not in captured.err and "failed" in captured.err


@pytest.mark.parametrize("tokens", [["1", "2", "3"], ["1", "1"], ["session-secret"], []])
def test_reject_invalid_identifiers_without_quote_request(diagnostic, tokens):
    class Client:
        def quotes(self, **kwargs):
            pytest.fail("invalid identifiers must not reach quote API")
    with pytest.raises(ValueError):
        diagnostic.probe(Client(), tokens)


@pytest.mark.parametrize("response", [[row(), row()], [], {"error": "SECRET"}, [row("999")]])
def test_ambiguous_missing_or_error_response_fails(diagnostic, response):
    client = SimpleNamespace(quotes=lambda **kwargs: response)
    with pytest.raises((ValueError, RuntimeError)):
        diagnostic.probe(client, ["40611"])


def test_default_probe_resolves_exact_atm_nifty_pair(diagnostic):
    calls = []
    class Client:
        def quotes(self, **kwargs):
            calls.append(kwargs)
            if kwargs["quote_type"] == "ltp":
                return [{"ltp": "25000"}]
            return [row(), row("40612")]
        def expiries(self, **kwargs):
            assert kwargs["underlying"] == "NIFTY"
            return {"expiries": ["2099-10-08"]}
        def option_chain(self, **kwargs):
            assert kwargs["underlying"] == "NIFTY"
            def contract(token, kind):
                return {"inst": {"neoSymbol": f"nse_fo|{token}", "symbol": f"NIFTY99OCT25000{kind}",
                                  "optType": kind, "strkPrc": "25000", "exp": "2099-10-08"},
                        "quote": {"ltp": "100"}, "oi": {}}
            return {"common_data": {"mktLot": "75"}, "call": [contract("40611", "CE")],
                    "put": [contract("40612", "PE")]}
    result = diagnostic.probe(Client())
    assert len(result) == 2
    assert len([call for call in calls if call["quote_type"] == "all"]) == 1
    assert calls[-1]["instrument_tokens"] == [
        {"exchange_segment": "nse_fo", "instrument_token": "40611"},
        {"exchange_segment": "nse_fo", "instrument_token": "40612"}]
