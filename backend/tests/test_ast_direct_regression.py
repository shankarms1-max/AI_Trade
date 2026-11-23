"""Frozen AST_FUTURES_V1_22_39 decade-audit regression mechanics with deterministic Angel 1-minute fixtures."""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from app.adaptive_supertrend.direct import regression as harness
from app.adaptive_supertrend.direct.policy import DirectPolicy
from app.adaptive_supertrend.direct.regression import (
    RESEARCH_INSTRUMENT,
    angel_sessions,
    compare_frozen_ledger,
    compare_summary,
    load_frozen_ledger,
    replay_sessions,
    summarize,
)
from app.adaptive_supertrend.indicators import supertrend
from app.scalper.repository import IST

POLICY = DirectPolicy(lot_size=65)
LEDGER_HEADER = ["atr_period", "multiplier", "date", "direction", "entry_at", "exit_at", "entry", "exit", "points",
                 "exit_reason"]


def weekdays(count, start=date(2025, 1, 6)):
    result, day = [], start
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


def uptrend(day_index, minute):
    """Steady rise with tiny alternating noise: AST stays BULL, so an open trade can only end at 15:20."""
    base = 20000 + (day_index * 375 + minute) * 0.5
    noise = 0.2 if minute % 2 else -0.2
    o, c = base, base + 0.5 + noise
    return o, max(o, c) + 0.3, min(o, c) - 0.3, c


def reversal(day_index, minute):
    """Trend until 11:45, then a steeper reversal: AST flips, the opposite breakout re-enters, 15:20 closes it."""
    sign = 1 if day_index % 2 == 0 else -1
    turn = 150
    move = minute * 0.6 if minute < turn else turn * 0.6 - (minute - turn) * 1.2
    base = 20000 + sign * move
    o, c = base, base + sign * (0.6 if minute < turn else -1.2) + (0.3 if minute % 3 else -0.3)
    return o, max(o, c) + 0.4, min(o, c) - 0.4, c


def write_angel(path, days, price=uptrend, *, fmt="naive", mutate=None):
    rows = []
    for d, day in enumerate(days):
        for minute in range(375):
            at = datetime.combine(day, time(9, 15)) + timedelta(minutes=minute)
            o, h, low, c = price(d, minute)
            rows.append([at, round(o, 2), round(h, 2), round(low, 2), round(c, 2)])
    if mutate:
        rows = mutate(rows)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        if fmt == "split":
            writer.writerow(["date", "time", "open", "high", "low", "close", "volume"])
        else:
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for at, *values in rows:
            if fmt == "split":
                writer.writerow([at.date().isoformat(), at.time().isoformat(), *values, 0])
            elif fmt == "aware_utc":
                writer.writerow([at.replace(tzinfo=IST).astimezone(timezone.utc).isoformat(), *values, 0])
            elif fmt == "aware_ist":
                writer.writerow([at.replace(tzinfo=IST).isoformat(), *values, 0])
            else:
                writer.writerow([at.isoformat(sep=" "), *values, 0])
    return path


# Parsing and resampling


def test_angel_1m_resampling_builds_exact_75_bar_sessions(tmp_path):
    days = weekdays(4)
    sessions, info = angel_sessions(write_angel(tmp_path / "a.csv", days))
    assert info["exact_sessions"] == 4 and info["excluded_sessions"] == {}
    first = sessions[0]
    assert len(first) == 75 and all(len(s) == 75 for s in sessions)
    assert first[0].start == datetime.combine(days[0], time(9, 15), IST)
    assert first[-1].end == datetime.combine(days[0], time(15, 30), IST)
    minutes = [uptrend(0, m) for m in range(5)]
    assert first[0].open == round(minutes[0][0], 2)
    assert first[0].close == round(minutes[4][3], 2)
    assert first[0].high == max(round(m[1], 2) for m in minutes)
    assert first[0].low == min(round(m[2], 2) for m in minutes)
    assert all(b.end - b.start == timedelta(minutes=5) for b in first)
    assert all(b.future_id == RESEARCH_INSTRUMENT and b.valid for b in first)
    for fmt in ("split", "aware_ist", "aware_utc"):
        other, _ = angel_sessions(write_angel(tmp_path / f"{fmt}.csv", days, fmt=fmt))
        assert [(b.start, b.open, b.high, b.low, b.close) for s in other for b in s] == [
            (b.start, b.open, b.high, b.low, b.close) for s in sessions for b in s
        ]


