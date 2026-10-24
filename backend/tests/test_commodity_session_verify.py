"""Monitor assertions use synthetic API responses and make no network calls."""

from copy import deepcopy
from datetime import timedelta

from tests.test_commodity_acts import AT
from worker.commodity_session_verify import inspect_observations


def data():
    research = {"instruments": []}
    paper = {"instruments": []}
    for instrument in ("CRUDEOILM", "NATGASMINI"):
        identity = {"future_token": instrument + "_F", "future_expiry": "2099-10-19", "option_expiry": "2099-10-15"}
        research["instruments"].append(
            {
                "instrument_id": instrument,
                "observation_count": 1,
                "decision_count": 1,
                "readiness": "ALPHA_WARMING_UP",
                "latest": {
                    "observed_at": AT.isoformat(),
                    **identity,
                    "alpha": {"validity_state": "INVALID", "warnings": ["INSUFFICIENT_ALPHA_HISTORY"]},
                },
            }
        )
        paper["instruments"].append(
            {
                "instrument_id": instrument,
                "paper_enabled": True,
                "execution_evidence": identity,
                "latest": {
                    "observed_at": AT.isoformat(),
                    "contract_renewal_status": "VERIFIED",
                    "decision": {"status": "NO_TRADE"},
                },
                "pending_entries": 0,
                "open_positions": 0,
                "closed_trades": 0,
                "realized_gross": None,
                "realized_net": None,
            }
        )
    return research, paper


def test_warmup_and_no_trades_are_not_fabricated_failures():
    research, paper = data()
    before = deepcopy((research, paper))
    result = inspect_observations(research, paper, AT + timedelta(seconds=1))
    assert result["errors"] == [] and result["broker_requests"] == 0
    assert result["instruments"][0]["realized_net"] is None
    assert (research, paper) == before


def test_stale_feed_and_failed_contract_renewal_are_reported():
    research, paper = data()
    paper["instruments"][1]["latest"]["contract_renewal_status"] = "MISSING"
    result = inspect_observations(research, paper, AT + timedelta(seconds=100))
    assert "CRUDEOILM:OBSERVATIONS_STALE_OR_UNAVAILABLE" in result["errors"]
    assert "NATGASMINI:CONTRACT_RENEWAL_UNAVAILABLE" in result["errors"]


def test_closed_session_does_not_report_expected_stale_data_as_failure():
    research, paper = data()
    assert inspect_observations(research, paper, AT.replace(hour=23, minute=45))["errors"] == []


def test_api_calendar_closed_session_does_not_trigger_false_staleness():
    research, paper = data()
    for row in paper["instruments"]:
        row["market_open"] = False
    result = inspect_observations(research, paper, AT + timedelta(seconds=100))
    assert result["errors"] == []
