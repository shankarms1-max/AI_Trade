"""Deterministic replay coverage for Phase 15.1."""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import MarketSnapshotRecord
from app.paper.models import PaperTrade
from app.scalper.config import ScalperConfig
from app.scalper.models import (
    ScalperCursor,
    ScalperEvent,
    ScalperMarketSnapshot,
    ScalperMarketSnapshotRecord,
    ScalperOptionQuote,
    ScalperTrade,
)
from app.scalper.paper import cost_schedule
from app.scalper.replay import (
    ReplayRow,
    ScalperReplayEngine,
    apply_replay_overrides,
    load_replay_rows,
    write_replay_outputs,
)
from app.scalper.service import ScalperService

IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2099, 10, 8)


def settings(**updates):
    return Settings(
        _env_file=None,
        kotak_consumer_key="offline-replay",
        scalper_enabled=True,
        scalper_signal_min_score=75,
        scalper_min_confirmations=2,
        scalper_min_short_distance_points=0,
        scalper_max_loss_per_trade=50_000,
        **updates,
    )


def snapshot(index: int, *, direction: int = 1) -> ScalperMarketSnapshot:
    at = datetime(2099, 10, 1, 10, 0, tzinfo=IST) + timedelta(seconds=index * 15)
    spot = 25_000 + direction * index * 20
    quotes = []
    for strike in range(24_400, 25_601, 50):
        for option_type in ("CE", "PE"):
            premium = max(
                5.0,
                ((strike - 24_200) if option_type == "PE"
                 else (25_800 - strike)) * .1,
            )
            quotes.append(ScalperOptionQuote(
                expiry=EXPIRY,
                strike=strike,
                option_type=option_type,
                trading_symbol=f"NIFTY{strike}{option_type}",
                instrument_token=f"{strike}{option_type}",
                source_market_timestamp=at,
                bid=premium,
                ask=premium + 1,
                bid_quantity=1000,
                ask_quantity=1000,
                depth_unit="UNITS",
                tick_size=.05,
                ltp=premium + .5,
                volume=100 + index * 10,
                open_interest=(1000 + index * (
                    30 if (option_type == "PE") == (direction > 0) else 5)),
            ))
    return ScalperMarketSnapshot(
        captured_at=at,
        request_started_at=at - timedelta(seconds=1),
        response_received_at=at,
        source_market_timestamp=at,
        nifty_spot=spot,
        nifty_future=spot + direction * (15 + index * 4),
        india_vix=14,
        lot_size=50,
        atm_strike=round(spot / 50) * 50,
        expiry=EXPIRY,
        quotes=quotes,
    )


def rows(observations):
    return [ReplayRow(index + 1, raw) for index, raw in enumerate(observations)]


def engine(**setting_updates):
    configured = settings(**setting_updates)
    return ScalperReplayEngine(
        ScalperConfig.from_settings(configured), cost_schedule(configured))


def run(observations, **kwargs):
    return engine().run(
        rows(observations),
        run_id="deterministic-run",
        created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        **kwargs,
    )


def spread_prices(raw, trade, *, debit):
    short_strike = trade["short_strike"]
    long_strike = trade["long_strike"]
    option_type = "PE" if trade["direction"] == "BULL" else "CE"
    long_bid = 10.0
    short_ask = long_bid + debit
    quotes = []
    for quote in raw.quotes:
        if quote.option_type == option_type and quote.strike == short_strike:
            quote = quote.model_copy(update={
                "bid": short_ask - 1,
                "ask": short_ask,
            })
        elif quote.option_type == option_type and quote.strike == long_strike:
            quote = quote.model_copy(update={
                "bid": long_bid,
                "ask": long_bid + 1,
            })
        quotes.append(quote)
    return raw.model_copy(update={"quotes": quotes})


def shifted(raw, at):
    return raw.model_copy(update={
        "captured_at": at,
        "request_started_at": at - timedelta(seconds=1),
        "response_received_at": at,
        "source_market_timestamp": at,
        "quotes": [item.model_copy(update={"source_market_timestamp": at})
                   for item in raw.quotes],
    })