def test_non_exact_sessions_are_excluded_whole_never_filled(tmp_path):
    days = weekdays(5)

    def damage(rows):
        missing = days[1]
        rows = [r for r in rows if not (r[0].date() == missing and r[0].time() == time(11, 0))]
        extra = [datetime.combine(days[2], time(15, 30)), 1, 1, 1, 1]
        rows.insert(next(i for i, r in enumerate(rows) if r[0].date() == days[3]), extra)
        bad = next(i for i, r in enumerate(rows) if r[0].date() == days[3] and r[0].time() == time(12, 0))
        rows[bad] = [rows[bad][0], 100, 90, 95, 99]  # high below open
        return rows

    sessions, info = angel_sessions(write_angel(tmp_path / "a.csv", days, mutate=damage))
    assert info["exact_sessions"] == 2
    assert info["excluded_sessions"] == {
        str(days[1]): "ROW_COUNT_374",
        str(days[2]): "ROW_COUNT_376",
        str(days[3]): "INVALID_OHLC",
    }
    assert [s[0].start.date() for s in sessions] == [days[0], days[4]]


@pytest.mark.parametrize("change", ["swap", "duplicate"])
def test_unsorted_or_duplicate_timestamps_fail_closed(tmp_path, change):
    def mutate(rows):
        if change == "swap":
            rows[10], rows[11] = rows[11], rows[10]
        else:
            rows.insert(10, list(rows[10]))
        return rows

    with pytest.raises(ValueError, match="STRICT_CHRONOLOGY"):
        angel_sessions(write_angel(tmp_path / "a.csv", weekdays(1), mutate=mutate))


def test_shifted_minute_convention_is_not_silently_accepted(tmp_path):
    def shift(rows):
        return [[r[0] + timedelta(minutes=1), *r[1:]] for r in rows]  # 09:16..15:30 end-labelled minutes

    with pytest.raises(ValueError, match="NO_EXACT_SESSIONS"):
        angel_sessions(write_angel(tmp_path / "a.csv", weekdays(2), mutate=shift))


# Warm-up construction


def test_each_session_uses_fresh_warmup_over_previous_15_exact_sessions(tmp_path, monkeypatch):
    days = weekdays(19)

    def drop(rows):  # Day 4 is not exact; it must be skipped, never part of a warm-up prefix.
        return [r for r in rows if not (r[0].date() == days[4] and r[0].time() == time(10, 0))]

    sessions, info = angel_sessions(write_angel(tmp_path / "a.csv", days, mutate=drop))
    assert info["exact_sessions"] == 18
    calls = []
    original = harness.evaluate

    def audit(bars):
        calls.append((len(bars), bars[0].start.date(), bars[-1].start.date()))
        return original(bars)

    monkeypatch.setattr(harness, "evaluate", audit)
    result = replay_sessions(sessions, POLICY)
    exact_days = [d for d in days if d != days[4]]
    assert result["warmup"]["evaluated_sessions"] == 3
    assert result["warmup"]["first_evaluated_date"] == str(exact_days[15])
    assert calls == [(16 * 75, exact_days[i - 15], exact_days[i]) for i in (15, 16, 17)]
    assert {t["trading_date"] for t in result["trades"]} <= {str(d) for d in exact_days[15:]}


# 15:20 versus the legacy 15:30 mark


def test_open_trade_exits_at_1520_and_legacy_1530_mark_cannot_be_the_baseline(tmp_path):
    from app.adaptive_supertrend.optimizer import signal_trades

    sessions, _ = angel_sessions(write_angel(tmp_path / "a.csv", weekdays(16)))
    result = replay_sessions(sessions, POLICY)
    today = sessions[-1]
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    bar_1520 = next(b for b in today if b.start.time().replace(tzinfo=None) == time(15, 20))
    assert trade["side"] == "LONG" and trade["entry_timestamp"] == today[4].start.isoformat()
    assert trade["exit_reason"] == "FORCED_EXIT"
    assert trade["exit_timestamp"] == bar_1520.start.isoformat() and trade["exit_price"] == bar_1520.open
    assert trade["gross_points"] == pytest.approx(bar_1520.open - today[4].open)
    assert all(datetime.fromisoformat(t["exit_timestamp"]).time() <= time(15, 20) for t in result["trades"])

    # The legacy research helper carries the same position to the 15:30 close.
    bars = [b for s in sessions[-16:] for b in s]
    legacy = [x for x in signal_trades(bars, supertrend(bars, 22, 39), [True] * len(bars))
              if x["date"] == str(today[0].start.date())]
    assert legacy[0]["entry_at"] == trade["entry_timestamp"]
    assert legacy[0]["exit_at"] == today[-1].end.isoformat() != trade["exit_timestamp"]
    path = tmp_path / "legacy.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(LEDGER_HEADER)
        for x in legacy:
            writer.writerow([22, 3.9, x["date"], x["direction"], x["entry_at"], x["exit_at"], x["price"],
                             today[-1].close, x["signal_points"], "SESSION_END"])
    rows, _ = load_frozen_ledger(path)
    comparison = compare_frozen_ledger(result["trades"], rows, exit_reason_map={"FORCED_EXIT": "SESSION_END"})
    assert comparison["status"] == "MISMATCH"
    assert {"exit_at", "exit_price", "gross_points"} <= set(comparison["fields"])


