"""Deterministic, labelled synthetic tests for the read-only four-strategy lab."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import csv
import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DatabaseError

from app.scalper.config import ScalperConfig
from app.scalper.models import (ScalperCandidate, ScalperMarketSnapshotRecord,
                                ScalperOptionQuoteRecord)
from app.scalper.paper import cost_schedule
from app.scalper_lab import STRATEGY_IDS
from app.scalper_lab.context import (FUTURES_BASIS, ResearchObservation,
                                     context_at)
from app.scalper_lab.engine import (LabTrade, Ledger, ScalperLab, _candidate,
                                    _exit_reason, _risk_blockers)
from app.scalper_lab.io import (load_futures_csv, load_local_sqlite,
                                load_postgresql, write_result)
from app.scalper_lab.strategies import decide
from tests.test_scalper import settings
from tests.test_scalper_trend import trend_history


def observed(count=70, direction=1):
    rows = []
    for index, raw in enumerate(trend_history(direction, count)):
        future = raw.nifty_future
        evidence = {"symbol": raw.future_instrument_id,
                    "expiry": raw.future_expiry.isoformat(),
                    "instrument_token": "53001",
                    "ltp": future,
                    "cumulative_volume": 100000 + index * 100,
                    "vwap": future - direction * 10,
                    "source_timestamp": raw.captured_at.isoformat(),
                    "basis": FUTURES_BASIS,
                    "source_fields": ["avg_cost", "last_volume", "lstup_time"]}
        rows.append(ResearchObservation(index + 1, raw, evidence))
    return rows


def context(rows):
    return context_at(rows[-1], rows[:-1])


def test_authoritative_futures_volume_and_provenance_are_mandatory():
    rows = observed()
    good = context(rows)
    assert good["vwap"]["status"] == "AVAILABLE"
    assert good["vwap"]["slope_bps_per_minute"] > 0
    assert good["regime"] == "BULLISH"
    for mutation, reason in (
        ({"cumulative_volume": None}, "MISSING_FUTURES_VOLUME"),
        ({"cumulative_volume": 0}, "MISSING_FUTURES_VOLUME"),
        ({"basis": "SAMPLED_LTP_TWAP"}, "MISSING_AUTHORITATIVE_VWAP"),
        ({"source_fields": []}, "UNVERIFIED_VWAP_PROVENANCE"),
        ({"symbol": "OTHER-FUTURE"}, "FUTURES_IDENTITY_MISMATCH"),
        ({"vwap": None}, "MISSING_AUTHORITATIVE_VWAP"),
    ):
        missing = rows[-1]
        altered = replace(missing, futures={**missing.futures, **mutation})
        evidence = context_at(altered, rows[:-1])
        assert evidence["vwap"]["status"] == reason
        assert evidence["vwap"]["value"] is None
        for strategy in ("VWAP_OI_REJECTION", "ORB_RETEST"):
            decision = decide(strategy, evidence, {}, len(rows) - 1)
            assert decision["status"] == "NO_SETUP"
            assert decision["blockers"]


def test_companion_futures_csv_accepts_only_explicit_broker_fields(tmp_path):
    row = observed(count=1)[0]
    path = tmp_path / "futures.csv"
    fields = ["timestamp", "source_timestamp", "symbol", "expiry",
              "instrument_token", "ltp", "cumulative_volume", "vwap",
              "basis", "source_fields"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"timestamp": row.snapshot.captured_at.isoformat(),
                         **{field: (json.dumps(value) if field == "source_fields" else value)
                            for field, value in row.futures.items()}})
    loaded = load_futures_csv(path)
    assert loaded[row.snapshot.captured_at.isoformat()]["cumulative_volume"] == 100000
    duplicate = path.read_text()
    path.write_text(duplicate + duplicate.splitlines()[-1] + "\n")
    with pytest.raises(ValueError, match="DUPLICATE_FUTURES_TIMESTAMP"):
        load_futures_csv(path)


def test_exact_contract_delta_oi_and_premium_behavior_over_all_horizons():
    rows = observed()
    options = context(rows)["options"]
    put = next(item for item in options["contracts"] if item["option_type"] == "PE")
    assert put["delta_oi"] == {"1m": 120, "3m": 360, "5m": 600, "15m": 1800}
    assert set(put["activity"].values()) == {"WRITING"}
    assert options["oi_basis"] == "SELF_COMPUTED_EXACT_CONTRACT_HISTORY"
    current = rows[-1]
    changed = current.snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"instrument_token": "replacement"}) for q in current.snapshot.quotes]})
    altered = replace(current, snapshot=changed)
    options = context_at(altered, rows[:-1])["options"]
    assert all(set(row["delta_oi"].values()) == {None} for row in options["contracts"])
    assert all(set(pcr["oi_change"].values()) == {None} for pcr in options["local_pcr"].values())
    stale = current.snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"source_market_timestamp":
                             current.snapshot.captured_at - timedelta(seconds=31)})
        for q in current.snapshot.quotes]})
    options = context_at(replace(current, snapshot=stale), rows[:-1])["options"]
    assert options["contracts"] == [] and options["local_pcr"]["3"]["oi"] is None


def test_local_pcr_oi_volume_change_and_elapsed_slopes_use_current_contract_basket():
    rows = observed()
    rows = [replace(row, snapshot=row.snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"volume": 100 + index * (10 if q.option_type == "PE" else 5)})
        for q in row.snapshot.quotes]})) for index, row in enumerate(rows)]
    data = context(rows)["options"]["local_pcr"]
    for radius in ("3", "5"):
        pcr = data[radius]
        assert pcr["complete"]
        current = (100 + 69 * 10) / (100 + 69 * 5)
        assert pcr["volume"] == pytest.approx(current)
        for label, minutes in (("1m", 1), ("3m", 3), ("5m", 5)):
            index = 69 - minutes * 4
            old = (100 + index * 10) / (100 + index * 5)
            assert pcr["baseline_volume"][label] == pytest.approx(old)
            assert pcr["volume_change"][label] == pytest.approx(current - old)
            assert pcr["volume_slope_per_minute"][label] == pytest.approx((current - old) / minutes)
            assert pcr["oi_slope_per_minute"][label] == pytest.approx(
                pcr["oi_change"][label] / minutes)
            assert pcr["baseline_oi"][label] is not None


def test_wall_strengthening_weakening_and_premium_oi_classification():
    rows = observed()
    good = context(rows)
    wall = good["options"]["walls"]["put_support"]
    assert wall["wall_state"]["1m"] == "STRENGTHENING"
    assert good["options"]["classification_by_direction"]["BULL"] == "SUPPORTIVE"
    current = rows[-1]
    weaker = current.snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"open_interest": 500}) for q in current.snapshot.quotes]})
    changed = context_at(replace(current, snapshot=weaker), rows[:-1])
    assert changed["options"]["walls"]["put_support"]["wall_state"]["1m"] == "WEAKENING"
    premium_up = current.snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"bid": q.bid + 2, "ask": q.ask + 2})
        if q.option_type == "PE" else q for q in current.snapshot.quotes]})
    changed = context_at(replace(current, snapshot=premium_up), rows[:-1])
    assert changed["options"]["classification_by_direction"]["BULL"] == "CONTRADICTORY"


def test_ambiguous_strike_cannot_double_count_pcr_or_create_a_spread():
    rows = observed()
    raw = rows[-1].snapshot
    atm = next(q for q in raw.quotes if q.strike == raw.atm_strike
               and q.option_type == "PE")
    duplicate = atm.model_copy(update={"instrument_token": "other-token",
                                       "trading_symbol": "other-symbol"})
    changed = raw.model_copy(update={"quotes": [*raw.quotes, duplicate]})
    current = replace(rows[-1], snapshot=changed)
    ctx = context_at(current, rows[:-1])
    assert ctx["options"]["local_pcr"]["3"]["oi"] is None
    decision = {"direction": "BULL"}
    candidate, rejected = _candidate(changed, decision, ctx,
                                     ScalperConfig.from_settings(settings()))
    assert candidate is None and rejected == {"AMBIGUOUS_OPTION_STRIKE": 1}


def test_vwap_pullback_rejection_and_bearish_mirror():
    for direction, name in ((1, "BULL"), (-1, "BEAR")):
        rows = observed(direction=direction)
        ctx = context(rows)
        state = {}
        first = decide("VWAP_OI_REJECTION", ctx, state, 69)
        assert first["status"] == "WATCH" and first["direction"] == name
        resumed = {**ctx, "future": ctx["future"] + direction * 3,
                   "fast_direction": name,
                   "timestamp": (rows[-1].snapshot.captured_at + timedelta(seconds=15)).isoformat()}
        second = decide("VWAP_OI_REJECTION", resumed, state, 70)
        assert second["status"] == "ENTER"
        assert second["trigger"] == "VWAP_PULLBACK_REJECTION_FAST_RESUMPTION"


def test_orb_breakout_retest_then_fast_resumption():
    ctx = context(observed())
    assert ctx["structure"]["opening_range_complete"]
    line = ctx["opening_high"]
    state = {}
    breakout = decide("ORB_RETEST", {**ctx, "spot": line + 6}, state, 69)
    assert breakout["status"] == "WATCH" and state["stage"] == "BREAKOUT"
    retest = decide("ORB_RETEST", {**ctx, "spot": line + 2}, state, 70)
    assert retest["status"] == "WATCH" and state["stage"] == "RETEST"
    entered = decide("ORB_RETEST", {**ctx, "spot": line + 6,
                                    "fast_direction": "BULL"}, state, 71)
    assert entered["status"] == "ENTER"
    assert entered["trigger"] == "ORB_BREAKOUT_RETEST_FAST_RESUMPTION"


def test_oi_wall_rejection_and_break_continuation():
    ctx = context(observed())
    put = ctx["options"]["walls"]["put_support"]
    state = {}
    first = decide("OI_WALL", {**ctx, "spot": put["strike"] + 2}, state, 69)
    assert first["status"] == "WATCH"
    second = decide("OI_WALL", {**ctx, "spot": put["strike"] + 5,
                                "fast_direction": "BULL"}, state, 70)
    assert second["status"] == "ENTER" and "REJECTION" in second["trigger"]

    call = ctx["options"]["walls"]["call_resistance"]
    weakened = {**call, "wall_state": {**call["wall_state"], "1m": "WEAKENING"},
                "activity": {**call["activity"], "1m": "SHORT_COVERING"}}
    walls = {**ctx["options"]["walls"], "call_resistance": weakened}
    options = {**ctx["options"], "walls": walls}
    state = {}
    first = decide("OI_WALL", {**ctx, "options": options,
                               "spot": call["strike"] - 2}, state, 69)
    assert first["status"] == "WATCH" and state["stage"] == "BREAK"
    second = decide("OI_WALL", {**ctx, "options": options,
                                "spot": call["strike"] + 6,
                                "fast_direction": "BULL"}, state, 70)
    assert second["status"] == "ENTER" and "BREAK" in second["trigger"]


def test_trend_pullback_requires_5m_15m_then_fast_resumption_without_vwap():
    ctx = context(observed())
    missing = {**ctx, "vwap": {**ctx["vwap"], "status": "MISSING_FUTURES_VOLUME",
                               "value": None}}
    state = {}
    pullback = {**missing, "spot": ctx["spot"] - 5,
                "structure": {**ctx["structure"], "returns_bps": {
                    **ctx["structure"]["returns_bps"], "1m": -3}}}
    first = decide("TREND_PULLBACK", pullback, state, 69)
    assert first["status"] == "WATCH"
    second = decide("TREND_PULLBACK", {**ctx, "spot": pullback["spot"] + 4,
                                       "fast_direction": "BULL"}, state, 70)
    assert second["status"] == "ENTER" and second["setup_anchor"] == pullback["spot"]


def test_thesis_exits_ignore_one_noisy_fast_flip_and_respect_hard_stops():
    rows = observed()
    ctx = context(rows)
    config = ScalperConfig.from_settings(settings())
    lab = ScalperLab(config, cost_schedule(settings()))
    # Use an actual isolated research trade to inspect mark behavior.
    result = lab.run(rows)
    entered = next(t for t in result.trades if t["entry_timestamp"] is not None)
    candidate = ScalperCandidate.model_validate(entered["candidate"])
    trade = LabTrade("OI_WALL", candidate, entered["decision"],
                     entered["decision_snapshot_id"], state="OPEN",
                     entry_credit=20, entry_timestamp=rows[-1].snapshot.captured_at)
    noisy = {**ctx, "fast_direction": "BEAR"}
    assert _exit_reason(trade, noisy, 20, config, event_blocked=False) is None
    assert _exit_reason(trade, noisy, 31, config, event_blocked=False) == "SPREAD_STOP"
    assert _exit_reason(trade, noisy, 20, replace(config, kill_switch=True),
                        event_blocked=False) == "KILL_SWITCH"
    assert _exit_reason(trade, noisy, 20, config, event_blocked=True) == "MARKET_EVENT_EXIT"


def _held(strategy, ctx, rows):
    decision = {"strategy_id": strategy, "timestamp": ctx["timestamp"],
                "direction": "BULL", "regime": ctx["regime"],
                "thesis": {"wall": ctx["options"]["walls"]["put_support"],
                           "opening_high": ctx["opening_high"],
                           "opening_low": ctx["opening_low"],
                           "pullback_anchor": ctx["spot"] - 10}}
    candidate, _ = _candidate(rows[-1].snapshot, decision, ctx,
                              ScalperConfig.from_settings(settings()))
    assert candidate is not None
    return LabTrade(strategy, candidate, decision, rows[-1].snapshot_id,
                    state="OPEN", entry_credit=20,
                    entry_timestamp=rows[-1].snapshot.captured_at)


def _weakened_wall(ctx, trade):
    wall = trade.decision["thesis"]["wall"]
    contracts = [{**row, "open_interest": 500,
                  "delta_oi": {**row["delta_oi"], "1m": -1000}}
                 if row["identity"] == wall["identity"] else row
                 for row in ctx["options"]["contracts"]]
    return {**ctx, "options": {**ctx["options"], "contracts": contracts}}


def test_each_strategy_exits_on_its_own_thesis_without_holding_delay():
    rows = observed()
    ctx = context(rows)
    config = ScalperConfig.from_settings(settings())

    vwap = _held("VWAP_OI_REJECTION", ctx, rows)
    adverse = {**ctx, "future": ctx["vwap"]["value"] - 1}
    assert _exit_reason(vwap, adverse, 20, config, event_blocked=False) is None
    assert _exit_reason(vwap, adverse, 20, config,
                        event_blocked=False) == "SUSTAINED_VWAP_RECLAIM"
    wall = _held("VWAP_OI_REJECTION", ctx, rows)
    assert _exit_reason(wall, _weakened_wall(ctx, wall), 20, config,
                        event_blocked=False) == "OI_WALL_UNWIND"

    orb = _held("ORB_RETEST", ctx, rows)
    broken = {**ctx, "spot": ctx["opening_high"] - 6}
    assert _exit_reason(orb, broken, 20, config,
                        event_blocked=False) == "ORB_STRUCTURE_FAILURE"
    orb = _held("ORB_RETEST", ctx, rows)
    reentry = {**ctx, "spot": ctx["opening_high"] - 1}
    assert _exit_reason(orb, reentry, 20, config, event_blocked=False) is None
    assert _exit_reason(orb, reentry, 20, config,
                        event_blocked=False) == "SUSTAINED_ORB_REENTRY"

    wall = _held("OI_WALL", ctx, rows)
    assert _exit_reason(wall, _weakened_wall(ctx, wall), 20, config,
                        event_blocked=False) == "OI_WALL_UNWIND"
    wall = _held("OI_WALL", ctx, rows)
    wall.decision["thesis"]["failed_wall"] = ctx["spot"] - 2
    reentered = {**ctx, "spot": ctx["spot"] - 8}
    assert _exit_reason(wall, reentered, 20, config,
                        event_blocked=False) == "WALL_BREAK_REENTRY"
    wall = _held("OI_WALL", ctx, rows)
    row = wall.decision["thesis"]["wall"]
    flipped = {**ctx, "options": {**ctx["options"], "contracts": [
        {**contract, "activity": {**contract["activity"], "1m": "LONG_BUILDUP"}}
        if contract["identity"] == row["identity"] else contract
        for contract in ctx["options"]["contracts"]]}}
    assert _exit_reason(wall, flipped, 20, config, event_blocked=False) is None
    assert _exit_reason(wall, flipped, 20, config,
                        event_blocked=False) == "PREMIUM_OI_FLIP"

    trend = _held("TREND_PULLBACK", ctx, rows)
    reversal = {**ctx, "futures_returns_bps": {**ctx["futures_returns_bps"],
                                               "5m": -5, "15m": -9}}
    assert _exit_reason(trend, reversal, 20, config,
                        event_blocked=False) == "TREND_REVERSAL"
    assert _exit_reason(trend, ctx, 31, config, event_blocked=False) == "SPREAD_STOP"
    assert _exit_reason(trend, ctx, 50,
                        replace(config, stop_credit_multiple=100,
                                max_loss_per_trade=1000),
                        event_blocked=False) == "HARD_MAX_LOSS"


def test_all_four_replay_ledgers_are_independent_and_deterministic(monkeypatch):
    rows = observed(count=70)

    def setup(strategy, ctx, state, index):
        return {"strategy_id": strategy, "timestamp": ctx["timestamp"],
                "status": "ENTER" if index == 65 else "NO_SETUP",
                "direction": "BULL", "trigger": "SYNTHETIC_TEST_SETUP",
                "regime": ctx["regime"], "vwap": ctx["vwap"],
                "walls": ctx["options"]["walls"], "local_pcr": ctx["options"]["local_pcr"],
                "positioning": ctx["options"]["classification_by_direction"],
                "delta_oi_reference": ctx["options"]["reference_timestamps"],
                "blockers": [], "watch_state": {},
                "thesis": {"strategy_id": strategy, "direction": "BULL",
                           "regime": ctx["regime"], "timing_trigger": "TEST",
                           "wall": ctx["options"]["walls"]["put_support"],
                           "vwap": ctx["vwap"], "local_pcr": ctx["options"]["local_pcr"],
                           "positioning_classification": "SUPPORTIVE",
                           "short_leg_reason": "TEST", "hedge_reason": "TEST",
                           "invalidation_conditions": []} if index == 65 else None}

    monkeypatch.setattr("app.scalper_lab.engine.decide", setup)
    lab = ScalperLab(ScalperConfig.from_settings(settings()), cost_schedule(settings()))
    first = lab.run(rows)
    second = lab.run(rows)
    assert first == second
    assert {trade["strategy_id"] for trade in first.trades} == set(STRATEGY_IDS)
    assert all(first.summaries[name]["executed_trades"] == 1 for name in STRATEGY_IDS)
    assert all(trade["candidate"]["construction_method"] == "SHORT_LEG_FIRST_V1"
               for trade in first.trades)
    assert all(trade["candidate"]["spread_width"] in (100, 200, 300, 400)
               for trade in first.trades)
    assert all(trade["outcome"] is None or trade["outcome"]["net_rupees"] is None
               for trade in first.trades)
    prefix = lab.run(rows[:65])
    assert prefix.contexts == first.contexts[:65]
    assert prefix.decisions == first.decisions[:65 * 4]


def test_risk_gates_stay_independent_of_legacy_score_and_book_restrictions():
    rows = observed()
    ctx = context(rows)
    config = ScalperConfig.from_settings(settings())
    decision = decide("OI_WALL", {**ctx, "spot": ctx["options"]["walls"]["put_support"]["strike"] + 2}, {}, 69)
    decision["direction"] = "BULL"
    candidate, _ = _candidate(rows[-1].snapshot, decision, ctx, config)
    assert candidate is not None
    assert candidate.defined_max_loss_per_lot <= config.max_loss_per_trade
    assert _risk_blockers(Ledger(), replace(config, signal_min_score=100),
                          rows[-1].snapshot.captured_at, False) == []
    assert "KILL_SWITCH_ACTIVE" in _risk_blockers(
        Ledger(), replace(config, kill_switch=True), rows[-1].snapshot.captured_at, False)
    assert "MARKET_EVENT_BLOCKED" in _risk_blockers(
        Ledger(), config, rows[-1].snapshot.captured_at, True)
    # The existing executable builder still enforces actual bid/ask and depth.
    invalid = rows[-1].snapshot.model_copy(update={"quotes": [
        q.model_copy(update={"bid_quantity": 0}) for q in rows[-1].snapshot.quotes]})
    assert _candidate(invalid, decision, ctx, config)[0] is None


def test_read_only_sqlite_input_and_new_artifacts_only(engine, tmp_path: Path):
    raw = observed(count=2)[0].snapshot
    metadata = {**raw.model_dump(exclude={"quotes"}), "id": 1,
                "capture_key": "x" * 64, "feature_json": "{}", "signal_json": "{}"}
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [metadata])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": 1} for quote in raw.quotes])
    database = Path(engine.url.database)
    loaded = load_local_sqlite(database, raw.captured_at.date(), raw.captured_at.date())
    assert len(loaded) == 1 and loaded[0].snapshot_id == 1
    assert loaded[0].futures is None
    output = tmp_path / "research"
    result = ScalperLab(ScalperConfig.from_settings(settings()),
                        cost_schedule(settings())).run(loaded)
    files = write_result(result, output)
    assert len(files) == 7
    assert len((output / "decisions.jsonl").read_text().splitlines()) == 4
    assert len(json.loads((output / "summary.json").read_text())) == 4
    with pytest.raises(FileExistsError):
        write_result(result, output)
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden_write (id INTEGER)")


@pytest.mark.skipif(not os.getenv("PHASE15_POSTGRES_TEST_URL"),
                    reason="PHASE15_POSTGRES_TEST_URL is required for PostgreSQL integration")
def test_postgresql_and_sqlite_load_equivalent_observations(engine, tmp_path, monkeypatch):
    schema = f"scalper_lab_{uuid4().hex}"
    base_url = make_url(os.environ["PHASE15_POSTGRES_TEST_URL"])
    scoped_url = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
    admin = create_engine(base_url)
    postgres = create_engine(scoped_url)
    rows = observed(count=3)
    last = rows[2].snapshot
    outside = last.model_copy(update={"captured_at": last.captured_at + timedelta(days=1)})
    fixtures = [(2, rows[1].snapshot), (1, rows[0].snapshot), (3, outside)]
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        ScalperMarketSnapshotRecord.__table__.create(postgres)
        ScalperOptionQuoteRecord.__table__.create(postgres)
        for target in (engine, postgres):
            with target.begin() as connection:
                for identity, snapshot in fixtures:
                    connection.execute(insert(ScalperMarketSnapshotRecord), [{
                        **snapshot.model_dump(exclude={"quotes"}), "id": identity,
                        "capture_key": f"lab-{identity}",
                        "feature_json": "{}", "signal_json": "{}"}])
                    connection.execute(insert(ScalperOptionQuoteRecord), [
                        {**quote.model_dump(), "scalper_snapshot_id": identity}
                        for quote in snapshot.quotes])
        day = rows[0].snapshot.captured_at.date()
        futures = {rows[0].snapshot.captured_at.isoformat(): rows[0].futures}
        sqlite = load_local_sqlite(Path(engine.url.database), day, day, futures)
        url = scoped_url.render_as_string(hide_password=False)
        with postgres.connect() as connection:
            before = (connection.execute(select(ScalperMarketSnapshotRecord)).all(),
                      connection.execute(select(ScalperOptionQuoteRecord)).all())
        loaded = load_postgresql(url, day, day, futures)
        assert loaded == sqlite
        assert [row.snapshot_id for row in loaded] == [1, 2]
        assert loaded[0].futures == rows[0].futures
        assert loaded[1].futures is None
        with postgres.connect() as connection:
            after = (connection.execute(select(ScalperMarketSnapshotRecord)).all(),
                     connection.execute(select(ScalperOptionQuoteRecord)).all())
        assert after == before

        def forbidden_write(session, *_):
            session.execute(text("UPDATE scalper_market_snapshots SET source = 'MUTATED'"))

        with monkeypatch.context() as patch:
            patch.setattr("app.scalper_lab.io._load_observations", forbidden_write)
            with pytest.raises(DatabaseError, match="read-only transaction"):
                load_postgresql(url, day, day)
        with postgres.connect() as connection:
            assert connection.execute(select(ScalperMarketSnapshotRecord)).all() == before[0]

        root = Path(__file__).resolve().parents[2]
        command = [sys.executable, str(root / "scripts/run_scalper_lab.py"),
                   "--start", day.isoformat(), "--end", day.isoformat()]
        for argument, env, output in (
            (["--database-url", url], os.environ.copy(), tmp_path / "postgres-explicit"),
            ([], {**os.environ, "DATABASE_URL": url}, tmp_path / "postgres-env"),
        ):
            result = subprocess.run(command + argument + ["--output", str(output)],
                                    env=env, capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["periods"][0]["observations"] == 2
    finally:
        postgres.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_future_observation_is_rejected_before_feature_calculation():
    rows = observed(count=2)
    with pytest.raises(ValueError, match="LAB_FUTURE_OBSERVATION"):
        context_at(rows[0], rows[1:])


def test_cli_direct_and_chronological_validation_use_local_read_only_file(engine, tmp_path):
    raw = observed(count=1)[0].snapshot
    metadata = {**raw.model_dump(exclude={"quotes"}), "id": 1,
                "capture_key": "y" * 64, "feature_json": "{}", "signal_json": "{}"}
    with engine.begin() as connection:
        connection.execute(insert(ScalperMarketSnapshotRecord), [metadata])
        connection.execute(insert(ScalperOptionQuoteRecord), [
            {**quote.model_dump(), "scalper_snapshot_id": 1} for quote in raw.quotes])
    root = Path(__file__).resolve().parents[2]
    base = [sys.executable, str(root / "scripts/run_scalper_lab.py"),
            "--database", str(engine.url.database)]
    day = raw.captured_at.date().isoformat()
    direct = subprocess.run(base + ["--start", day, "--end", day,
                                    "--output", str(tmp_path / "direct")],
                            capture_output=True, text=True, check=False)
    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout)["periods"][0]["observations"] == 1
    split = subprocess.run(base + ["--research-start", day,
                                   "--research-end", day,
                                   "--validation-start", "2099-10-02",
                                   "--validation-end", "2099-10-02",
                                   "--output", str(tmp_path / "split")],
                           capture_output=True, text=True, check=False)
    assert split.returncode == 0, split.stderr
    periods = json.loads(split.stdout)["periods"]
    assert [(row["period"], row["observations"]) for row in periods] == [
        ("RESEARCH", 1), ("OUT_OF_SAMPLE_VALIDATION", 0)]
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM scalper_market_snapshots").scalar_one() == 1
