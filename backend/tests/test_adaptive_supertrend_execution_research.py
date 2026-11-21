"""Offline actual-book research, causal selection and unchanged execution differential."""

from copy import deepcopy
from datetime import datetime, time, timedelta
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, DotEnvSettingsSource, EnvSettingsSource

from app.adaptive_supertrend import execution_policy_research as research
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.execution_research_config import ExecutionSpec, SignalOnlyPolicy
from app.adaptive_supertrend.execution_research_engine import HISTORICAL_COVERAGE_CENSORED, ExecutionShadow
from app.adaptive_supertrend.journal import verify
from app.adaptive_supertrend.models import AstCursor
from app.adaptive_supertrend.optimizer import GRID, metrics
from app.adaptive_supertrend.research import load_bars
from app.adaptive_supertrend.service import AstPaperService, freeze
from app.core.config import Settings
from app.db.session import build_session_factory
from app.research.manifest import digest
from app.scalper.repository import IST
from tests.test_adaptive_supertrend import EXPIRY, START, bars, policy
from tests.test_adaptive_supertrend_paper import capture, raw, rows
from tests.test_adaptive_supertrend_regime_research import DAYS, source_file
from tests.test_scalper import settings

EXAMPLE = Path(__file__).resolve().parents[2] / "docs/examples/ast-execution-research-candidates.json"


def spec(names=("LOW_RISK", "BALANCED"), **controls):
    value = json.loads(EXAMPLE.read_text())
    value["candidates"] = [c for c in value["candidates"] if c["name"] in names]
    value["controls"].update(controls)
    return ExecutionSpec.model_validate(value)


def signal_policy():
    # Synthetic tiny-history test policy; never a runtime/research recommendation.
    return policy(execution=None, regime={"mode": "NONE"})


def book(at, *, instrument="NOV", price=25100, closing=False):
    template = raw(0, price)
    quotes = []
    for strike, premium in ((25000, 70), (24950, 50), (24900, 40), (24850, 30), (24800, 20), (24750, 10)):
        q = template.quotes[0].model_copy(
            update={
                "expiry": EXPIRY,
                "strike": strike,
                "option_type": "PE",
                "exchange": "nse_fo",
                "instrument_token": f"nse_fo|{strike}",
                "trading_symbol": f"NIFTY{strike}PE",
                "source_market_timestamp": at - timedelta(seconds=2),
                "bid": premium if not closing else premium * 0.7,
                "ask": (premium if not closing else premium * 0.7) + 0.05,
                "bid_quantity": 650,
                "ask_quantity": 650,
                "depth_unit": "UNITS",
                "tick_size": 0.05,
                "ltp": 999,
                "open_interest": 200000,
                "volume": 150000,
            }
        )
        quotes.append(q)
    return template.model_copy(
        update={
            "captured_at": at,
            "request_started_at": at - timedelta(seconds=1),
            "response_received_at": at,
            "source_market_timestamp": None,
            "nifty_spot": price,
            "nifty_future": price,
            "future_instrument_id": instrument,
            "future_expiry": EXPIRY,
            "expiry": EXPIRY,
            "lot_size": 65,
            "quotes": quotes,
        }
    )


def snapshots(path, days=DAYS[:4], changed_day=None):
    records = []
    for day in days:
        for t in (time(10, 30), time(10, 30, 15), time(15, 20, 5)):
            at = datetime.combine(day, t, IST)
            snapshot = book(at, closing=t.hour == 15)
            if day == changed_day:
                snapshot = snapshot.model_copy(
                    update={"quotes": [q.model_copy(update={"bid": None, "ask": None}) for q in snapshot.quotes]}
                )
            records.append(snapshot)
    path.write_text("".join(r.model_dump_json() + "\n" for r in records))
    return records


def fake_optimize(training, day, selected_at, cfg, **kwargs):
    """Only orchestration/leakage tests use this complete-grid fixture."""
    assert kwargs == {"signal_research_only": True}
    assert all(b.start.date() < day and b.end < selected_at for b in training)
    value = metrics([{"date": str(training[0].start.date()), "signal_points": 5}], 1, 2)
    ranking = [
        {"atr_period": p, "multiplier_tenths": m, "metrics": value, "neighbour_robustness": {"fixture": True}}
        for p, m in GRID
    ]
    return {
        "trading_date": str(day),
        "selected_at": selected_at.isoformat(),
        "data_cutoff": max(b.end for b in training).isoformat(),
        "policy": cfg.model_dump(mode="json"),
        "policy_hash": cfg.policy_hash,
        "training_data_identity": digest([str(b) for b in training]),
        "ranking": ranking,
        "selected": ranking[0],
        "walk_forward_status": "NOT_RUN",
        "grid_count": 1886,
    }