# Ledger parity


def ledger_rows(trades, *, direction=True, extra=True):
    rows = []
    if extra:
        rows.append([7, 1.1, "2025-01-06", "BULL", "2025-01-06 09:35:00", "2025-01-06 10:00:00", 1, 2, 1, "X"])
    for t in trades:
        rows.append([22, 3.9, t["trading_date"], t["direction"] if direction else t["side"],
                     datetime.fromisoformat(t["entry_timestamp"]).replace(tzinfo=None).isoformat(sep=" "),
                     datetime.fromisoformat(t["exit_timestamp"]).replace(tzinfo=None).isoformat(sep=" "),
                     t["entry_price"], t["exit_price"], t["gross_points"], t["exit_reason"]])
    return rows


def write_ledger(path, rows, header=LEDGER_HEADER):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture(scope="module")
def traded(tmp_path_factory):
    root = tmp_path_factory.mktemp("traded")

    sessions, _ = angel_sessions(write_angel(root / "a.csv", weekdays(24), price=reversal))
    result = replay_sessions(sessions, POLICY)
    assert result["summary"]["exit_reason_counts"] == {"AST_FLIP": 9, "FORCED_EXIT": 9}
    assert {t["side"] for t in result["trades"]} == {"LONG", "SHORT"}
    return result


def test_frozen_ledger_parity_exact_fields_and_tolerances(tmp_path, traded):
    trades = traded["trades"]
    rows, info = load_frozen_ledger(write_ledger(tmp_path / "l.csv", ledger_rows(trades)))
    assert info["rows_other_parameters_excluded"] == 1 and info["rows"] == len(trades)
    assert compare_frozen_ledger(trades, rows)["status"] == "MATCH"
    sides, _ = load_frozen_ledger(write_ledger(tmp_path / "s.csv", ledger_rows(trades, direction=False)))
    assert compare_frozen_ledger(trades, sides)["status"] == "MATCH"

    def variant(index, column, value):
        changed = ledger_rows(trades, extra=False)
        changed[index][LEDGER_HEADER.index(column)] = value
        loaded, _ = load_frozen_ledger(write_ledger(tmp_path / f"{column}.csv", changed))
        return compare_frozen_ledger(trades, loaded)

    last = len(trades) - 1
    exit_at = datetime.fromisoformat(trades[last]["exit_timestamp"]).replace(tzinfo=None) + timedelta(minutes=5)
    late = variant(last, "exit_at", exit_at.isoformat(sep=" "))
    assert late["status"] == "MISMATCH" and late["fields"] == ["exit_at"] and late["first_difference_index"] == last
    assert late["expected"]["exit_at"] != late["actual"]["exit_at"]
    assert variant(0, "entry", trades[0]["entry_price"] + 0.001)["status"] == "MATCH"
    assert variant(0, "entry", trades[0]["entry_price"] + 0.1)["fields"] == ["entry_price"]
    assert variant(0, "points", trades[0]["gross_points"] + 0.1)["fields"] == ["gross_points"]
    flipped = "BEAR" if trades[0]["direction"] == "BULL" else "BULL"
    assert variant(0, "direction", flipped)["fields"] == ["side"]
    assert variant(0, "exit_reason", "SOMETHING_ELSE")["fields"] == ["exit_reason"]
    assert variant(0, "date", "2001-01-01")["fields"] == ["trading_date"]
    short, _ = load_frozen_ledger(write_ledger(tmp_path / "short.csv", ledger_rows(trades)[:-1]))
    assert compare_frozen_ledger(trades, short)["fields"] == ["count"]
    with pytest.raises(ValueError, match="COLUMNS_MISSING:exit_reason"):
        load_frozen_ledger(write_ledger(tmp_path / "bad.csv", [], LEDGER_HEADER[:-1]))


def test_exit_reason_vocabulary_must_be_mapped_explicitly(tmp_path, traded):
    trades = traded["trades"]
    rows = ledger_rows(trades, extra=False)
    names = {"AST_FLIP": "FLIP", "FORCED_EXIT": "EOD_1520"}
    for row in rows:
        row[-1] = names[row[-1]]
    loaded, _ = load_frozen_ledger(write_ledger(tmp_path / "l.csv", rows))
    assert compare_frozen_ledger(trades, loaded)["status"] == "MISMATCH"
    assert compare_frozen_ledger(trades, loaded, exit_reason_map=names)["status"] == "MATCH"