def entered_prefix(direction=1):
    observations = [snapshot(index, direction=direction) for index in range(4)]
    result = run(observations)
    assert result.summary["executed_trades"] == 1
    return observations, result.trades[0]


def test_repeated_replay_is_deterministic_and_manifest_dataset_hash_is_stable():
    observations = [snapshot(index) for index in range(6)]
    first = run(observations)
    second = run(observations)
    assert first.summary == second.summary
    assert first.trades == second.trades
    assert first.signal_timeline == second.signal_timeline
    assert first.manifest["dataset_hash"] == second.manifest["dataset_hash"]


def test_future_observations_do_not_change_replayed_prefix():
    observations = [snapshot(index) for index in range(7)]
    prefix = run(observations[:4])
    extended = run(observations)
    assert prefix.signal_timeline == extended.signal_timeline[:4]


def test_next_observation_fill_and_same_observation_fill_is_forbidden():
    result = run([snapshot(index) for index in range(4)])
    trade = result.trades[0]
    assert trade["decision_timestamp"] < trade["entry_timestamp"]
    decision_id = next(
        row["snapshot_id"] for row in result.signal_timeline
        if row["decision"] == "ENTRY_PENDING")
    entry_at = datetime.fromisoformat(trade["entry_timestamp"])
    entry_id = next(index + 1 for index in range(4)
                    if snapshot(index).captured_at == entry_at)
    assert entry_id > decision_id


@pytest.mark.parametrize("change,reason", [
    ({"source_market_timestamp": None}, "MISSING_QUOTE_TIMESTAMP"),
    ({"depth_unit": "UNKNOWN"}, "UNKNOWN_REQUIRED_DEPTH"),
])
def test_stale_or_bad_depth_rejects_pending_entry(change, reason):
    observations = [snapshot(index) for index in range(3)]
    bad = snapshot(3)
    bad = bad.model_copy(update={
        "quotes": [item.model_copy(update=change) for item in bad.quotes]})
    result = run([*observations, bad])
    assert result.trades[0]["state"] == "ENTRY_REJECTED"
    assert result.trades[0]["rejection_reason"] == reason
    assert any(reason in value
               for value in result.observation_quality[-1]["diagnostics"])


def test_stale_quote_age_rejects_pending_entry():
    observations = [snapshot(index) for index in range(3)]
    bad = snapshot(3)
    old = bad.captured_at - timedelta(seconds=31)
    bad = bad.model_copy(update={
        "quotes": [item.model_copy(update={"source_market_timestamp": old})
                   for item in bad.quotes]})
    result = run([*observations, bad])
    assert result.trades[0]["rejection_reason"] == "STALE_OR_FUTURE_QUOTE"
    assert "QUOTE_STALE_OR_FUTURE_QUOTE" in (
        result.observation_quality[-1]["diagnostics"])


def test_out_of_order_and_duplicate_snapshots_are_declared_unusable():
    one, two = snapshot(1), snapshot(2)
    out_of_order = engine().run([ReplayRow(2, two), ReplayRow(1, one)])
    assert any(
        "OUT_OF_ORDER_OBSERVATION" in item["fatal_reasons"]
        for item in out_of_order.observation_quality)
    duplicate = engine().run([ReplayRow(1, one), ReplayRow(2, one)])
    assert duplicate.summary["unusable_observations"] == 2
    assert all(item["status"] == "UNUSABLE"
               for item in duplicate.observation_quality)


@pytest.mark.parametrize("direction,strategy", [
    (1, "BULL_PUT_SPREAD"),
    (-1, "BEAR_CALL_SPREAD"),
])
def test_bullish_and_bearish_spreads_replay(direction, strategy):
    result = run([snapshot(index, direction=direction) for index in range(4)])
    assert result.trades[0]["strategy"] == strategy
    assert result.trades[0]["entry_timestamp"] is not None