@pytest.fixture(scope="module")
def real_experiment(tmp_path_factory):
    root = tmp_path_factory.mktemp("execution-real-grid")
    futures = source_file(root, count=4)
    source = root / "snapshots.jsonl"
    snapshots(source)
    progress = []
    original = source.read_bytes(), futures.read_bytes()
    result = research.run_research(load_bars(futures), source, signal_policy(), spec(), 1, 1, progress.append)
    assert original == (source.read_bytes(), futures.read_bytes())
    return result, progress


def test_real_full_grid_frozen_once_per_day_for_all_execution_candidates(real_experiment):
    result, progress = real_experiment
    assert result["compute"]["grid_fits"] == 3
    assert result["compute"]["candidates"] == 2
    assert result["compute"]["grid_size"] == 1886
    assert len([p for p in progress if p.startswith("grid ")]) == 3
    assert all(c["oos"]["actual_trades_opened"] > 0 for c in result["candidates"])
    for day, cached in result["daily_parameters"].items():
        shared = cached["signal"]
        assert shared is not None
        assert datetime.fromisoformat(shared["data_cutoff"]).date().isoformat() < day
        for candidate in result["candidates"]:
            hypothetical = cached["combined"][candidate["name"]]
            assert (shared["atr_period"], shared["multiplier_tenths"]) == (
                hypothetical["atr_period"],
                hypothetical["multiplier_tenths"],
            )
            assert hypothetical["policy_hash"] == candidate["combined_hypothetical_policy_id"]
            assert hypothetical["policy_hash"] != result["signal_policy_id"]
    assert len({c["combined_hypothetical_policy_id"] for c in result["candidates"]}) == 2
    assert result["futures_signal_opportunities_before_execution"]
    assert any("validation-only" in p for p in progress)


def test_exact_entries_exits_statistics_and_no_fabricated_costs(real_experiment):
    result, _ = real_experiment
    for candidate in result["candidates"]:
        summary = candidate["oos"]
        assert summary["actual_closed_trades"] == summary["actual_trades_opened"]
        assert summary["unresolved_exposure"] == 0
        assert summary["closed_option_metrics"]["net_option_pnl"] is None
        assert summary["distributions"]["quote_age_seconds"]["max"] == 2
        assert summary["distributions"]["open_interest"]["min"] == 200000
        assert summary["distributions"]["volume"]["min"] == 150000
        for doc in candidate["trades"]:
            assert doc["entry_timestamp"] > doc["decision_timestamp"]
            assert doc["entry_quotes"]["short"]["side"] == "bid"
            assert doc["entry_quotes"]["long"]["side"] == "ask"
            assert (
                doc["entry_credit"]
                == doc["entry_quotes"]["short"]["quote"]["bid"] - doc["entry_quotes"]["long"]["quote"]["ask"]
            )
            assert (
                doc["exit_debit"]
                == doc["exit_quotes"]["short"]["quote"]["ask"] - doc["exit_quotes"]["long"]["quote"]["bid"]
            )
            assert doc["exit_reason"] == "FORCED_CLOSE" and doc["net_pnl"] is None
            assert doc["policy_hash"] == digest(doc["policy"])
            assert doc["entry_credit"] != 999  # LTP fixture is deliberately unusable.


def test_plain_gzip_determinism_and_candidate_invariance(tmp_path, monkeypatch):
    values = load_bars(source_file(tmp_path, count=4))
    plain = tmp_path / "raw.jsonl"
    snapshots(plain)
    compressed = tmp_path / "raw.jsonl.gz"
    compressed.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
    calls = []

    def counted(*args, **kwargs):
        calls.append((args[1], args[3].policy_hash))
        return fake_optimize(*args, **kwargs)

    monkeypatch.setattr(research, "optimize", counted)
    before = plain.read_bytes(), compressed.read_bytes()
    a = research.run_research(values, plain, signal_policy(), spec(), 1, 1)
    assert len(calls) == 3
    b = research.run_research(values, compressed, signal_policy(), spec(), 1, 1)
    assert a == b
    single = research.run_research(values, plain, signal_policy(), spec(("BALANCED",)), 1, 1)
    for day, cached in a["daily_parameters"].items():
        assert cached["signal"] == single["daily_parameters"][day]["signal"]
        assert cached["combined"]["BALANCED"] == single["daily_parameters"][day]["combined"]["BALANCED"]
    assert all(h == signal_policy().policy_hash for _, h in calls)
    assert before == (plain.read_bytes(), compressed.read_bytes())


