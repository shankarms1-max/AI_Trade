"""Explicit NONE and daily optimizer-policy research, never runtime defaults."""

from copy import deepcopy
from datetime import datetime
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

from app.adaptive_supertrend import optimizer_policy_research as research
from app.adaptive_supertrend.config import AstPolicy, Weights
from app.adaptive_supertrend.decision import decide
from app.adaptive_supertrend.indicators import adx, efficiency_ratio, supertrend
from app.adaptive_supertrend.optimizer import GRID, _quality, _segments, optimize, rank, signal_trades
from app.adaptive_supertrend.research import load_bars
from app.scalper.repository import IST
from tests.test_adaptive_supertrend import START, bars, parameters, policy
from tests.test_adaptive_supertrend_regime_research import DAYS, source_file


def profiles():
    return {
        "fixture": Weights(
            **dict.fromkeys(
                ("profitability", "drawdown", "profit_factor", "sharpe", "frequency", "consistency", "neighbours"), 1
            )
        )
    }


def choices(*, lookbacks=(1,), minimum=(1,), validation=(1,), oos=(2,), weights=None):
    return research.candidates(lookbacks, (2,), minimum, weights or profiles(), validation, oos)


def test_none_is_explicit_configured_and_hash_distinct():
    legacy = policy()
    unfiltered = policy(regime={"mode": "NONE"})
    assert legacy.regime.mode == "ADX_ER"
    assert unfiltered.regime.mode == "NONE"
    assert unfiltered.missing == []
    assert policy(regime=None).missing == ["regime"]
    assert legacy.policy_hash != unfiltered.policy_hash
    assert json.loads(unfiltered.model_dump_json())["regime"] == {"mode": "NONE"}
    assert AstPolicy.model_validate_json(unfiltered.model_dump_json()) == unfiltered
    old = legacy.model_dump(mode="json")
    old["regime"].pop("mode")
    assert AstPolicy.model_validate(old) == legacy
    for invalid in ({}, {"mode": None}, {"mode": "NONE", "adx_threshold": 0}, {"mode": "ADX_ER"}):
        with pytest.raises(ValidationError):
            policy(regime=invalid)


def test_none_mask_includes_warmup_and_invalid_boundaries_but_does_not_bypass_segmentation():
    from app.adaptive_supertrend.research import SessionGap

    rows = bars(10)
    boundary = SessionGap(rows[4].end, rows[5].end)
    values = rows[:5] + [boundary] + rows[6:]
    assert _quality(values, policy(regime={"mode": "NONE"})) == [True] * len(values)
    assert [len(segment) for segment in _segments(values)] == [5, 4]
    assert _quality([], policy(regime={"mode": "NONE"})) == []


def test_legacy_adx_er_mask_and_decision_unchanged():
    cfg = policy()
    values = bars(35)
    original = []
    for i, value in enumerate(adx(values, cfg.regime.adx_period)):
        er = efficiency_ratio(values[max(0, i - cfg.regime.er_lookback): i + 1], cfg.regime.er_lookback)
        original.append(
            value is not None
            and er is not None
            and value >= cfg.regime.adx_threshold
            and er >= cfg.regime.er_threshold
        )
    assert _quality(values, cfg) == original
    explicit = policy(regime={**cfg.regime.model_dump(), "mode": "ADX_ER"})
    assert cfg.policy_hash == explicit.policy_hash
    assert decide(values, values[-1].end, 25200, parameters(cfg), {}, cfg) == decide(
        values, values[-1].end, 25200, parameters(explicit), {}, explicit
    )


def test_none_skips_only_regime_gates_and_never_supplies_parameters():
    cfg = policy(regime={"mode": "NONE"})
    values = bars(15)
    result = decide(values, values[-1].end, 25200, parameters(cfg), {}, cfg)
    assert result["status"] == "ENTER"
    assert result["features"]["adx"] is None and result["features"]["efficiency_ratio"] is None
    missing = decide(values, values[-1].end, 25200, None, {}, cfg)
    assert "AST_PARAMETERS_UNAVAILABLE" in missing["blockers"]
    assert missing["status"] == "NO_SETUP"
    unconfigured = decide(values, values[-1].end, 25200, None, {}, policy(regime=None))
    assert "AST_POLICY_UNCONFIGURED" in unconfigured["blockers"]


def test_research_execution_exemption_does_not_allow_missing_signal_policy_or_runtime_execution():
    day = DAYS[1]
    values = bars(day=datetime.combine(DAYS[0], START.time(), IST))
    cfg = choices()[0].policy
    assert cfg.missing == ["execution"]
    with pytest.raises(ValueError, match="UNCONFIGURED"):
        optimize(values, day, datetime.combine(day, START.replace(hour=9, minute=0).time(), IST), cfg)
    for absent in ("optimizer", "regime"):
        with pytest.raises(ValueError, match="UNCONFIGURED"):
            optimize(
                values,
                day,
                datetime.combine(day, START.replace(hour=9, minute=0).time(), IST),
                cfg.model_copy(update={absent: None}),
                signal_research_only=True,
            )


