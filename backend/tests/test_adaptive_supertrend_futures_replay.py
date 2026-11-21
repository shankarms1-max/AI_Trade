"""File-only replay differentials against the actual paper service and frozen daily optimizer."""

import csv
from datetime import datetime, time, timedelta
import gzip
import json

import pytest
from sqlalchemy import select

from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend import futures_replay as replay
from app.adaptive_supertrend.futures_replay_report import summary, trade_result
from app.adaptive_supertrend.journal import verify
from app.adaptive_supertrend.models import AstCursor, AstTrade
from app.adaptive_supertrend.research import load_bars
from app.adaptive_supertrend.service import AstPaperService, freeze
from app.db.session import build_session_factory
from tests.test_adaptive_supertrend import EXPIRY, START, bars, policy
from tests.test_adaptive_supertrend_paper import capture, raw
from tests.test_scalper import settings


def fixture_files(root):
    path, snapshot_path, config = root / "futures.csv", root / "ACTS_PAPER.jsonl.gz", root / "policy.json"
    cfg = policy(regime={"mode": "NONE"}).model_dump(mode="json")
    cfg["optimizer"]["lookback_sessions"] = 15
    cfg["execution"].update(entry_end_time="10:15", session_exit_time="10:20")
    config.write_text(json.dumps(cfg))
    dates = sorted(START - timedelta(days=i) for i in range(1, 28) if (START - timedelta(days=i)).weekday() < 5)[-15:]
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["start", "end", "open", "high", "low", "close", "future_instrument_id", "future_expiry"])
        for day in [*dates, START]:
            for bar in bars(75 if day in (dates[0], START) else 1, day=day):
                writer.writerow(
                    [bar.start.isoformat(), bar.end.isoformat(), bar.open, bar.high, bar.low, bar.close, "FUT", EXPIRY]
                )
    tape = []
    for i in range(262):
        # The first fill gets exactly one opportunity, then a completed bar turns down.
        value = raw(i, (25000 + (i // 20) * 3 + (i % 20) / 100) if i < 240 else 24800 - i / 100)
        tape.append(value)
    write_snapshots(snapshot_path, tape)
    return path, snapshot_path, config, tape


def write_snapshots(path, tape):
    with gzip.open(path, "wt") as stream:
        for raw_value in tape:
            stream.write(raw_value.model_dump_json(exclude={"quotes"}) + "\n")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    return fixture_files(tmp_path_factory.mktemp("ast_replay"))


def test_cli_deterministic_no_broker_or_database_and_cutoff_override(inputs, tmp_path, monkeypatch):
    from pydantic_settings import BaseSettings
    from sqlalchemy.engine import Engine

    def forbidden(*args, **kwargs):
        pytest.fail("offline replay attempted runtime I/O")

    monkeypatch.setattr(BaseSettings, "__init__", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.delenv("KOTAK_CONSUMER_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "forbidden://offline")
    path, snapshots, config, _ = inputs
    output = tmp_path / "result.json"
    args = [
        "--futures-csv",
        str(path),
        "--snapshots",
        str(snapshots),
        "--policy",
        str(config),
        "--session-exit-time",
        "10:19:30",
        "--json",
        str(output),
    ]
    assert replay.main(args) == 0
    content = output.read_bytes()
    assert replay.main(args) == 0 and output.read_bytes() == content
    result = json.loads(content)
    assert result["policy"]["execution"]["session_exit_time"] == "10:19:30"
    assert result["comparison"]["supplied_walk_forward_benchmark"]["gross_points"] == 395.6
    comp = result["comparison"]
    delta = comp["closed_trade_difference_attribution"]
    assert delta["both_closed"] == 1
    assert comp["same_session_5m_gross_points"] + delta["matched_entry_effect_points"] + delta[
        "matched_exit_effect_points"
    ] == pytest.approx(result["summary"]["gross_points"])
    assert result["summary"]["exit_reason_counts"]["SESSION_END"] == 1
    assert result["sessions"][0]["freeze_status"] == "FROZEN"
    assert set(result["input_sha256"]) == {"futures_csv", "snapshots", "policy"}
    with pytest.raises(SystemExit):
        replay.main([*args[:-1], str(snapshots)])


def test_runtime_and_file_replay_same_entries_consumption_reversal_and_pnl(engine, inputs):
    path, snapshots, config, tape = inputs
    cfg = AstPolicy.model_validate_json(config.read_bytes())
    # Keep the held position past the reversal decision and provide its later fill.
    cfg = cfg.model_copy(update={"execution": cfg.execution.model_copy(update={"session_exit_time": time(10, 25)})})
    result = replay.run_replay(path, snapshots, cfg)
    sessions = build_session_factory(engine)
    with sessions.begin() as session:
        session.add(AstCursor(id=1))
    frozen = freeze(
        sessions,
        [b for b in load_bars(path) if b.start.date() < START.date()],
        START.date(),
        START.replace(hour=9, minute=0),
        cfg,
    )
    assert result["sessions"][0]["parameters"] == frozen
    service = AstPaperService(sessions, settings(ast_config_json=cfg.model_dump_json()))
    for obs in tape:
        service.process_snapshot(capture(engine, obs))
    verify(sessions, full=True)
    with sessions() as session:
        expected = [trade_result(t) for t in session.scalars(select(AstTrade).order_by(AstTrade.decision_snapshot_id))]
        consumed = json.loads(session.get(AstCursor, 1).state_json)["consumed"]
    assert expected == result["trades"]
    filled = [t for t in expected if t["entry_timestamp"]]
    assert filled and [t["opportunity_id"] for t in filled] == consumed
    assert all(
        datetime.fromisoformat(t["entry_timestamp"]) - datetime.fromisoformat(t["decision_timestamp"])
        == timedelta(seconds=15)
        for t in filled
    )
    assert result["summary"]["exit_reason_counts"] == {"COMPLETED_5M_REVERSAL": 1}
    assert result["sessions"][0]["observations_at_or_after_exit_cutoff"] == 0
    assert all(t["lot_size"] == 65 for t in filled)


def test_selection_is_pre_session_only_and_raw_prefix_cannot_see_later_price(inputs, tmp_path, monkeypatch):
    path, snapshots, config, tape = inputs
    cfg = AstPolicy.model_validate_json(config.read_bytes())
    calls = []
    optimize = replay.optimize

    def audit(training, day, at, policy):
        assert at.time() < time(9, 15) and all(b.end < at and b.start.date() < day for b in training)
        assert len({b.start.date() for b in training}) == 15
        calls.append(policy.policy_hash)
        return optimize(training, day, at, policy)

    monkeypatch.setattr(replay, "optimize", audit)
    original = replay.run_replay(path, snapshots, cfg)
    altered = tmp_path / "partial.gz"
    write_snapshots(altered, tape[:230])
    partial = replay.run_replay(path, altered, cfg)
    assert calls == [cfg.policy_hash, cfg.policy_hash]  # one daily fit per run, no candidate-policy search
    assert original["sessions"][0]["parameters"] == partial["sessions"][0]["parameters"]
    for k in ("decision_timestamp", "entry_timestamp", "entry_price", "opportunity_id"):
        assert original["trades"][0][k] == partial["trades"][0][k]
    assert partial["summary"]["open_at_eof"] == 1 and partial["summary"]["closed_trades"] == 0
    assert partial["trades"][0]["exit_price"] is None and partial["trades"][0]["gross_inr"] is None


def test_missing_lot_and_gaps_block_fills_in_offline_tape(inputs, tmp_path):
    path, snapshots, config, tape = inputs
    cfg = AstPolicy.model_validate_json(config.read_bytes())
    # Remove lot evidence for every row. No synthetic fallback is allowed.
    missing = tmp_path / "missing.gz"
    write_snapshots(missing, [v.model_copy(update={"lot_size": None}) for v in tape[:200] if v not in tape[30:36]])
    result = replay.run_replay(path, missing, cfg)
    day = result["sessions"][0]
    assert day["decision_blockers_by_observation"]["FUTURES_LOT_SIZE_UNAVAILABLE_OR_MISMATCH"] == day["observations"]
    assert day["gap_count_above_policy_limit"] == 1
    assert result["summary"]["filled_trades"] == 0
    write_snapshots(missing, list(reversed(tape[:2])))
    with pytest.raises(ValueError, match="STRICT_OBSERVATION_ORDER"):
        replay.run_replay(path, missing, cfg)


def test_replay_requires_fifteen_sessions_policy_and_positive_explicit_lot(inputs):
    path, snapshots, _, _ = inputs
    with pytest.raises(ValueError, match="15_SESSION"):
        replay.run_replay(path, snapshots, policy())
    original = policy().model_dump(mode="json")
    for invalid in (None, 0, -1, True):
        with pytest.raises(ValueError):
            AstPolicy.model_validate({**original, "execution": {**original["execution"], "lot_size": invalid}})


def test_closed_only_summary_drawdown_and_rejections():
    def row(state, i, pnl, **fields):
        return {
            "state": state,
            "exit_timestamp": f"2026-11-16T10:0{i}" if state == "CLOSED" else None,
            "entry_timestamp": None if state == "ENTRY_REJECTED" else str(i),
            "gross_points": pnl,
            "gross_inr": pnl * 65 if pnl is not None else None,
            "exit_reason": "SESSION_END",
            **fields,
        }

    result = summary(
        [
            row("CLOSED", 1, 4),
            row("CLOSED", 2, -3),
            row("CLOSED", 3, -2),
            row("UNRESOLVED_EXPOSURE", 4, None),
            row("ENTRY_REJECTED", 5, None, rejection_reason="ENTRY_OBSERVATION_GAP"),
        ],
        3,
    )
    assert result["closed_trades"] == 3 and result["filled_trades"] == 4
    assert result["gross_points"] == -1 and result["gross_inr"] == -65
    assert result["profit_factor"] == 0.8 and result["win_rate"] == 1 / 3
    assert result["max_drawdown_points"] == 5 and result["max_drawdown_inr"] == 325
    assert result["average_win_points"] == 4 and result["average_loss_points"] == -2.5
    assert result["unresolved_trades"] == 1 and result["rejected_entries"] == 1


def test_duplicate_native_bucket_cannot_supply_next_fill(inputs, tmp_path):
    _, _, _, tape = inputs
    first = tape[0]
    path = tmp_path / "duplicate.gz"
    duplicate = first.model_copy(update={"captured_at": first.captured_at + timedelta(seconds=1)})
    write_snapshots(path, [first, duplicate])
    with pytest.raises(ValueError, match="DUPLICATE_NATIVE_CAPTURE_BUCKET"):
        list(replay.observations(path))


def test_missing_session_exit_carries_unpriced_exposure_into_next_raw_day(inputs, tmp_path):
    path, _, config, tape = inputs
    obs = tape[0]
    next_day = obs.model_copy(
        update={
            field: getattr(obs, field) + timedelta(days=1)
            for field in ("captured_at", "request_started_at", "response_received_at")
        }
    )
    snapshots = tmp_path / "missing_exit.gz"
    write_snapshots(snapshots, [*tape[:230], next_day])
    result = replay.run_replay(path, snapshots, AstPolicy.model_validate_json(config.read_bytes()))
    first, second = result["sessions"]
    assert first["end_of_data_position_state"] == "OPEN"
    assert first["observations_at_or_after_exit_cutoff"] == 0
    assert second["csv_session_present"] is False
    assert second["end_of_data_position_state"] == "UNRESOLVED_EXPOSURE"
    assert result["summary"]["closed_trades"] == 0
    assert result["trades"][0]["unresolved_reason"] == "MISSED_SESSION_EXIT"
    assert result["trades"][0]["gross_inr"] is None