def test_oos_books_and_later_prices_cannot_choose_the_execution_policy(tmp_path, monkeypatch):
    csv = source_file(tmp_path, count=4)
    values = load_bars(csv)
    source = tmp_path / "raw.jsonl"
    snapshots(source)
    monkeypatch.setattr(research, "optimize", fake_optimize)
    before = research.run_research(values, source, signal_policy(), spec(), 1, 1)
    snapshots(source, changed_day=DAYS[2])
    source_file(tmp_path, count=4, changed_day=DAYS[2])
    after = research.run_research(load_bars(csv), source, signal_policy(), spec(), 1, 1)
    assert before["folds"][0]["selection"] == after["folds"][0]["selection"]
    assert before["daily_parameters"][str(DAYS[2])]["signal"] == after["daily_parameters"][str(DAYS[2])]["signal"]
    assert before["folds"][0]["oos_results"] != after["folds"][0]["oos_results"]


def prepared_shadow(*, costs=None, **controls):
    cfg = AstPolicy.model_validate({**signal_policy().model_dump(mode="json"), "costs": costs or {}})
    definition = spec(("WIDTH_100_ONLY",), **controls)
    shadow = ExecutionShadow(cfg, definition, definition.candidates[0])
    training = bars(day=START - timedelta(days=1))
    selected = fake_optimize(training, START.date(), START.replace(hour=9, minute=0), cfg, signal_research_only=True)
    params = research.parameter_document(selected, shadow.policy)
    value = book(START.replace(hour=10, minute=30), instrument="FUT")
    context = bars(15)
    signal_params = research.parameter_document(selected, cfg)
    state = {}
    decision = decide(
        context,
        value.captured_at,
        value.nifty_future,
        signal_params,
        state,
        SignalOnlyPolicy.model_validate(cfg.model_dump(mode="json")),
        future_identity=("FUT", str(EXPIRY)),
    )
    assert decision["status"] == "ENTER"
    return shadow, value, decision, params, state["opening_range"]


def test_next_observation_only_and_actual_core_exit_books():
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    assert shadow.rows[0].state == "PENDING_PAPER_ENTRY"
    shadow.observe(value, 1, decision, params, orb)
    assert shadow.rows[0].state == "PENDING_PAPER_ENTRY"  # same clock never fills
    following = book(value.captured_at + timedelta(seconds=15), instrument="FUT")
    shadow.observe(following, 2, decision, params, orb)
    assert shadow.rows[0].state == "OPEN"
    at = value.captured_at.replace(hour=15, minute=20)
    closing = book(at, instrument="FUT", closing=True)
    shadow.observe(closing, 3, decision, params, orb)
    doc = json.loads(shadow.rows[0].document)
    assert shadow.rows[0].state == "CLOSED" and doc["exit_reason"] == "FORCED_CLOSE"
    assert doc["exit_quotes"]["short"]["side"] == "ask" and doc["exit_quotes"]["long"]["side"] == "bid"


@pytest.mark.parametrize(
    "failure", ["missing_timestamp", "stale", "missing_book", "wrong_token", "wrong_expiry", "missing_depth"]
)
def test_pending_fill_fails_closed_without_ltp_fallback(failure):
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    following = book(value.captured_at + timedelta(seconds=15), instrument="FUT")
    updates = {
        "missing_timestamp": {"source_market_timestamp": None},
        "stale": {"source_market_timestamp": following.captured_at - timedelta(seconds=10)},
        "missing_book": {"bid": None, "ask": None},
        "wrong_token": {"instrument_token": "WRONG"},
        "wrong_expiry": {"expiry": EXPIRY + timedelta(days=1)},
        "missing_depth": {"bid_quantity": None, "ask_quantity": None},
    }[failure]
    following = following.model_copy(update={"quotes": [q.model_copy(update=updates) for q in following.quotes]})
    if failure == "wrong_expiry":
        following = following.model_copy(update={"expiry": EXPIRY + timedelta(days=1)})
    shadow.observe(following, 2, decision, params, orb)
    assert shadow.rows[0].state == "ENTRY_REJECTED"
    assert "entry_credit" not in json.loads(shadow.rows[0].document)
    assert sum(shadow.days[str(START.date())]["fill_rejects"].values()) == 1