@pytest.fixture(scope="module")
def experiment(tmp_path_factory):
    path = source_file(tmp_path_factory.mktemp("optimizer-policy"), count=6)
    values = load_bars(path)
    weight_choices = profiles()
    weight_choices["drawdown"] = weight_choices["fixture"].model_copy(update={"drawdown": 3})
    inputs = choices(lookbacks=(1, 2), minimum=(1, 10000), weights=weight_choices)
    progress = []
    output = research.run_research(values, inputs, progress.append)
    return values, inputs, output, progress


def test_full_grid_daily_reselection_explicit_candidates_and_benchmarks(experiment):
    values, inputs, output, progress = experiment
    assert len(GRID) == output["computation"]["grid_size"] == 1886
    # 2 lookbacks x 3 dates; min/weights do not rerun the grid, nor do benchmarks.
    assert output["computation"]["grid_fits"] == 6
    assert output["computation"]["candidate_count"] == len(inputs) == 8
    assert len([p for p in progress if p.startswith("grid ")]) == 6
    assert any(p.startswith("fold=") for p in progress)
    for candidate in output["candidates"]:
        cfg = AstPolicy.model_validate(candidate["signal_policy"])
        assert cfg.regime.mode == "NONE" and cfg.execution is None
        assert candidate["policy_hash"] == cfg.policy_hash
        assert [b["label"] for b in candidate["benchmarks"]] == ["7/1.1", "7/1.2", "26/1.2"]
        assert all("NEVER_FALLBACK" in b["role"] for b in candidate["benchmarks"])
        assert len(candidate["daily_oos"]) == 2  # full 2-session OOS block, no truncated tail
        for row in candidate["daily_oos"]:
            at = datetime.fromisoformat(row["selection_clock"])
            cutoff = datetime.fromisoformat(row["data_cutoff"])
            assert cutoff < at and cutoff.date().isoformat() < row["date"]
            assert row["date"] not in row["training_dates"]
            assert len(row["training_dates"]) == cfg.optimizer.lookback_sessions
            assert all(t["date"] == row["date"] for t in row["trades"])
            assert all(
                datetime.fromisoformat(t["entry_at"]) < datetime.fromisoformat(t["exit_at"]) for t in row["trades"]
            )
        if cfg.optimizer.minimum_trades == 10000:
            assert candidate["summary"]["valid_selected_oos_sessions"] == 0
            assert all(r["selected"] is None and not r["trades"] for r in candidate["daily_oos"])
        else:
            assert candidate["summary"]["valid_selected_oos_sessions"] == 2
    assert output["walk_forward_layouts"][0]["uses_oos_for_policy_selection"] is False


def test_reranking_matches_independent_original_optimize_and_static_oracle(experiment):
    values, inputs, result, _ = experiment
    candidate = next(c for c in inputs if c.optimizer.lookback_sessions == 2 and c.optimizer.minimum_trades == 1)
    record = next(c for c in result["candidates"] if c["candidate_id"] == candidate.key)
    first = record["daily_oos"][0]
    training = [b for b in values if str(b.start.date()) in first["training_dates"]]
    selected = optimize(
        training,
        DAYS[3],
        datetime.combine(DAYS[3], START.replace(hour=9, minute=0).time(), IST),
        candidate.policy,
        signal_research_only=True,
    )
    assert first["selected"] == {k: selected["selected"][k] for k in ("atr_period", "multiplier_tenths")}
    assert first["training_data_identity"] == selected["training_data_identity"]
    oos = [row["date"] for row in record["daily_oos"]]
    reference = []
    for day in oos:
        i = list(map(str, DAYS)).index(day)
        context = [b for b in values if DAYS[i - 2] <= b.start.date() <= DAYS[i]]
        for segment in _segments(context):
            reference.extend(
                t
                for t in signal_trades(segment, supertrend(segment, 7, 11), [True] * len(segment))
                if t["date"] == day
            )
    assert record["benchmarks"][0]["all_oos"] == research._measure(reference, oos)


def test_later_oos_enters_training_only_after_it_is_past(experiment):
    _, _, result, _ = experiment
    rows = result["candidates"][0]["daily_oos"]
    assert rows[0]["date"] in rows[1]["training_dates"]
    assert rows[1]["date"] not in rows[0]["training_dates"]


def test_summary_no_bridged_missing_selection_and_region_counts():
    def row(day, selected):
        return {"date": day, "selected": selected, "trades": []}

    fast = {"atr_period": 7, "multiplier_tenths": 11}
    slow = {"atr_period": 26, "multiplier_tenths": 12}
    value = research._summary([row(str(DAYS[0]), fast), row(str(DAYS[1]), None), row(str(DAYS[2]), slow)])
    assert value["valid_selected_oos_sessions"] == 2
    assert value["comparable_adjacent_session_pairs"] == 0 and value["turnover"] is None
    assert value["fast_region_fraction"] == value["slow_region_fraction"] == 0.5
    assert not value["complete_selection_coverage"]


