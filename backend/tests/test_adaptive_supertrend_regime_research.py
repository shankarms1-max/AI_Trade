"""Causal training-only thresholds and independent descriptive/OOS research rankings."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import csv
import json

import pytest

from app.adaptive_supertrend import regime_research as research
from app.adaptive_supertrend.indicators import adx, efficiency_ratio, supertrend
from app.adaptive_supertrend.optimizer import _segments, metrics, signal_trades
from app.adaptive_supertrend.research import SessionGap, load_bars
from app.scalper.repository import IST

DAYS = [
    date(2026, 10, 26),
    date(2026, 10, 27),
    date(2026, 10, 28),
    date(2026, 10, 29),
    date(2026, 10, 30),
    date(2026, 11, 2),
    date(2026, 11, 3),
]


def spec(**changes):
    values = dict(
        adx_period_min=2,
        adx_period_max=3,
        er_lookback_min=2,
        er_lookback_max=3,
        threshold_quantiles=(Decimal(".25"), Decimal(".75")),
        minimum_trades_per_block=1,
        train_sessions=2,
        validation_sessions=1,
        oos_sessions=1,
        ast_configs=research.parse_configs("FAST:5/1.1,SLOW:20/1.2"),
    )
    values.update(changes)
    return research.ResearchSpec(**values)


def source_file(tmp_path, *, count=4, partial=False, roll=False, changed_day=None):
    path = tmp_path / "futures-5m.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["start", "end", "open", "high", "low", "close", "future_instrument_id", "future_expiry"])
        for day_index, day in enumerate(DAYS[:count]):
            current = 25000.0 + day_index * 20
            for index in range(73 if partial else 75):
                at = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=index * 5)
                move = (4 if index < 18 else -5 if index < 36 else 6 if index < 54 else -3) + (index % 4 - 1.5)
                if changed_day == day:
                    move = (12 if index % 2 else -15) + day_index
                closed = current + move
                instrument = "OCT" if roll and day < date(2026, 10, 28) else "NOV"
                expiry = "2026-10-27" if instrument == "OCT" else "2026-11-26"
                writer.writerow(
                    [
                        at.isoformat(),
                        (at + timedelta(minutes=5)).isoformat(),
                        current,
                        max(current, closed) + 2 + index % 3,
                        min(current, closed) - 2 - index % 2,
                        closed,
                        instrument,
                        expiry,
                    ]
                )
                current = closed
    return path


def test_default_families_are_exact_research_candidates_only():
    rows = research.DEFAULT_AST_CONFIGS
    assert [(r.atr_period, r.multiplier_tenths) for r in rows if r.family == "FAST"] == [
        (5, 11),
        (5, 12),
        (6, 11),
        (6, 12),
        (7, 11),
        (7, 12),
    ]
    assert [(r.atr_period, r.multiplier_tenths) for r in rows if r.family == "SLOW"] == [
        (20, 12),
        (22, 12),
        (24, 12),
        (25, 12),
        (26, 11),
        (26, 12),
        (26, 13),
    ]
    assert len(rows) == 13
    assert research.parse_configs("26/1.2")[0].multiplier_tenths == 12


def test_training_quantiles_are_exact_and_validation_cannot_create_thresholds(tmp_path):
    path = source_file(tmp_path)
    bars = load_bars(path)
    settings = spec()
    result = research.run_research(bars, settings)
    fold = result["folds"][0]
    prepared = research.Prepared(bars, settings)
    training = {str(day) for day in DAYS[:2]}
    for policy in fold["training_thresholds"]:
        values = sorted(
            value
            for i, segment in enumerate(prepared.segments)
            for bar, value in zip(segment, prepared.adxs[i][policy["adx_period"]])
            if str(bar.start.date()) in training and value is not None
        )
        assert policy["adx_threshold"] == research.quantile(values, Decimal(policy["adx_threshold_quantile"]))
        assert policy["adx_training_observations"] == len(values)
        er_values = sorted(
            value
            for i, segment in enumerate(prepared.segments)
            for bar, value in zip(segment, prepared.ers[i][policy["er_lookback"]])
            if str(bar.start.date()) in training and value is not None
        )
        assert policy["er_threshold"] == research.quantile(er_values, Decimal(policy["er_threshold_quantile"]))
        assert policy["threshold_source"] == "TRAINING_ONLY"
    changed = research.run_research(load_bars(source_file(tmp_path, changed_day=DAYS[2])), settings)
    assert changed["folds"][0]["training_thresholds"] == fold["training_thresholds"]
    assert changed["folds"][0]["training_data_identity"] == fold["training_data_identity"]
    frozen = {row["policy_id"]: row for row in fold["training_thresholds"]}
    selected = changed["folds"][0]["training_validation_selection"]["parameters"]
    if selected:
        assert selected == frozen[selected["policy_id"]]


def test_oos_price_changes_cannot_change_training_validation_selection(tmp_path):
    original = research.run_research(load_bars(source_file(tmp_path)), spec())
    changed = research.run_research(load_bars(source_file(tmp_path, changed_day=DAYS[3])), spec())
    before, after = original["folds"][0], changed["folds"][0]
    assert before["training_thresholds"] == after["training_thresholds"]
    assert before["training_validation_selection"] == after["training_validation_selection"]
    before_rows = {row["parameters"]["policy_id"]: row for row in before["candidates"]}
    for row in after["candidates"]:
        old = before_rows[row["parameters"]["policy_id"]]
        assert row["validation_selection_rank"] == old["validation_selection_rank"]
        for actual, expected in zip(row["ast_results"], old["ast_results"]):
            assert actual["filtered"]["training"] == expected["filtered"]["training"]
            assert actual["filtered"]["validation"] == expected["filtered"]["validation"]
    assert original["methodology"]["post_oos_ranking_is_ex_post_not_honest_oos_selection"]
    assert original["selected_policy_oos"]["uses_oos_for_selection"] is False
    assert original["folds"][0]["unfiltered_baseline"] != changed["folds"][0]["unfiltered_baseline"]


def test_chronological_rolling_blocks_have_nonoverlapping_oos():
    blocks = research.chronological_blocks(DAYS, spec())
    assert len(blocks) == 4
    assert blocks[0] == {"training": DAYS[:2], "validation": DAYS[2:3], "oos": DAYS[3:4]}
    assert blocks[1] == {"training": DAYS[1:3], "validation": DAYS[3:4], "oos": DAYS[4:5]}
    assert [day for fold in blocks for day in fold["oos"]] == DAYS[3:]
    for fold in blocks:
        assert fold["training"][-1] < fold["validation"][0] < fold["oos"][0]
    with pytest.raises(ValueError, match="ORDERED_UNIQUE"):
        research.chronological_blocks(list(reversed(DAYS)), spec())
    with pytest.raises(ValueError, match="INSUFFICIENT_SESSIONS"):
        research.chronological_blocks(DAYS[:3], spec())


def test_repeated_results_and_unfiltered_oracle_are_exact(tmp_path):
    bars = load_bars(source_file(tmp_path))
    settings = spec()
    first = research.run_research(bars, settings)
    assert first == research.run_research(bars, settings)
    training_validation_oos = research.chronological_blocks(DAYS[:4], settings)[0]
    for config in settings.ast_configs:
        trades = [
            trade
            for segment in _segments(bars)
            for trade in signal_trades(
                segment, supertrend(segment, config.atr_period, config.multiplier_tenths), [True] * len(segment)
            )
        ]
        for block, days in training_validation_oos.items():
            strings = [str(day) for day in days]
            expected = metrics([row for row in trades if row["date"] in strings], len(days), len(days), strings)
            assert first["folds"][0]["unfiltered_baseline"][config.key][block] == expected
        for row in first["folds"][0]["candidates"]:
            actual = next(r for r in row["ast_results"] if r["ast"]["config_id"] == config.key)
            assert actual["unfiltered"] == first["folds"][0]["unfiltered_baseline"][config.key]
            for kind in ("filtered", "unfiltered"):
                for block in ("training", "validation", "oos"):
                    assert actual[kind][block]["gross_option_pnl"] is None
                    assert actual[kind][block]["net_option_pnl"] is None
                    assert actual[kind][block]["cost_status"] == "SIGNAL_ONLY_NO_OPTION_FILLS"
            oos, baseline = actual["filtered"]["oos"], actual["unfiltered"]["oos"]
            assert (
                actual["regime_filter_value"]["points"] == oos["gross_signal_points"] - baseline["gross_signal_points"]
            )
            assert actual["regime_filter_value"]["drawdown"] == oos["max_drawdown"] - baseline["max_drawdown"]
            assert actual["regime_filter_value"]["trade_count"] == oos["total_trades"] - baseline["total_trades"]
    json.dumps(first, allow_nan=False)


def test_partial_sessions_and_october_28_roll_reset_all_indicator_context(tmp_path):
    bars = load_bars(source_file(tmp_path, count=4, partial=True, roll=True))
    settings = spec()
    prepared = research.Prepared(bars, settings)
    assert len([bar for bar in bars if isinstance(bar, SessionGap)]) == 4
    assert [len(segment) for segment in prepared.segments] == [73] * 4
    roll = prepared.segments[2]
    assert roll[0].start.date() == date(2026, 10, 28) and roll[0].future_id == "NOV"
    assert roll[0].valid and all(row.close is not None for row in roll)
    assert prepared.directions[2]["5/1.1"] == supertrend(roll, 5, 11)
    assert prepared.adxs[2][2] == adx(roll, 2)
    assert prepared.adxs[2][2][:3] == [None] * 3
    assert prepared.ers[2][2][:2] == [None] * 2
    assert prepared.ers[2][2][-1] == efficiency_ratio(roll, 2)
    assert {bar.future_id for bar in prepared.segments[1]} == {"OCT"}
    result = research.run_research(bars, settings)
    assert result["dataset"]["valid_completed_bars"] == 292
    assert result["dataset"]["session_gap_boundaries"] == 4
    assert result["folds"][0]["dates"]["validation"] == ["2026-10-28"]


def test_no_execution_policy_settings_database_or_broker_required(tmp_path, monkeypatch):
    from app.adaptive_supertrend.config import ExecutionPolicy
    from app.core.config import get_settings
    import app.core.config as core

    def forbidden(*args, **kwargs):
        pytest.fail("Runtime policy/settings were accessed")

    monkeypatch.setattr(ExecutionPolicy, "__init__", forbidden)
    monkeypatch.setattr(core, "get_settings", forbidden)
    monkeypatch.setenv("DATABASE_URL", "forbidden://no-database-may-connect")
    result = research.run_research(load_bars(source_file(tmp_path)), spec())
    assert result["purpose"] == "FUTURES_SIGNAL_RESEARCH_ONLY_NO_RUNTIME_POLICY"
    assert "execution" not in result["spec"]
    assert get_settings is not forbidden


def test_missing_training_indicators_remain_unavailable_not_zero(tmp_path):
    settings = spec(adx_period_min=100, adx_period_max=100, er_lookback_min=100, er_lookback_max=100)
    result = research.run_research(load_bars(source_file(tmp_path, partial=True)), settings)
    fold = result["folds"][0]
    assert fold["training_validation_selection"]["policy_id"] is None
    for row in fold["candidates"]:
        assert row["parameters"]["adx_threshold"] is None and row["parameters"]["er_threshold"] is None
        assert row["robustness"] is None and row["threshold_status"] == "NO_TRAINING_INDICATOR_VALUES"
        assert all(r["filtered"] is None and r["unfiltered"]["oos"]["total_trades"] >= 0 for r in row["ast_results"])
    assert not result["selected_policy_oos"]["complete_fold_coverage"]
    assert result["selected_policy_oos"]["ast_results"][0]["oos"] is None


def test_precomputation_is_per_fold_and_er_slices_are_bounded(tmp_path, monkeypatch):
    settings = spec()
    bars = load_bars(source_file(tmp_path))
    counts = {"adx": 0, "atr": 0, "supertrend": 0}
    for name in counts:
        original = getattr(research, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(research, name, wrapper)
    original_er = research.efficiency_ratio
    lengths = []

    def er(rows, lookback):
        lengths.append((len(rows), lookback))
        return original_er(rows, lookback)

    monkeypatch.setattr(research, "efficiency_ratio", er)
    result = research.run_research(bars, settings)
    assert counts == {"adx": 2, "atr": 2, "supertrend": 2}
    assert all(length <= lookback + 1 for length, lookback in lengths)
    assert result["precomputation"]["hits"] > 0
    assert result["precomputation"]["cache_capacity"] == 4096


def test_minimum_evidence_and_ranking_use_robustness_before_total_points(tmp_path):
    result = research.run_research(load_bars(source_file(tmp_path)), spec())
    rows = result["post_oos_research_ranking"]
    assert rows == sorted(rows, key=research._rank_key)
    for index, row in enumerate(rows, 1):
        assert row["post_oos_research_rank"] == index
        assert set(row["family_robustness"]) == {"FAST", "SLOW"}
    stronger = dict(rows[0])
    weaker = dict(rows[0])
    stronger["robustness"] = {**rows[0]["robustness"], "qualified_positive_both": 2, "total_oos_points": 1}
    weaker["robustness"] = {**rows[0]["robustness"], "qualified_positive_both": 1, "total_oos_points": 999999}
    assert research._rank_key(stronger) < research._rank_key(weaker)
    sparse = research.run_research(load_bars(source_file(tmp_path)), spec(minimum_trades_per_block=999))
    assert sparse["folds"][0]["training_validation_selection"]["policy_id"] is None
    assert all(
        row["robustness"]["minimum_evidence_configurations"] == 0 for row in sparse["post_oos_research_ranking"]
    )


def test_trade_reduction_without_points_or_drawdown_gain_is_visible():
    base = {"gross_signal_points": 10, "max_drawdown": 5, "total_trades": 5}
    worse = {"gross_signal_points": 9, "max_drawdown": 6, "total_trades": 2}
    improved = {"gross_signal_points": 11, "max_drawdown": 4, "total_trades": 2}
    assert research._comparison(worse, base)["trade_reduction_without_points_or_drawdown_gain"]
    assert not research._comparison(improved, base)["trade_reduction_without_points_or_drawdown_gain"]


def test_global_oos_aggregation_does_not_double_count_overlapping_training_windows(tmp_path):
    result = research.run_research(load_bars(source_file(tmp_path, count=6)), spec())
    assert len(result["folds"]) == 3
    global_row = result["post_oos_research_ranking"][0]
    policy_id = global_row["parameters"]["policy_id"]
    for actual in global_row["ast_results"]:
        config_id = actual["ast"]["config_id"]
        fold_rows = [
            next(r for r in fold["candidates"] if r["parameters"]["policy_id"] == policy_id)
            for fold in result["folds"]
        ]
        counted = [next(r for r in row["ast_results"] if r["ast"]["config_id"] == config_id) for row in fold_rows]
        assert actual["filtered"]["oos"]["total_trades"] == sum(r["filtered"]["oos"]["total_trades"] for r in counted)
        assert actual["filtered"]["oos"]["gross_signal_points"] == sum(
            r["filtered"]["oos"]["gross_signal_points"] for r in counted
        )
        assert "MAY_OVERLAP" in actual["filtered"]["validation"]["aggregation_basis"]
    assert result["dataset"]["evaluated_oos_dates"] == [str(day) for day in DAYS[3:6]]


def test_cli_json_csv_repeatability_and_input_preservation(tmp_path):
    source = source_file(tmp_path)
    original = source.read_bytes()
    output, table = tmp_path / "research.json", tmp_path / "research.csv"
    args = [
        "--csv",
        str(source),
        "--json",
        str(output),
        "--csv-output",
        str(table),
        "--ast-configs",
        "FAST:5/1.1,SLOW:20/1.2",
        "--adx-period-min",
        "2",
        "--adx-period-max",
        "3",
        "--er-lookback-min",
        "2",
        "--er-lookback-max",
        "3",
        "--threshold-quantiles",
        ".25,.75",
        "--minimum-trades-per-block",
        "1",
        "--train-sessions",
        "2",
        "--validation-sessions",
        "1",
        "--oos-sessions",
        "1",
    ]
    assert research.main(args) == 0
    first_json, first_csv = output.read_bytes(), table.read_bytes()
    assert research.main(args) == 0
    assert output.read_bytes() == first_json and table.read_bytes() == first_csv and source.read_bytes() == original
    payload = json.loads(first_json)
    assert payload["source"]["sha256"]
    with table.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 32 and "regime_filter_value_points" in rows[0]
    assert rows[0]["unfiltered_oos_gross_signal_points"] != ""
    bad = args.copy()
    bad[bad.index("--json") + 1] = str(source)
    with pytest.raises(SystemExit):
        research.main(bad)
    assert source.read_bytes() == original


@pytest.mark.parametrize("value", ["NaN", "-.1", "1.1", "", "Infinity"])
def test_bad_quantiles_fail(value):
    with pytest.raises(ValueError):
        research.parse_quantiles(value)


@pytest.mark.parametrize("value", ["5/1.15", "4/1.2", "5/5.1", "5/1.2,5/1.2", ""])
def test_bad_ast_configs_fail(value):
    with pytest.raises(ValueError):
        research.parse_configs(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"adx_period_min": 4, "adx_period_max": 2},
        {"er_lookback_min": 0},
        {"minimum_trades_per_block": 0},
        {"oos_sessions": 0},
    ],
)
def test_invalid_research_spec_fails(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def test_computational_guard_does_not_silently_truncate_the_grid(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "MAX_CONFIG_EVALUATIONS", 1)
    with pytest.raises(ValueError, match="GRID_TOO_LARGE"):
        research.run_research(load_bars(source_file(tmp_path)), spec())


def test_quantile_reference_interpolation_and_endpoints():
    values = [0.0, 10.0, 20.0, 30.0]
    assert [research.quantile(values, Decimal(q)) for q in ("0", ".25", ".5", ".75", "1")] == [0, 7.5, 15, 22.5, 30]
    assert research.quantile([], Decimal(".5")) is None


def test_later_oos_cannot_change_any_earlier_fold_or_its_own_selection(tmp_path):
    original = research.run_research(load_bars(source_file(tmp_path, count=6)), spec())
    changed = research.run_research(load_bars(source_file(tmp_path, count=6, changed_day=DAYS[5])), spec())
    assert original["folds"][:2] == changed["folds"][:2]
    assert original["folds"][2]["training_thresholds"] == changed["folds"][2]["training_thresholds"]
    assert (
        original["folds"][2]["training_validation_selection"] == changed["folds"][2]["training_validation_selection"]
    )


@pytest.mark.parametrize(
    "field,better",
    [
        ("worst_oos_points", 1),
        ("median_oos_points", 1),
        ("aggregate_oos_drawdown", -1),
        ("total_validation_oos_trades", 1),
    ],
)
def test_lexicographic_ranking_uses_each_documented_tiebreak(field, better):
    template = {"adx_period": 2, "adx_threshold_quantile": ".25", "er_lookback": 2, "er_threshold_quantile": ".25"}
    summary = {
        "minimum_evidence_configurations": 1,
        "qualified_positive_both": 1,
        "worst_oos_points": 10,
        "median_oos_points": 10,
        "aggregate_oos_drawdown": 10,
        "total_validation_oos_trades": 10,
    }
    base = {"parameters": template, "robustness": summary}
    improved = {"parameters": template, "robustness": {**summary, field: summary[field] + better}}
    assert research._rank_key(improved) < research._rank_key(base)
    tied = {"parameters": {**template, "adx_period": 3}, "robustness": summary}
    assert research._rank_key(base) < research._rank_key(tied)


def test_cli_default_candidates_and_hardlink_output_cannot_overwrite_source(tmp_path):
    source = source_file(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "all-candidates.json"
    args = [
        "--csv",
        str(source),
        "--json",
        str(output),
        "--adx-period-min",
        "2",
        "--adx-period-max",
        "2",
        "--er-lookback-min",
        "2",
        "--er-lookback-max",
        "2",
        "--threshold-quantiles",
        ".5",
        "--minimum-trades-per-block",
        "1",
        "--train-sessions",
        "2",
        "--validation-sessions",
        "1",
        "--oos-sessions",
        "1",
    ]
    assert research.main(args) == 0
    result = json.loads(output.read_text())
    assert len(result["spec"]["ast_configs"]) == 13
    assert len(result["folds"][0]["candidates"][0]["ast_results"]) == 13
    linked = tmp_path / "hardlink.json"
    linked.hardlink_to(source)
    args[args.index("--json") + 1] = str(linked)
    with pytest.raises(SystemExit):
        research.main(args)
    assert source.read_bytes() == original


def test_validation_overlap_is_labelled_but_oos_remains_disjoint(tmp_path):
    result = research.run_research(load_bars(source_file(tmp_path, count=6)), spec(validation_sessions=2))
    assert len(result["folds"]) == 2
    assert result["folds"][0]["dates"]["validation"][-1] == result["folds"][1]["dates"]["validation"][0]
    assert result["dataset"]["evaluated_oos_dates"] == [str(day) for day in DAYS[4:6]]
    for policy in result["post_oos_research_ranking"]:
        policy_id = policy["parameters"]["policy_id"]
        expected_thresholds = []
        for fold in result["folds"]:
            values = next(row for row in fold["training_thresholds"] if row["policy_id"] == policy_id)
            expected_thresholds.append({"fold": fold["fold"], **values})
        assert policy["thresholds_by_fold"] == expected_thresholds
        for row in policy["ast_results"]:
            assert row["filtered"]["oos"]["trade_frequency"] == row["filtered"]["oos"]["total_trades"] / 2
            assert row["filtered"]["validation"]["aggregation_basis"].endswith("MAY_OVERLAP")


def test_cli_rejects_a_source_changed_during_calculation(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    output = tmp_path / "changed-source.json"
    original = research.run_research

    def change_input(bars, settings):
        result = original(bars, settings)
        source.write_text(source.read_text() + "\n")
        return result

    monkeypatch.setattr(research, "run_research", change_input)
    args = [
        "--csv",
        str(source),
        "--json",
        str(output),
        "--ast-configs",
        "5/1.1",
        "--adx-period-min",
        "2",
        "--adx-period-max",
        "2",
        "--er-lookback-min",
        "2",
        "--er-lookback-max",
        "2",
        "--threshold-quantiles",
        ".5",
        "--minimum-trades-per-block",
        "1",
        "--train-sessions",
        "2",
        "--validation-sessions",
        "1",
        "--oos-sessions",
        "1",
    ]
    with pytest.raises(SystemExit):
        research.main(args)
    assert not output.exists()
