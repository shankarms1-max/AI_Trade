"""AST_V1 indicator vectors, causal decisions, optimizer and policy regressions."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
import json
from time import perf_counter

import pytest
from pydantic import ValidationError

from app.adaptive_supertrend.bars import build_bars, opening_range
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.indicators import adx, atr, efficiency_ratio, supertrend, wilder
from app.adaptive_supertrend.optimizer import GRID, evaluate_grid, metrics, optimize, walk_forward
from app.adaptive_supertrend.research import load_bars, main
from app.parallel_paper.bars import Bar, MarketPoint
from app.scalper.repository import IST

START = datetime(2026, 11, 16, 9, 15, tzinfo=IST)
EXPIRY = date(2026, 11, 26)


def policy(**updates):
    # Explicit synthetic research choices; these are test inputs, never runtime defaults.
    value = {
        "optimizer": {
            "lookback_sessions": 1,
            "subperiods": 2,
            "minimum_trades": 1,
            "validation_sessions": 1,
            "out_of_sample_sessions": 1,
            "weights": dict(
                profitability=1, drawdown=1, profit_factor=1, sharpe=1, frequency=1, consistency=1, neighbours=1
            ),
        },
        "regime": {"adx_period": 2, "adx_threshold": 0, "er_lookback": 2, "er_threshold": 0},
        "execution": {
            "entry_end_time": "14:45:00",
            "session_exit_time": "15:20:00",
            "contracts": [{"instrument_id": "FUT", "expiry": str(EXPIRY), "lot_size": 65}],
        },
    }
    value.update(updates)
    return AstPolicy.model_validate(value)


def bars(count=75, day=START, sign=1):
    return [
        Bar(
            day + timedelta(minutes=i * 5),
            day + timedelta(minutes=(i + 1) * 5),
            25000 + sign * i * 3,
            25002 + sign * i * 3,
            24998 + sign * i * 3,
            25001 + sign * i * 3,
            None,
        )
        for i in range(count)
    ]


def parameters(cfg=None, at=START):
    cfg = cfg or policy()
    return {
        "trading_date": at.date().isoformat(),
        "policy_hash": cfg.policy_hash,
        "atr_period": 5,
        "multiplier_tenths": 10,
    }


def points(count=80, **updates):
    result = [MarketPoint(START + timedelta(seconds=15 * i), 25000 + i, "FUT", EXPIRY) for i in range(count)]
    if updates:
        result[-1] = replace(result[-1], **updates)
    return result


def test_grid_exact_unique_deterministic_tenths():
    assert len(GRID) == len(set(GRID)) == 1886
    assert GRID[0] == (5, 10) and GRID[-1] == (50, 50)
    assert all(isinstance(p, int) and isinstance(m, int) for p, m in GRID)


def test_wilder_atr_reference():
    assert wilder([2, 4, 6, 8, 10], 3)[:2] == [None, None]
    assert wilder([2, 4, 6, 8, 10], 3)[2:] == pytest.approx([4, 16 / 3, 62 / 9])
    values = [
        Bar(START, START, 10, 12, 9, 11, None),
        Bar(START, START, 11, 14, 10, 13, None),
        Bar(START, START, 13, 15, 12, 14, None),
        Bar(START, START, 14, 18, 13, 17, None),
    ]
    assert atr(values, 3)[:2] == [None, None]
    assert atr(values, 3)[2:] == pytest.approx([10 / 3, 35 / 9])


def test_supertrend_reference_flips_and_doji():
    values = [10, 11, 12, 13, 14, 18, 5, 4, 3, 22, 23]
    rows = [
        Bar(
            START + timedelta(minutes=5 * i),
            START + timedelta(minutes=5 * (i + 1)),
            close,
            close + 1,
            close - 1,
            close,
            None,
        )
        for i, close in enumerate(values)
    ]
    actual = supertrend(rows, 5, 10)
    assert [row["direction"] for row in actual] == [None] * 4 + [
        "BULL",
        "BULL",
        "BEAR",
        "BEAR",
        "BEAR",
        "BULL",
        "BULL",
    ]
    assert [row["value"] for row in actual[4:]] == pytest.approx([12, 15.4, 9.88, 8.304, 6.8432, 14.92544, 16.940352])
    flat = [replace(bar, open=10, high=10, low=10, close=10) for bar in rows]
    assert all(row["direction"] == "BULL" for row in supertrend(flat, 5, 10)[4:])
    assert supertrend(rows[:4], 5, 10)[-1]["direction"] is None


def test_adx_er_reference_flat_and_warmup():
    rows = bars(10)
    assert adx(rows, 2) == [None, None, None] + [100] * 7
    assert efficiency_ratio(rows, 3) == 1
    oscillating = [replace(bar, close=value) for bar, value in zip(rows, [10, 12, 10, 12, 10, 12, 10, 12, 10, 12])]
    assert efficiency_ratio(oscillating, 4) == 0
    flat = [replace(bar, high=10, low=10, close=10, open=10) for bar in rows]
    assert adx(flat, 2)[-1] == 0 and efficiency_ratio(flat, 3) == 0
    assert adx(rows[:2], 2) == [None, None] and efficiency_ratio(rows[:3], 3) is None


def test_completed_session_anchored_bars_gaps_and_identity():
    data = points()
    result = build_bars(data, START + timedelta(minutes=19, seconds=45))
    assert len(result) == 3 and all(row.valid for row in result)
    assert result[0].start == START and result[-1].end == START + timedelta(minutes=15)
    assert not build_bars(data[:20], START + timedelta(minutes=4, seconds=45))
    missing = [point for i, point in enumerate(data) if not 5 <= i <= 10]
    assert build_bars(missing, START + timedelta(minutes=20))[0].reason == "PRICE_SEQUENCE_GAP"
    data[10] = replace(data[10], future_id="WRONG")
    assert not build_bars(data, START + timedelta(minutes=20))[0].valid
    with pytest.raises(ValueError, match="CAUSAL"):
        build_bars(points(), START)
    with pytest.raises(ValueError, match="DUPLICATE"):
        build_bars([*points(), points()[0]], START + timedelta(minutes=20))


def test_orb_exact_frozen_causal_window():
    data = points(240)
    at = START + timedelta(hours=1)
    result = build_bars(data, at)
    orb = opening_range(result, at)
    assert orb["opening_high"] == 25059 and orb["opening_low"] == 25000
    assert orb["range_start"] == START.isoformat()
    assert orb["range_end"] == START.replace(minute=30).isoformat()
    changed = result[:3] + [replace(row, high=99999, low=1) for row in result[3:]]
    assert opening_range(changed, at) == orb
    assert not opening_range(result[:2], START.replace(minute=29))["complete"]
    with pytest.raises(ValueError, match="FUTURE_BAR"):
        opening_range(result, START)


@pytest.mark.parametrize("sign,direction", [(1, "BULL"), (-1, "BEAR")])
def test_aligned_and_conflict_decisions(sign, direction):
    cfg = policy()
    data = bars(12, sign=sign)
    at = data[-1].end
    state = {}
    price = 25100 if sign > 0 else 24900
    decision = decide(data, at, price, parameters(cfg), state, cfg)
    assert decision["status"] == "ENTER" and decision["direction"] == direction
    conflict = decide(data, at, 24900 if sign > 0 else 25100, parameters(cfg), state, cfg)
    assert "AST_ORB_DIRECTION_CONFLICT" in conflict["blockers"]
    state["consumed"].append(decision["opportunity_id"])
    assert "AST_OPPORTUNITY_CONSUMED" in decide(data, at, price, parameters(cfg), state, cfg)["blockers"]
    assert "ORB_NOT_BROKEN" in decide(data, at, 25001, parameters(cfg), {}, cfg)["blockers"]


def test_no_future_partial_bar_and_precise_unconfigured_warmup():
    cfg = policy()
    data = bars(12)
    at = data[-1].end
    with pytest.raises(ValueError, match="FUTURE_BAR"):
        decide(data, at - timedelta(seconds=1), 25100, parameters(), {}, cfg)
    earlier = decide(data[:-1], at - timedelta(seconds=1), 25100, parameters(), {}, cfg)
    assert earlier["features"]["completed_bar_at"] == data[-2].end.isoformat()
    unavailable = decide(data, at, 25100, None, {}, AstPolicy())
    assert {"AST_POLICY_UNCONFIGURED", "AST_PARAMETERS_UNAVAILABLE"} <= set(unavailable["blockers"])
    warm = decide(data[:3], data[2].end, 25100, parameters(), {}, cfg)
    assert "AST_WARMUP_REQUIRED" in warm["blockers"]
    assert "AST_ADX_WARMUP_REQUIRED" in warm["blockers"]
    cfg = policy(regime={"adx_period": 2, "adx_threshold": 100, "er_lookback": 2, "er_threshold": 1})
    noisy = [replace(row, close=25000 + (i % 2)) for i, row in enumerate(data)]
    assert "AST_ER_THRESHOLD_NOT_MET" in decide(noisy, at, 25100, parameters(cfg), {}, cfg)["blockers"]


def test_frozen_orb_date_reset_and_contract_change():
    cfg, data, state = policy(), bars(12), {}
    first = decide(data, data[-1].end, 25100, parameters(), state, cfg, future_identity=("A", EXPIRY))
    assert first["status"] == "ENTER"
    changed = [replace(bar, high=999999) for bar in data]
    assert (
        decide(changed, data[-1].end, 25100, parameters(), state, cfg)["features"]["opening_high"]
        == first["features"]["opening_high"]
    )
    assert (
        "AST_SESSION_FUTURES_IDENTITY_CHANGED_OR_MISSING"
        in decide(data, data[-1].end, 25100, parameters(), state, cfg, future_identity=("B", EXPIRY))["blockers"]
    )
    tomorrow = START + timedelta(days=1)
    assert "AST_OPENING_RANGE_UNAVAILABLE" in decide([], tomorrow, 25100, None, state, cfg)["blockers"]
    assert "opening_range" not in state and state["consumed"] == []


def test_metrics_hand_calculation_and_signal_points_only():
    result = metrics([{"signal_points": p, "date": str(i // 2)} for i, p in enumerate([10, -4, 6, -2])], 2, 2)
    assert result["total_trades"] == 4 and result["win_rate"] == 0.5
    assert result["gross_signal_points"] == 10 and result["max_drawdown"] == 4
    assert result["profit_factor"] == pytest.approx(16 / 6)
    assert result["expectancy"] == 2.5 and result["payoff_ratio"] == pytest.approx(8 / 3)
    assert result["estimated_net_pnl"] is None


def test_full_grid_determinism_robustness_and_benchmark(tmp_path):
    cfg, data = policy(), bars()
    started = perf_counter()
    first = evaluate_grid(data, cfg)
    elapsed = perf_counter() - started
    second = evaluate_grid(data, cfg)
    assert first == second and len(first) == 1886
    assert first[0]["eligible"] and first[0]["neighbour_robustness"]["count"] in (2, 3, 4)
    assert first[0]["metrics"]["cost_status"] == "SIGNAL_POINTS_ONLY"
    (tmp_path / "grid-benchmark.json").write_text(
        json.dumps({"grid": len(first), "bars": len(data), "seconds": elapsed, "processes": 1})
    )


def test_optimizer_cutoff_and_no_arbitrary_fallback():
    cfg, data = policy(), bars()
    day = (START + timedelta(days=1)).date()
    before = datetime.combine(day, time(9), IST)
    result = optimize(data, day, before, cfg)
    assert result["grid_count"] == 1886 and result["selected"]
    assert result["data_cutoff"] < result["selected_at"] and result["training_data_identity"]
    with pytest.raises(ValueError, match="CUTOFF"):
        optimize(data, day, before.replace(minute=15), cfg)
    with pytest.raises(ValueError, match="CUTOFF"):
        optimize([*data, *bars(day=before.replace(minute=15))], day, before, cfg)
    with pytest.raises(ValueError, match="UNCONFIGURED"):
        optimize(data, day, before, AstPolicy())
    cfg2 = cfg.model_copy(update={"optimizer": cfg.optimizer.model_copy(update={"minimum_trades": 999})})
    assert cfg2.policy_hash != cfg.policy_hash
    assert optimize(data, day, before, cfg2)["selected"] is None


def test_chronological_walk_forward_repeatable_and_no_test_leak():
    cfg = policy()
    data = [bar for i in range(3) for bar in bars(day=START + timedelta(days=i))]
    first = walk_forward(data, cfg)
    assert first == walk_forward(data, cfg)
    assert len(first) == 1 and first[0]["train_dates"] < first[0]["validation_dates"] < first[0]["test_dates"]
    modified = data[:150] + [replace(bar, close=bar.close + 1000, high=bar.high + 1000) for bar in data[150:]]
    assert walk_forward(modified, cfg)[0]["selected"] == first[0]["selected"]


@pytest.mark.parametrize(
    "update",
    [
        {"lots": 2},
        {"bar_minutes": 3},
        {"opening_range_minutes": 30},
        {"execution": {"allowed_widths": [400]}},
        {"regime": {"adx_period": 2, "adx_threshold": -1, "er_lookback": 2, "er_threshold": 0}},
    ],
)
def test_invalid_policy_fails(update):
    with pytest.raises(ValidationError):
        policy(**update)


def test_csv_cli_deterministic_and_identity_validation(tmp_path):
    import csv

    file = tmp_path / "bars.csv"
    with file.open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["start", "end", "open", "high", "low", "close", "future_instrument_id", "future_expiry"])
        for bar in bars():
            writer.writerow(
                [
                    bar.start.isoformat(),
                    bar.end.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    "FUT",
                    EXPIRY.isoformat(),
                ]
            )
    assert len(load_bars(file)) == 75
    cfg = tmp_path / "policy.json"
    cfg.write_text(policy().model_dump_json())
    out = tmp_path / "output.json"
    args = [
        "--csv",
        str(file),
        "--policy",
        str(cfg),
        "--date",
        "2026-11-17",
        "--grid",
        "--top",
        "3",
        "--json",
        str(out),
    ]
    assert main(args) == 0
    first = out.read_bytes()
    assert main(args) == 0 and out.read_bytes() == first
    result = json.loads(first)
    assert result["grid_count"] == 1886 and len(result["ranking"]) == 3
    file.write_text(file.read_text().replace("FUT", ""))
    with pytest.raises(ValueError, match="IDENTITY"):
        load_bars(file)


def test_indicator_prefix_invariance_and_quality_rejection():
    data = bars(25)
    assert supertrend(data, 5, 10)[:10] == supertrend(data[:10], 5, 10)
    assert adx(data, 3)[:10] == adx(data[:10], 3)
    cfg = policy(regime={"adx_period": 2, "adx_threshold": 1, "er_lookback": 2, "er_threshold": 0.1})
    flat = [replace(bar, open=25000, high=25000, low=25000, close=25000) for bar in data]
    result = decide(flat, flat[-1].end, 25100, parameters(cfg), {}, cfg)
    assert {"AST_ADX_THRESHOLD_NOT_MET", "AST_ER_THRESHOLD_NOT_MET"} <= set(result["blockers"])


def test_bar_roll_boundary_and_expired_future_fail_closed():
    data = points(40)
    data[20:] = [replace(point, future_id="NEXT") for point in data[20:]]
    result = build_bars(data, START + timedelta(minutes=10))
    assert result[0].valid and not result[1].valid and result[1].reason == "AST_FUTURES_CONTRACT_BOUNDARY"
    expired = [replace(point, future_expiry=START.date() - timedelta(days=1)) for point in points(20)]
    assert build_bars(expired, START + timedelta(minutes=5))[0].reason == "AST_FUTURES_EXPIRY_INVALID"


def test_profit_only_optimizer_selection_is_not_a_robust_policy():
    cfg = policy().model_dump(mode="json")
    cfg["optimizer"]["weights"] = {name: int(name == "profitability") for name in cfg["optimizer"]["weights"]}
    with pytest.raises(ValidationError, match="ROBUSTNESS_WEIGHT_REQUIRED"):
        AstPolicy.model_validate(cfg)


def test_chronological_consistency_keeps_sessions_without_trades():
    result = metrics(
        [{"date": "2026-11-20", "signal_points": 10}], 4, 2, ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    )
    assert result["subperiod_signal_points"] == [0, 10]
    assert result["subperiod_session_dates"] == [["2026-11-17", "2026-11-18"], ["2026-11-19", "2026-11-20"]]
    assert result["consistency"] == 0.5 and result["trade_frequency"] == 0.25


@pytest.mark.parametrize("invalid", [float("inf"), float("nan"), 0, -1])
def test_invalid_current_future_cannot_create_a_partial_bar_breakout(invalid):
    data = bars(12)
    result = decide(data, data[-1].end + timedelta(seconds=15), invalid, parameters(), {}, policy())
    assert result["status"] == "NO_SETUP" and "AST_FUTURE_PRICE_UNAVAILABLE" in result["blockers"]


def _write_session_csv(path, sessions, *, second_identity="FUT"):
    import csv

    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["start", "end", "open", "high", "low", "close", "future_instrument_id", "future_expiry"])
        for index, session in enumerate(sessions):
            for bar in session:
                writer.writerow(
                    [
                        bar.start.isoformat(),
                        bar.end.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        "FUT" if index == 0 else second_identity,
                        EXPIRY.isoformat(),
                    ]
                )


def _prices(row):
    return row.start, row.end, row.open, row.high, row.low, row.close


@pytest.mark.parametrize("second_identity", ["FUT", "NEXT"])
def test_csv_truncated_sessions_keep_real_opening_bars_and_explicit_nonprice_gaps(tmp_path, second_identity):
    from app.adaptive_supertrend.research import SessionGap, SESSION_GAP

    first = bars(73)
    second = bars(73, day=START + timedelta(days=1))
    source = tmp_path / "two-truncated-sessions.csv"
    _write_session_csv(source, [first, second], second_identity=second_identity)
    original = source.read_bytes()
    loaded = load_bars(source)
    genuine = [row for row in loaded if row.valid]
    boundaries = [row for row in loaded if not row.valid]
    assert len(genuine) == 146 and len(boundaries) == 2
    assert [_prices(row) for row in genuine] == [_prices(row) for row in first + second]
    for boundary, session in zip(boundaries, [first, second]):
        assert isinstance(boundary, SessionGap) and boundary.reason == SESSION_GAP
        assert boundary.start == session[-1].end
        assert boundary.end == session[-1].end.replace(hour=15, minute=30)
        assert boundary.end - boundary.start == timedelta(minutes=10)
        assert (boundary.open, boundary.high, boundary.low, boundary.close, boundary.volume) == (None,) * 5
        assert not any(row.valid and boundary.start <= row.start < boundary.end for row in loaded)
    assert loaded[74].valid and loaded[74].start == second[0].start
    assert loaded[74].future_id == second_identity
    cutoff = second[2].end
    causal = [row for row in loaded if row.end <= cutoff]
    orb = opening_range(causal, cutoff)
    assert orb["complete"] and orb["opening_range_date"] == second[0].start.date().isoformat()
    assert orb["range_start"] == second[0].start.isoformat() and orb["range_end"] == cutoff.isoformat()
    assert orb["opening_high"] == max(row.high for row in second[:3])
    assert orb["opening_low"] == min(row.low for row in second[:3])
    assert source.read_bytes() == original


def test_csv_session_boundaries_reset_supertrend_adx_and_er_warmup(tmp_path):
    from app.adaptive_supertrend.optimizer import _segments

    first, second = bars(73), bars(73, day=START + timedelta(days=1))
    source = tmp_path / "warmup.csv"
    _write_session_csv(source, [first, second])
    segments = _segments(load_bars(source))
    assert list(map(len, segments)) == [73, 73]
    assert all(row.valid and row.close is not None for segment in segments for row in segment)
    assert supertrend(segments[0], 5, 10) == supertrend(first, 5, 10)
    assert supertrend(segments[1], 5, 10) == supertrend(second, 5, 10)
    assert all(row["direction"] is None for row in supertrend(segments[1], 5, 10)[:4])
    assert adx(segments[1], 3) == adx(second, 3)
    assert adx(segments[1], 3)[:5] == [None] * 5
    assert adx(segments[1], 3)[5] == 100
    assert efficiency_ratio(segments[1][:2], 2) is None


def test_csv_session_boundaries_are_deterministic_and_do_not_leak_next_session(tmp_path):
    from app.adaptive_supertrend.optimizer import _segments

    first, second = bars(73), bars(73, day=START + timedelta(days=1))
    previous_only, both, changed = [tmp_path / name for name in ("prior.csv", "both.csv", "changed.csv")]
    _write_session_csv(previous_only, [first])
    _write_session_csv(both, [first, second])
    modified = [
        replace(row, open=row.open + 1000, high=row.high + 1000, low=row.low + 1000, close=row.close + 1000)
        for row in second
    ]
    _write_session_csv(changed, [first, modified])
    original = load_bars(both)
    assert original == load_bars(both)
    prior = load_bars(previous_only)
    close = first[-1].end.replace(hour=15, minute=30)
    assert [row for row in original if row.end <= close] == prior
    assert [row for row in load_bars(changed) if row.end <= close] == prior
    # At the last actual capture, the later missing-interval marker is not yet an observation.
    assert [_prices(row) for row in original if row.end <= first[-1].end] == [_prices(row) for row in first]
    assert _segments(prior) == _segments(original)[:1]
    before_second_open = second[0].start - timedelta(seconds=1)
    assert all(row.start.date() == START.date() for row in original if row.end <= before_second_open)
    before_first_completion = second[0].end - timedelta(seconds=1)
    assert not opening_range([row for row in original if row.end <= before_first_completion], before_first_completion)[
        "complete"
    ]


def test_csv_incomplete_session_positions_remain_unpriced_in_signal_research(tmp_path):
    from app.adaptive_supertrend.optimizer import _segments, signal_trades

    first, second = bars(73), bars(73, day=START + timedelta(days=1))
    source = tmp_path / "unpriced.csv"
    _write_session_csv(source, [first, second])
    loaded = load_bars(source)
    for segment in _segments(loaded):
        sequence = supertrend(segment, 5, 10)
        assert sequence[4]["direction"] == "BULL" and segment[4].close > max(row.high for row in segment[:3])
        assert signal_trades(segment, sequence, [True] * len(segment)) == []
    # A separate complete source supplies a real 15:30 close; the research exit requires that evidence.
    complete = bars()
    priced = signal_trades(complete, supertrend(complete, 5, 10), [True] * len(complete))
    assert len(priced) == 1 and priced[0]["exit_at"] == complete[-1].end.isoformat()
    result = optimize(
        loaded, (START + timedelta(days=2)).date(), (START + timedelta(days=2)).replace(hour=9, minute=0), policy()
    )
    assert result["selected"] is None and result["grid_count"] == 1886
    assert all(row["metrics"]["total_trades"] == 0 for row in result["ranking"])
    assert any(row["valid"] is False and row["close"] is None for row in result["training_bars"])
    json.dumps(result, allow_nan=False)


def test_csv_full_sessions_keep_existing_overnight_continuity(tmp_path):
    from app.adaptive_supertrend.optimizer import _segments
    from app.adaptive_supertrend.research import SessionGap

    source = tmp_path / "complete.csv"
    inputs = [bars(), bars(day=START + timedelta(days=1))]
    _write_session_csv(source, inputs)
    loaded = load_bars(source)
    assert len(loaded) == 150 and all(row.valid for row in loaded)
    assert not any(isinstance(row, SessionGap) for row in loaded)
    assert len(_segments(loaded)) == 1


@pytest.mark.parametrize("missing_open", [False, True])
def test_csv_session_boundary_does_not_repair_intraday_gaps_or_missing_open(tmp_path, missing_open):
    source = tmp_path / "missing-data.csv"
    first = bars(73)
    second = bars(73, day=START + timedelta(days=1))
    second.pop(0 if missing_open else 4)
    _write_session_csv(source, [first, second])
    loaded = load_bars(source)
    bad_start = second[0].start if missing_open else second[4].start
    bad = next(row for row in loaded if row.start == bad_start)
    assert not bad.valid and bad.reason == "AST_CONTRACT_OR_SEQUENCE_BOUNDARY"
    if missing_open:
        cutoff = (START + timedelta(days=1)).replace(minute=30)
        assert not opening_range([row for row in loaded if row.end <= cutoff], cutoff)["complete"]