@pytest.mark.parametrize("debit_multiple,reason", [
    (2.1, "SPREAD_STOP"),
    (0.1, "PROFIT_CAPTURE"),
])
def test_spread_stop_and_profit_capture(debit_multiple, reason):
    observations, trade = entered_prefix()
    final = spread_prices(snapshot(4), trade, debit=trade["entry_credit"] * debit_multiple)
    result = run([*observations, final])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == reason


def test_trailing_exit_and_excursion_calculation():
    observations, trade = entered_prefix()
    credit = trade["entry_credit"]
    # Scale the original 5/9 -> 7/9 path to the selected spread's actual credit.
    favorable_debit = round(credit * 5 / 9 / .05) * .05
    giveback_debit = round(credit * 7 / 9 / .05) * .05
    favorable = spread_prices(snapshot(4), trade, debit=favorable_debit)
    giveback = spread_prices(snapshot(5), trade, debit=giveback_debit)
    result = run([*observations, favorable, giveback])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == "TRAILING_EXIT"
    assert closed["mfe"] == pytest.approx(credit - favorable_debit)
    assert closed["mae"] == 0.0
    assert closed["minimum_spread_debit"] == pytest.approx(favorable_debit)
    assert closed["max_spread_debit"] == credit


def test_signal_failure_exit():
    observations, _ = entered_prefix()
    failing = snapshot(4).model_copy(update={
        "nifty_spot": 25_010,
        "nifty_future": 24_950,
        "atm_strike": 25_000,
    })
    result = run([*observations, failing])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == "SIGNAL_FAILURE"


def test_structural_invalidation_exit():
    observations, _ = entered_prefix()
    failing = snapshot(4).model_copy(update={
        "nifty_spot": 24_999,
        "nifty_future": 25_010,
        "atm_strike": 25_000,
    })
    result = run([*observations, failing])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == "STRUCTURE_FAILURE"


def test_time_exit():
    configured = settings(scalper_time_stop_minutes=1)
    replay = ScalperReplayEngine(
        ScalperConfig.from_settings(configured), cost_schedule(configured))
    observations = [snapshot(index) for index in range(9)]
    result = replay.run(rows(observations))
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == "TIME_STOP"


def test_forced_close():
    observations, _ = entered_prefix()
    at = snapshot(4).captured_at.replace(hour=15, minute=20)
    result = run([*observations, shifted(snapshot(4), at)])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["exit_reason"] == "FORCED_INTRADAY_CLOSE"


def test_gross_pnl_mae_mfe_and_gross_only_accounting():
    observations, trade = entered_prefix()
    result = run([*observations, spread_prices(snapshot(4), trade, debit=1.0)])
    closed = next(item for item in result.trades if item["state"] == "CLOSED")
    assert closed["gross_points"] == trade["entry_credit"] - 1.0
    assert closed["gross_rupees"] == (trade["entry_credit"] - 1.0) * 50
    assert closed["mfe"] == trade["entry_credit"] - 1.0 and closed["mae"] == 0.0
    assert closed["net_rupees"] is None
    assert closed["accounting_status"] == "GROSS_ONLY"


def test_parameter_override_is_isolated_and_changes_policy_hash():
    base = ScalperConfig.from_settings(settings())
    changed, normalized = apply_replay_overrides(
        base, {"SCALPER_SIGNAL_MIN_SCORE": 85})
    assert base.signal_min_score == 75
    assert changed.signal_min_score == 85
    assert normalized == {"SCALPER_SIGNAL_MIN_SCORE": 85}
    observations = [snapshot(index) for index in range(5)]
    authoritative = run(observations)
    experimental = run(
        observations, overrides={"SCALPER_SIGNAL_MIN_SCORE": 85})
    assert authoritative.manifest["base_policy_hash"] == (
        experimental.manifest["base_policy_hash"])
    assert authoritative.manifest["policy_hash"] != (
        experimental.manifest["policy_hash"])
    assert experimental.manifest["mode"] == "NON_AUTHORITATIVE_RESEARCH"


def test_validation_period_is_labeled_out_of_sample():
    result = run(
        [snapshot(index) for index in range(3)],
        split_label="OUT_OF_SAMPLE",
    )
    assert result.manifest["split_label"] == "OUT_OF_SAMPLE"