# Aggregates


def test_daily_equity_drawdown_definition():
    def doc(day, points, minute):
        return {"state": "CLOSED", "trading_date": day, "gross_points": points, "side": "LONG",
                "direction": "BULL", "signal_timestamp": "", "entry_timestamp": "", "entry_price": 0,
                "exit_timestamp": f"{day}T10:{minute:02d}:00+05:30", "exit_price": 0, "exit_reason": "AST_FLIP",
                "ast": {"transition": None}}

    summary = summarize([doc("2025-01-06", 10, 0), doc("2025-01-06", -30, 5), doc("2025-01-07", 5, 0),
                         doc("2025-01-08", -8, 0)])["summary"]
    assert summary["drawdown_definition"] == "DAILY_EQUITY_FROM_ZERO"
    assert summary["trade_sequence_max_drawdown_points"] == 33  # per trade: 10, -20, -15, -23 from peak 10
    assert summary["max_drawdown_points"] == 23  # daily: -20, -15, -23 from peak 0
    assert summary["net_max_drawdown_points_at_2pt"] == 31  # daily net equity: -24, -21, -31 from peak 0
    assert summary["net_points_at_2pt"] == -31


def test_cli_angel_input_ledger_report_and_nonzero_exit(tmp_path, traded, capsys):
    source = write_angel(tmp_path / "angel.csv", weekdays(24), price=reversal)
    ledger = write_ledger(tmp_path / "ledger.csv", ledger_rows(traded["trades"]))
    summary = write_ledger(tmp_path / "summary.csv", [[22, 3.9, 3461, 18031.89]],
                           ["atr_period", "multiplier", "trades", "points"])
    out = tmp_path / "out.json"
    args = ["--angel-1m-csv", str(source), "--frozen-ledger", str(ledger), "--frozen-summary", str(summary),
            "--json", str(out)]
    # Ledger parity holds, but synthetic aggregates cannot equal the frozen decade: the gate exits non-zero.
    assert harness.main(args) == 1
    result = json.loads(out.read_text())
    assert result["frozen_ledger_comparison"]["status"] == "MATCH"
    assert result["frozen_summary_comparison"]["status"] == "MISMATCH"
    assert result["dataset"]["source"] == "ANGEL_1M" and result["warmup"]["sessions"] == 15
    assert set(result["input_sha256"]) == {"angel_1m_csv", "frozen_ledger", "frozen_summary"}
    assert result["frozen_summary_rows"] == [{"atr_period": "22", "multiplier": "3.9", "trades": "3461",
                                              "points": "18031.89"}]
    content = out.read_bytes()
    harness.main(args)
    assert out.read_bytes() == content  # Byte-deterministic report.
    rows = ledger_rows(traded["trades"], extra=False)
    rows[1][LEDGER_HEADER.index("exit")] = rows[1][LEDGER_HEADER.index("exit")] + 1
    assert harness.main([*args[:3], str(write_ledger(tmp_path / "bad.csv", rows)), "--json", str(out)]) == 1
    mismatch = json.loads(out.read_text())["frozen_ledger_comparison"]
    assert mismatch["status"] == "MISMATCH" and mismatch["first_difference_index"] == 1
    assert mismatch["fields"] == ["exit_price"]
    assert mismatch["expected"]["exit_price"] == mismatch["actual"]["exit_price"] + 1
    capsys.readouterr()
    with pytest.raises(SystemExit):
        harness.main(["--angel-1m-csv", str(source), "--futures-csv", str(source), "--json", str(out)])


def test_frozen_decade_historical_regression():
    source = os.getenv("AST_DIRECT_ANGEL_1M_CSV")
    if not source or not Path(source).exists():
        pytest.skip("AST_DIRECT_ANGEL_1M_CSV must name nifty50-spot-1m-full-sessions.csv (research host only)")
    sessions, _ = angel_sessions(Path(source))
    result = replay_sessions(sessions, POLICY)
    ledger = os.getenv("AST_DIRECT_FROZEN_LEDGER")
    if ledger:
        rows, _ = load_frozen_ledger(Path(ledger))
        mapping = dict(item.split("=", 1) for item in os.getenv("AST_DIRECT_EXIT_REASON_MAP", "").split(",") if item)
        comparison = compare_frozen_ledger(result["trades"], rows, exit_reason_map=mapping)
        assert comparison["status"] == "MATCH", comparison
    gate = compare_summary(result["summary"])
    assert gate["status"] == "MATCH", gate