def test_oos_never_selects_optimizer_policy_and_future_prices_cannot_change_prior_selection(tmp_path, monkeypatch):
    path = source_file(tmp_path, count=6)
    original = load_bars(path)
    source_file(tmp_path, count=6, changed_day=DAYS[4])
    changed = load_bars(path)
    calls = []

    def grid_fixture(training, day, at, cfg, **kwargs):
        # Deterministic cheap full-grid fixture for orchestration only. Separate
        # experiment tests above use the real optimize() over all 1886 choices.
        assert all(b.start.date() < day and b.end < at for b in training)
        calls.append((day, digest_prices(training)))
        sign = 1 if training[-1].close > training[0].close else -1
        ranking = [
            {
                "atr_period": p,
                "multiplier_tenths": m,
                "metrics": research._measure(
                    [{"date": str(training[0].start.date()), "signal_points": sign * (p + m)}],
                    [str(training[0].start.date())],
                ),
            }
            for p, m in GRID
        ]
        return {
            "ranking": rank(ranking, cfg),
            "selected_at": at.isoformat(),
            "training_data_identity": digest_prices(training),
            "data_cutoff": max(b.end for b in training).isoformat(),
        }

    monkeypatch.setattr(research, "optimize", grid_fixture)
    inputs = choices(minimum=(1, 2), oos=(1, 2))
    before = research.run_research(original, inputs)
    after = research.run_research(changed, inputs)
    again = research.run_research(original, inputs)
    assert json.dumps(before, sort_keys=True) == json.dumps(again, sort_keys=True)
    for layout_a, layout_b in zip(before["walk_forward_layouts"], after["walk_forward_layouts"]):
        for a, b in zip(layout_a["folds"], layout_b["folds"]):
            if a["oos_dates"][0] <= str(DAYS[4]):
                assert a["validation_ranking"] == b["validation_ranking"]
                assert a["selected_candidate_id"] == b["selected_candidate_id"]
    for a, b in zip(before["candidates"], after["candidates"]):
        for x, y in zip(a["daily_oos"], b["daily_oos"]):
            if x["date"] <= str(DAYS[4]):
                for key in ("selected", "training_dates", "training_data_identity", "data_cutoff"):
                    assert x[key] == y[key]
            if x["date"] < str(DAYS[4]):
                assert x == y


def digest_prices(values):
    return hashlib.sha256(repr(values).encode()).hexdigest()


def test_candidates_fail_closed_no_defaults_and_resource_budgets():
    with pytest.raises(ValueError, match="WEIGHT"):
        research.candidates((1,), (2,), (1,), {}, (1,), (1,))
    with pytest.raises(ValueError, match="UNIQUE"):
        choices(lookbacks=(1, 1))
    with pytest.raises(ValueError, match="BUDGET"):
        choices(lookbacks=tuple(range(1, 258)))
    with pytest.raises(ValidationError):
        research.candidates((1,), (1,), (1,), profiles(), (1,), (1,))
    with pytest.raises(ValueError, match="HISTORY_INSUFFICIENT"):
        research.run_research(bars(), choices(lookbacks=(10,)))


def test_cli_explicit_inputs_progress_deterministic_output_and_source_safety(
    tmp_path, monkeypatch, experiment, capsys
):
    _, _, output, _ = experiment
    path = source_file(tmp_path, count=4)
    weight_file, destination, table = tmp_path / "weights.json", tmp_path / "out.json", tmp_path / "out.csv"
    weight_file.write_text(json.dumps({k: v.model_dump() for k, v in profiles().items()}))
    data_before = path.read_bytes()

    def run(values, inputs, progress):
        progress("fixture progress to stderr")
        return deepcopy(output)

    monkeypatch.setattr(research, "run_research", run)
    argv = [
        "--csv",
        str(path),
        "--weight-profiles",
        str(weight_file),
        "--json",
        str(destination),
        "--csv-output",
        str(table),
        "--lookback-sessions",
        "1,2",
        "--subperiods",
        "2",
        "--minimum-trades",
        "1",
        "--validation-sessions",
        "1",
        "--oos-sessions",
        "2",
    ]
    assert research.main(argv) == 0
    first, first_csv = destination.read_bytes(), table.read_bytes()
    assert research.main(argv) == 0
    assert destination.read_bytes() == first and table.read_bytes() == first_csv
    assert path.read_bytes() == data_before
    captured = capsys.readouterr()
    assert not captured.out and "progress" in captured.err
    assert json.loads(first)["source_checksums"][str(path)] == hashlib.sha256(data_before).hexdigest()
    with pytest.raises(SystemExit):
        research.main(["--csv", str(path)])
    argv[argv.index("--json") + 1] = str(weight_file)
    with pytest.raises(SystemExit):
        research.main(argv)


def test_harness_has_no_database_execution_worker_or_acts_dependency():
    text = inspect.getsource(research)
    assert "app.acts_paper" not in text and "app.db" not in text and "Session(" not in text
    assert "ExecutionPolicy(" not in text and "app.adaptive_supertrend.service" not in text