def test_outputs_are_complete_and_immutable(tmp_path):
    result = run([snapshot(index) for index in range(5)])
    paths = write_replay_outputs(result, tmp_path / "replay")
    assert set(paths) == {
        "trades", "summary", "manifest", "observation_quality",
        "signal_timeline", "daily_summary",
    }
    assert json.loads((tmp_path / "replay/manifest.json").read_text())["run_id"]
    with pytest.raises(FileExistsError, match="OUTPUT_EXISTS"):
        write_replay_outputs(result, tmp_path / "replay")


def test_live_stored_signal_comparison_and_replay_are_read_only(tmp_path):
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'source.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(ScalperCursor(id=1))
    configured = settings()
    live = ScalperService(sessions, configured)
    for index in range(5):
        live.capture_once(snapshot(index))
    with sessions() as session:
        before = SimpleNamespace(
            snapshots=session.scalar(select(func.count(ScalperMarketSnapshotRecord.id))),
            trades=session.scalar(select(func.count(ScalperTrade.id))),
            events=session.scalar(select(func.count(ScalperEvent.sequence))),
            phase14=session.scalar(select(func.count(MarketSnapshotRecord.id))),
            paper=session.scalar(select(func.count(PaperTrade.id))),
        )
    loaded = load_replay_rows(sessions, date(2099, 10, 1), date(2099, 10, 1))
    result = ScalperReplayEngine(
        ScalperConfig.from_settings(configured), cost_schedule(configured)).run(loaded)
    assert result.manifest["base_policy_hash"] == live.engine.policy_hash
    assert result.manifest["policy_hash"] == live.engine.policy_hash
    assert all(
        item["LIVE_STORED_VS_REPLAY_FEATURE_MATCH"] is True
        for item in result.signal_timeline)
    assert all(
        item["LIVE_STORED_VS_REPLAY_SIGNAL_MATCH"] is True
        for item in result.signal_timeline)
    assert all(
        item["LIVE_STORED_VS_REPLAY_CANDIDATE_PRESENCE_MATCH"] is True
        for item in result.signal_timeline)
    assert any(
        item["LIVE_STORED_VS_REPLAY_TRADE_DECISION_MATCH"] is True
        for item in result.signal_timeline)
    with sessions() as session:
        after = SimpleNamespace(
            snapshots=session.scalar(select(func.count(ScalperMarketSnapshotRecord.id))),
            trades=session.scalar(select(func.count(ScalperTrade.id))),
            events=session.scalar(select(func.count(ScalperEvent.sequence))),
            phase14=session.scalar(select(func.count(MarketSnapshotRecord.id))),
            paper=session.scalar(select(func.count(PaperTrade.id))),
        )
    assert before == after
    assert after.phase14 == 0 and after.paper == 0
    database.dispose()


def test_cli_generates_required_artifacts(tmp_path):
    database_path = tmp_path / "cli-source.db"
    database = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(ScalperCursor(id=1))
    live = ScalperService(sessions, settings())
    for index in range(5):
        live.capture_once(snapshot(index))
    database.dispose()

    output = tmp_path / "cli-output"
    environment = os.environ | {
        "KOTAK_CONSUMER_KEY": "offline-replay",
        "SCALPER_SIGNAL_MIN_SCORE": "75",
        "SCALPER_MIN_CONFIRMATIONS": "2",
        "SCALPER_MIN_SHORT_DISTANCE_POINTS": "0",
        "SCALPER_MAX_LOSS_PER_TRADE": "50000",
    }
    project = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(project / "scripts/run_scalper_replay.py"),
        "--start", "2099-10-01",
        "--end", "2099-10-01",
        "--database-url", f"sqlite+pysqlite:///{database_path}",
        "--output", str(output),
        "--json",
    ]
    completed = subprocess.run(
        command,
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["runs"][0]["summary"]["observations"] == 5
    assert {path.name for path in output.iterdir()} == {
        "trades.csv", "summary.json", "manifest.json", "observation_quality.csv",
        "signal_timeline.csv", "daily_summary.csv",
    }