def test_coverage_loss_preserves_unresolved_evidence_and_allows_only_later_session_continuation():
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    missing = book(value.captured_at + timedelta(seconds=30), instrument="FUT")
    missing = missing.model_copy(
        update={"quotes": [q.model_copy(update={"instrument_token": "missing"}) for q in missing.quotes]}
    )
    shadow.observe(missing, 3, decision, params, orb)
    assert shadow.rows[0].state == "UNRESOLVED_EXPOSURE"
    original = shadow.rows[0].document
    doc = json.loads(original)
    assert doc["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    assert not any(key in doc for key in ("exit_timestamp", "exit_debit", "gross_pnl", "net_pnl"))
    censored = shadow.coverage_censoring[shadow.rows[0].id]
    assert censored["classification"] == HISTORICAL_COVERAGE_CENSORED
    assert censored["pnl"] is None and censored["pnl_status"] == "UNPRICED"
    assert censored["original_document_hash"] == digest(doc)
    assert "research_metadata" in shadow.trace[-1]
    # Synthetic later confirmed transition isolates the risk block from the
    # already-consumed-opportunity guard. It must not create a same-session trade.
    later = book(value.captured_at + timedelta(minutes=10), instrument="FUT")
    fresh = deepcopy(decision)
    fresh["features"]["ast_transition"] = later.captured_at.isoformat()
    shadow.observe(later, 4, fresh, params, orb)
    assert len(shadow.rows) == 1
    assert shadow.days[str(START.date())]["entry_rejects"]["MAX_OPEN_POSITIONS_REACHED"] > 0
    next_raw, next_decision, next_params, next_orb = next_session_signal(shadow)
    shadow.observe(next_raw, 5, next_decision, next_params, next_orb)
    assert len(shadow.rows) == 2 and shadow.rows[1].state == "PENDING_PAPER_ENTRY"
    shadow.observe(
        book(next_raw.captured_at + timedelta(seconds=15), instrument="FUT"), 6, next_decision, next_params, next_orb
    )
    assert shadow.rows[1].state == "OPEN"
    assert shadow.rows[0].state == "UNRESOLVED_EXPOSURE" and shadow.rows[0].document == original


def next_session_signal(shadow):
    day = START + timedelta(days=1)
    cfg = signal_policy()
    result = fake_optimize(bars(day=START), day.date(), day.replace(hour=9, minute=0), cfg, signal_research_only=True)
    value = book(day.replace(hour=10, minute=30), instrument="FUT")
    state = {}
    decision = decide(
        bars(15, day=day),
        value.captured_at,
        value.nifty_future,
        research.parameter_document(result, cfg),
        state,
        SignalOnlyPolicy.model_validate(cfg.model_dump(mode="json")),
        future_identity=("FUT", str(EXPIRY)),
    )
    assert decision["status"] == "ENTER"
    return value, decision, research.parameter_document(result, shadow.policy), state["opening_range"]


@pytest.mark.parametrize("reason", ["MISSING_QUOTE_TIMESTAMP", "INSUFFICIENT_DEPTH", "MISSING_EXECUTABLE_SIDE"])
def test_other_unresolved_reasons_continue_blocking_later_research_sessions(reason):
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    bad = book(value.captured_at.replace(hour=15, minute=20), instrument="FUT")
    changes = {
        "MISSING_QUOTE_TIMESTAMP": {"source_market_timestamp": None},
        "INSUFFICIENT_DEPTH": {"bid_quantity": 0, "ask_quantity": 0},
        "MISSING_EXECUTABLE_SIDE": {"bid": None, "ask": None},
    }[reason]
    bad = bad.model_copy(update={"quotes": [q.model_copy(update=changes) for q in bad.quotes]})
    shadow.observe(bad, 3, decision, params, orb)
    assert shadow.rows[0].state == "UNRESOLVED_EXPOSURE"
    assert json.loads(shadow.rows[0].document)["unresolved_reason"] == reason
    assert not shadow.coverage_censoring
    raw_next, decision_next, params_next, orb_next = next_session_signal(shadow)
    shadow.observe(raw_next, 4, decision_next, params_next, orb_next)
    assert len(shadow.rows) == 1
    summary = shadow.summary([str(START.date())], START + timedelta(days=1))
    assert summary["other_unresolved_exposure"] == summary["active_exposure_at_cutoff"] == 1
    assert summary["historical_coverage_censored_trades"] == 0


def test_validation_cutoff_cannot_see_a_future_censor_or_retroactively_clear_active_risk():
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    cutoff = (START + timedelta(days=1)).replace(hour=9, minute=15)
    before = shadow.summary([str(START.date())], cutoff)
    future = book(value.captured_at + timedelta(days=1), instrument="FUT")
    future = future.model_copy(
        update={"quotes": [q.model_copy(update={"instrument_token": q.instrument_token + "X"}) for q in future.quotes]}
    )
    shadow.observe(future, 3, decision, params, orb)
    assert shadow.summary([str(START.date())], cutoff) == before
    assert before["active_exposure_at_cutoff"] == 1
    assert before["historical_coverage_censored_trades"] == before["entry_cohort_coverage_censored"] == 0
    later = shadow.summary([str(START.date())], START + timedelta(days=2))
    assert later["entry_cohort_coverage_censored"] == 1
    assert later["entry_cohort_closed_metrics"]["gross_option_pnl"] is None


def test_coverage_censoring_keeps_later_oos_folds_evaluable_without_zero_pnl(tmp_path, monkeypatch):
    futures = source_file(tmp_path, count=4)
    source = tmp_path / "raw.jsonl"
    records = snapshots(source)
    # First eligible session's held legs disappear at the forced-exit observation.
    at_loss = datetime.combine(DAYS[1], time(15, 20, 5), IST)
    records = [
        (
            r.model_copy(
                update={
                    "quotes": [q.model_copy(update={"instrument_token": q.instrument_token + "X"}) for q in r.quotes]
                }
            )
            if r.captured_at == at_loss
            else r
        )
        for r in records
    ]
    source.write_text("".join(r.model_dump_json() + "\n" for r in records))
    monkeypatch.setattr(research, "optimize", fake_optimize)
    result = research.run_research(load_bars(futures), source, signal_policy(), spec(("WIDTH_100_ONLY",)), 1, 1)
    candidate = result["candidates"][0]
    assert result["schema_version"] == "AST_EXECUTION_POLICY_RESEARCH_V2"
    assert result["folds"][0]["selection"]["candidate_id"] is None  # censor-only validation is not a zero-return win
    ranking = result["folds"][0]["selection"]["validation_ranking"][0]
    assert ranking["closed_sample_size"] == 0 and ranking["coverage_censored_sample_size"] == 1
    assert ranking["censor_rate"] == 1 and ranking["confidence"] == "DESCRIPTIVE_ONLY_NO_CONFIDENCE_CLAIM"
    assert result["folds"][1]["selection"]["candidate_id"] == candidate["execution_candidate_id"]
    total = candidate["all_research_sessions"]
    assert total["actual_trades_opened"] == 3
    assert total["fully_observed_closed_trades"] == total["entry_cohort_fully_observed_closed"] == 2
    assert total["historical_coverage_censored_trades"] == total["entry_cohort_coverage_censored"] == 1
    assert total["other_unresolved_exposure"] == 0 and total["unresolved_exposure"] == 1
    assert total["observed_path_completion_rate"] == pytest.approx(2 / 3)
    assert total["censor_reason_counts"] == {"MISSING_EXACT_CONTRACT": 1}
    first = candidate["trades"][0]
    assert candidate["trade_states"][first["trade_id"]] == "UNRESOLVED_EXPOSURE"
    assert candidate["research_trade_metadata"][first["trade_id"]]["classification"] == HISTORICAL_COVERAGE_CENSORED
    assert "gross_pnl" not in first and "exit_timestamp" not in first
    metric = total["closed_option_metrics"]
    closed = [t for t in candidate["trades"] if "exit_timestamp" in t]
    assert metric["population"] == "FULLY_OBSERVED_PATHS_ONLY" and metric["total_trades"] == 2
    assert metric["gross_option_pnl"] == pytest.approx(sum(t["gross_pnl"] for t in closed))
    assert metric["expectancy"] == pytest.approx(sum(t["gross_pnl"] for t in closed) / 2)
    assert metric["net_option_pnl"] is None
    compressed = tmp_path / "raw.jsonl.gz"
    compressed.write_bytes(gzip.compress(source.read_bytes(), mtime=0))
    assert (
        research.run_research(load_bars(futures), compressed, signal_policy(), spec(("WIDTH_100_ONLY",)), 1, 1)
        == result
    )


def test_controls_are_explicit_identical_and_blockers_separate():
    shadow, value, decision, params, orb = prepared_shadow(kill_switch=True)
    shadow.observe(value, 1, decision, params, orb)
    summary = shadow.summary([str(START.date())], START + timedelta(days=1))
    assert not shadow.rows
    assert summary["opportunities_with_any_executable_candidate"] == 1
    assert summary["research_control_block_counts"] == {"KILL_SWITCH_ACTIVE": 1}
    definition = spec()
    for candidate in definition.candidates:
        cfg = definition.execution(candidate)
        for key, value in definition.controls.model_dump().items():
            assert cfg.model_dump()[key] == value
        assert cfg.safety.label == "HARD_SAFETY_ONLY"
        assert cfg.safety.profit_capture_pct is cfg.safety.stop_credit_multiple is cfg.safety.time_stop_minutes is None
    bad = definition.model_dump(mode="json")
    bad["controls"]["safety"]["profit_capture_pct"] = 50
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(bad)
    for key in type(definition.controls).model_fields:
        bad = definition.model_dump(mode="json")
        del bad["controls"][key]
        with pytest.raises(ValidationError):
            ExecutionSpec.model_validate(bad)


def test_future_exit_never_counts_in_validation_statistics():
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    cutoff = (value.captured_at + timedelta(days=1)).replace(hour=9, minute=15)
    before = shadow.summary([str(START.date())], cutoff)
    shadow.observe(
        book((value.captured_at + timedelta(days=1)).replace(hour=15, minute=20), instrument="FUT", closing=True),
        3,
        decision,
        params,
        orb,
    )
    after = shadow.summary([str(START.date())], cutoff)
    assert before == after
    assert after["open_at_cutoff"] == 1 and after["actual_closed_trades"] == 0


def test_eof_does_not_invent_a_forced_exit_or_price():
    shadow, value, decision, params, orb = prepared_shadow()
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    summary = shadow.summary([str(START.date())], START + timedelta(days=1))
    assert summary["open_at_cutoff"] == 1 and summary["actual_closed_trades"] == 0
    assert summary["entry_cohort_unpriced_at_cutoff"] == 1
    assert summary["closed_option_metrics"]["net_option_pnl"] is None
    assert "exit_debit" not in json.loads(shadow.rows[0].document)


def test_net_requires_explicit_complete_supported_dated_costs():
    costs = dict(
        cost_schedule_version="SYNTHETIC_TEST_ONLY",
        effective_from=str(START.date()),
        effective_to=str(EXPIRY),
        declared_complete=True,
        brokerage_per_order=1,
        exchange_rate=0,
        stt_rate=0,
        gst_rate=0,
        stamp_rate=0,
        sebi_rate=0,
        slippage_points_per_leg=0.05,
    )
    shadow, value, decision, params, orb = prepared_shadow(costs=costs)
    shadow.observe(value, 1, decision, params, orb)
    shadow.observe(book(value.captured_at + timedelta(seconds=15), instrument="FUT"), 2, decision, params, orb)
    shadow.observe(
        book(value.captured_at.replace(hour=15, minute=20), instrument="FUT", closing=True), 3, decision, params, orb
    )
    doc = json.loads(shadow.rows[0].document)
    assert doc["final_outcome"]["cost_completeness"] == "NET_COMPLETE"
    assert doc["net_pnl"] == pytest.approx(doc["gross_pnl"] - doc["final_outcome"]["total_cost_rupees"])
    assert (
        shadow.summary([str(START.date())], START + timedelta(days=1))["closed_option_metrics"]["net_option_pnl"]
        == doc["net_pnl"]
    )


def test_candidate_count_compute_and_source_order_guards(tmp_path, monkeypatch):
    values = json.loads(EXAMPLE.read_text())
    values["candidates"] = [{**values["candidates"][0], "name": str(i)} for i in range(33)]
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(values)
    values = json.loads(EXAMPLE.read_text())
    values["candidates"][0]["allowed_widths"] = [250]
    with pytest.raises(ValidationError):
        ExecutionSpec.model_validate(values)
    path = tmp_path / "raw.jsonl"
    records = snapshots(path)
    monkeypatch.setattr(research, "MAX_EVALUATIONS", 1)
    with pytest.raises(ValueError, match="BUDGET"):
        research.inventory(path, 2)
    monkeypatch.setattr(research, "MAX_EVALUATIONS", 1000)
    path.write_text(records[0].model_dump_json() + "\n" + records[0].model_dump_json() + "\n")
    with pytest.raises(ValueError, match="ORDER"):
        research.inventory(path, 2)


def test_research_delegates_existing_lifecycle_without_broker_or_database_calls():
    assert ExecutionShadow._transition is AstPaperService._transition
    assert ExecutionShadow._risk is AstPaperService._risk
    for module in (research, inspect.getmodule(ExecutionShadow)):
        source = inspect.getsource(module)
        assert (
            "app.acts_paper" not in source
            and "create_engine(" not in source
            and "build_session_factory(" not in source
        )
        assert "app.broker" not in source and "app.collector" not in source and "worker." not in source


def test_cli_read_only_sources_alias_guard_progress_and_deterministic_output(tmp_path, monkeypatch, capsys):
    csv = source_file(tmp_path, count=4)
    raw_path, policy_path, candidates_path, output = [
        tmp_path / n for n in ("raw.jsonl.gz", "signal.json", "candidates.json", "out.json")
    ]
    source = tmp_path / "raw.jsonl"
    snapshots(source)
    raw_path.write_bytes(gzip.compress(source.read_bytes(), mtime=0))
    policy_path.write_text(signal_policy().model_dump_json())
    candidates_path.write_text(spec().model_dump_json())
    monkeypatch.setattr(research, "optimize", fake_optimize)
    sources = [csv, raw_path, policy_path, candidates_path]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    args = [
        "--futures-csv",
        str(csv),
        "--snapshots",
        str(raw_path),
        "--signal-policy",
        str(policy_path),
        "--candidates",
        str(candidates_path),
        "--validation-sessions",
        "1",
        "--oos-sessions",
        "1",
        "--json",
        str(output),
    ]
    assert research.main(args) == 0
    first = output.read_bytes()
    assert research.main(args) == 0 and output.read_bytes() == first
    assert json.loads(first)["source_file_sha256"] == hashes
    captured = capsys.readouterr()
    assert captured.out == "" and "grid " in captured.err and "fold " in captured.err
    assert hashes == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    args[-1] = str(csv)
    with pytest.raises(SystemExit):
        research.main(args)


@pytest.mark.parametrize("coverage_loss", [False, True])
def test_research_shadow_differential_against_real_paper_service(engine, coverage_loss):
    sessions = build_session_factory(engine)
    definition = spec(("WIDTH_100_ONLY",))
    sig = signal_policy()
    cfg = definition.combined(sig, definition.candidates[0])
    with sessions.begin() as session:
        session.add(AstCursor(id=1))
    prior = START - timedelta(days=1)
    frozen = freeze(sessions, bars(day=prior), START.date(), START.replace(hour=9, minute=0), cfg)
    signal_result = research.optimize(
        bars(day=prior), START.date(), START.replace(hour=9, minute=0), sig, signal_research_only=True
    )
    assert research.parameter_document(signal_result, cfg) == frozen
    service = AstPaperService(sessions, settings(ast_config_json=cfg.model_dump_json()))
    shadow = ExecutionShadow(sig, definition, definition.candidates[0])
    # Build the same sampled completed-bar history for both sides. Decisions below
    # are obtained from the actual service, before the research candidate remap.
    for i in range(300):
        value = book(START + timedelta(seconds=15 * i), instrument="FUT", price=25000 + i / 20)
        # Warm signal history without creating an earlier executable option entry.
        value = value.model_copy(
            update={"quotes": [q.model_copy(update={"bid_quantity": 0, "ask_quantity": 0}) for q in value.quotes]}
        )
        service.process_snapshot(capture(engine, value))
    value = book(START.replace(hour=10, minute=30), instrument="FUT")
    sid = capture(engine, value)
    assert service.process_snapshot(sid) == "PENDING_PAPER_ENTRY"
    runtime = rows(service)[0][2]
    parameters = runtime["session_parameters"]
    decision = {
        "status": "ENTER",
        "direction": runtime["candidate"]["direction"],
        "blockers": [],
        "features": deepcopy(runtime["strategy_features"]),
        "opportunity_id": runtime["opportunity_id"],
    }
    orb = {
        k: decision["features"][k]
        for k in (
            "range_start",
            "range_end",
            "opening_range_date",
            "complete",
            "opening_high",
            "opening_low",
            "reason",
        )
    }
    shadow.observe(value, sid, decision, parameters, orb)
    for offset, at in (
        (1, value.captured_at + timedelta(seconds=15)),
        (2, value.captured_at.replace(hour=15, minute=20)),
    ):
        snapshot = book(at, instrument="FUT", closing=offset == 2)
        if coverage_loss and offset == 2:
            snapshot = snapshot.model_copy(
                update={
                    "quotes": [
                        q.model_copy(update={"instrument_token": q.instrument_token + "X"}) for q in snapshot.quotes
                    ]
                }
            )
        sid = capture(engine, snapshot)
        service.process_snapshot(sid)
        shadow.observe(snapshot, sid, decision, parameters, orb)
        assert rows(service)[0][1] == shadow.rows[0].state
        for key in ("entry_credit", "entry_quotes", "defined_max_risk", "policy_hash", "opportunity_id"):
            assert json.loads(shadow.rows[0].document)[key] == rows(service)[0][2][key]
    expected, observed = rows(service)[0][2], json.loads(shadow.rows[0].document)
    if coverage_loss:
        assert observed["unresolved_reason"] == expected["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
        assert not any(k in expected or k in observed for k in ("exit_timestamp", "exit_debit", "gross_pnl"))
        assert shadow.coverage_censoring[observed["trade_id"]]["classification"] == HISTORICAL_COVERAGE_CENSORED
        # Reappearing quotes on a later day do not recover the production paper row.
        service.process_snapshot(capture(engine, book(value.captured_at + timedelta(days=1), instrument="FUT")))
        assert len(rows(service)) == 1 and rows(service)[0][1] == "UNRESOLVED_EXPOSURE"
        assert rows(service)[0][2] == expected
    else:
        for key in ("exit_debit", "exit_quotes", "gross_pnl", "net_pnl", "exit_reason"):
            assert observed[key] == expected[key]
    verify(sessions, full=True)


def test_shadow_needs_no_consumer_key_or_settings_environment_loading(monkeypatch):
    for name in list(os.environ):
        if name.casefold() == "kotak_consumer_key":
            monkeypatch.delenv(name)
    monkeypatch.setenv("SCALPER_ALLOWED_WIDTHS", "INVALID_ENVIRONMENT_MUST_NOT_BE_READ")
    monkeypatch.setenv("SCALPER_INTERVAL_SECONDS", "INVALID_ENVIRONMENT_MUST_NOT_BE_READ")

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline research must not construct/load BaseSettings")

    monkeypatch.setattr(Settings, "__init__", forbidden)
    monkeypatch.setattr(BaseSettings, "__init__", forbidden)
    monkeypatch.setattr(BaseSettings, "_settings_build_values", forbidden)
    monkeypatch.setattr(EnvSettingsSource, "__call__", forbidden)
    monkeypatch.setattr(DotEnvSettingsSource, "__call__", forbidden)
    definition = spec()
    shadow = ExecutionShadow(signal_policy(), definition, definition.candidates[0])
    assert shadow.config.interval_seconds == Settings.model_fields["scalper_interval_seconds"].default
    assert shadow.config.allowed_widths == definition.candidates[0].allowed_widths
    assert not hasattr(shadow, "settings")
    assert not hasattr(shadow, "kotak_consumer_key")


def test_execution_results_ignore_unrelated_credential_and_scalper_environment(tmp_path, monkeypatch):
    values = load_bars(source_file(tmp_path, count=3))
    source = tmp_path / "raw.jsonl"
    snapshots(source, days=DAYS[:3])
    monkeypatch.setattr(research, "optimize", fake_optimize)
    for name in list(os.environ):
        if name.casefold() == "kotak_consumer_key":
            monkeypatch.delenv(name)
    before = research.run_research(values, source, signal_policy(), spec(), 1, 1)
    # A negative-control environment value, never passed to or used as a credential.
    monkeypatch.setenv("KOTAK_CONSUMER_KEY", "UNRELATED_ENVIRONMENT_VALUE_MUST_NOT_BE_READ")
    monkeypatch.setenv("SCALPER_ALLOWED_WIDTHS", "INVALID_ENVIRONMENT_MUST_NOT_BE_READ")
    monkeypatch.setenv("SCALPER_SIGNAL_MIN_SCORE", "INVALID_ENVIRONMENT_MUST_NOT_BE_READ")
    after = research.run_research(values, source, signal_policy(), spec(), 1, 1)
    assert before == after


def test_real_cli_without_consumer_key_and_with_poisoned_environment_and_dotenv(tmp_path):
    futures = source_file(tmp_path, count=3)
    source, policy_path, candidates_path, output = [
        tmp_path / n for n in ("raw.jsonl.gz", "signal.json", "candidates.json", "out.json")
    ]
    plain = tmp_path / "raw.jsonl"
    snapshots(plain, days=DAYS[:3])
    source.write_bytes(gzip.compress(plain.read_bytes(), mtime=0))
    policy_path.write_text(signal_policy().model_dump_json())
    candidates_path.write_text(spec().model_dump_json())
    (tmp_path / ".env").write_text("SCALPER_ALLOWED_WIDTHS=INVALID_DOTENV_MUST_NOT_BE_READ\n")
    env = {name: value for name, value in os.environ.items() if name.casefold() != "kotak_consumer_key"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["SCALPER_INTERVAL_SECONDS"] = "INVALID_ENVIRONMENT_MUST_NOT_BE_READ"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.adaptive_supertrend.execution_policy_research",
            "--futures-csv",
            str(futures),
            "--snapshots",
            str(source),
            "--signal-policy",
            str(policy_path),
            "--candidates",
            str(candidates_path),
            "--validation-sessions",
            "1",
            "--oos-sessions",
            "1",
            "--json",
            str(output),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and "grid " in result.stderr
    report = json.loads(output.read_text())
    assert report["compute"]["grid_fits"] == 2
    assert all(candidate["oos"]["actual_trades_opened"] > 0 for candidate in report["candidates"])
