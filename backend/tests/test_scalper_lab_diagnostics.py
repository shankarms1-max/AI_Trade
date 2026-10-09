"""The ACTS diagnostic reports evidence without changing replay decisions."""
from copy import deepcopy
import json
import sys

from app.scalper_lab import acts
from app.scalper_lab.strategies import decide
from scripts.diagnose_scalper_acts import diagnose, main
from tests.test_scalper_acts import acts_context, qualified_rows


def _context(identity, at, *, complete, direction):
    return {
        "snapshot_id": identity, "timestamp": at, "spot": 24900.0,
        "future": 24920.0, "future_instrument_id": "NIFTY-FUT",
        "future_expiry": "2026-10-29", "opening_high": 25000.0,
        "opening_low": 24950.0,
        "futures_returns_bps": {"5m": -5.0, "15m": -9.0},
        "structure": {"range_position": .3, "opening_range_complete": complete,
                      "horizon_reference_at": {"5m": None, "15m": None}},
        "vwap": {"status": "UNAVAILABLE", "value": None},
        "regime": "BEARISH",
        "acts": {"direction": direction, "alpha": {"status": "ALPHA_UNAVAILABLE"},
                 "regime": {"futures_5m_bps": -5.0,
                            "futures_15m_bps": -9.0, "range_position": .3}},
    }


def test_diagnostic_exposes_opening_range_gate_vs_trend_pullback():
    at = "2026-10-08T09:30:00+05:30"
    incomplete = _context(1, at, complete=False, direction=None)
    complete = _context(2, "2026-10-08T09:30:15+05:30", complete=True,
                        direction="BEAR")
    decisions = [{"snapshot_id": row["snapshot_id"],
                  "strategy_id": "TREND_PULLBACK", "direction": "BEAR",
                  "status": "WATCH", "blockers": []}
                 for row in (incomplete, complete)]
    original = deepcopy((incomplete, complete, decisions))
    summary, details = diagnose([incomplete, complete], decisions)

    assert (incomplete, complete, decisions) == original
    assert summary["counts"]["valid_futures_5m"] == 2
    assert summary["counts"]["valid_futures_15m"] == 2
    assert summary["counts"]["futures_5m_bear_pass"] == 2
    assert summary["counts"]["futures_15m_bear_pass"] == 2
    assert summary["counts"]["range_bear_pass"] == 2
    assert summary["counts"]["opening_range_complete"] == 1
    assert summary["counts"]["full_bear_regime_before_alpha"] == 1
    assert summary["counts"]["trend_bear_acts_other"] == 1
    assert summary["counts"]["acts_input_mismatch_futures_5m_bps"] == 0
    assert summary["missing_fields"]["opening_range_incomplete"] == 1
    assert summary["alpha_status_split"] == {"ALPHA_UNAVAILABLE": 2}
    assert details[0]["trend_pullback_direction"] == "BEAR"
    assert details[0]["acts_regime"] == "RANGE"
    assert details[0]["spot_to_opening_midpoint"] == "BELOW"
    assert details[0]["future_to_opening_midpoint"] == "BELOW"
    assert details[0]["timezone_offset"] == 19800


def test_diagnostic_marks_missing_futures_and_contract_change():
    first = _context(1, "2026-10-08T09:25:00+05:30", complete=False, direction=None)
    second = _context(2, "2026-10-08T09:30:00+05:30", complete=True, direction=None)
    second["future_instrument_id"] = "OTHER-FUT"
    second["futures_returns_bps"]["5m"] = None
    second["acts"]["regime"]["futures_5m_bps"] = None
    second["structure"]["horizon_reference_at"]["5m"] = first["timestamp"]
    summary, details = diagnose([first, second], [])

    assert summary["opening_window_observations"] == 1
    assert summary["counts"]["valid_futures_5m"] == 1
    assert summary["counts"]["future_contract_change_5m"] == 1
    assert summary["missing_fields"]["futures_5m"] == 1
    assert details[1]["futures_contract_continuity"]["5m"] == "CHANGED_CONTRACT"


def test_incomplete_opening_range_blocks_acts_but_not_trend_pullback():
    ctx = acts_context(qualified_rows(-1))
    assert acts.regime(ctx)[0] == "BEAR"
    ctx["structure"]["opening_range_complete"] = False
    assert acts.regime(ctx)[0] is None
    assert decide("TREND_PULLBACK", ctx, {}, 69)["direction"] == "BEAR"


def test_diagnostic_cli_writes_separate_observation_report(tmp_path, monkeypatch):
    source = tmp_path / "replay"
    source.mkdir()
    context = _context(1, "2026-10-08T09:30:00+05:30",
                       complete=False, direction=None)
    (source / "contexts.jsonl").write_text(json.dumps(context) + "\n")
    (source / "decisions.jsonl").write_text("")
    output = tmp_path / "diagnostic"
    monkeypatch.setattr(sys, "argv", [
        "diagnose_scalper_acts", "--lab-dir", str(source), "--output", str(output)])

    main()

    assert json.loads((output / "summary.json").read_text())["observations"] == 1
    assert json.loads((output / "observations.jsonl").read_text())["snapshot_id"] == 1
    assert (source / "contexts.jsonl").read_text() == json.dumps(context) + "\n"
