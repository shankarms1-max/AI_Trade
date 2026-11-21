"""Training-quantile ADX/ER research; no runtime policy, execution or database access."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import median
from typing import Any

from app.adaptive_supertrend.indicators import adx, atr, efficiency_ratio, supertrend
from app.adaptive_supertrend.optimizer import _segments, metrics, signal_trades
from app.adaptive_supertrend.research import SessionGap, load_bars
from app.parallel_paper.bars import Bar
from app.research.manifest import digest

VERSION = "AST_REGIME_RESEARCH_V1"
MAX_CONFIG_EVALUATIONS = 100_000  # Output/compute guard, not a strategy policy.


@dataclass(frozen=True)
class AstConfig:
    family: str
    atr_period: int
    multiplier_tenths: int

    def __post_init__(self) -> None:
        if not self.family or not 5 <= self.atr_period <= 50 or not 10 <= self.multiplier_tenths <= 50:
            raise ValueError("AST_RESEARCH_CONFIG_OUTSIDE_GRID")

    @property
    def key(self) -> str:
        return f"{self.atr_period}/{self.multiplier_tenths / 10:.1f}"

    def payload(self) -> dict:
        return {**asdict(self), "config_id": self.key, "multiplier": f"{self.multiplier_tenths / 10:.1f}"}


DEFAULT_AST_CONFIGS = tuple(
    [AstConfig("FAST", p, m) for p in (5, 6, 7) for m in (11, 12)]
    + [AstConfig("SLOW", p, 12) for p in (20, 22, 24, 25)]
    + [AstConfig("SLOW", 26, m) for m in (11, 12, 13)]
)


def parse_configs(text: str) -> tuple[AstConfig, ...]:
    result = []
    try:
        for item in text.split(","):
            family, pair = item.strip().split(":", 1) if ":" in item else ("CUSTOM", item.strip())
            p, multiplier = pair.split("/")
            tenths = Decimal(multiplier) * 10
            if not tenths.is_finite() or tenths != tenths.to_integral_value():
                raise ValueError("integer tenths required")
            result.append(AstConfig(family.strip(), int(p), int(tenths)))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("AST_CONFIGS_REQUIRE_FAMILY_OPTIONAL_PERIOD/MULTIPLIER_COMMA_LIST") from exc
    if not result or len({row.key for row in result}) != len(result):
        raise ValueError("AST_RESEARCH_DUPLICATE_OR_EMPTY_CONFIGS")
    return tuple(sorted(result, key=lambda row: (row.family, row.atr_period, row.multiplier_tenths)))


def parse_quantiles(text: str) -> tuple[Decimal, ...]:
    try:
        result = tuple(sorted({Decimal(value.strip()) for value in text.split(",")}))
        if not result or any(not q.is_finite() or not 0 <= q <= 1 for q in result):
            raise ValueError("quantiles must be in [0,1]")
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("AST_RESEARCH_QUANTILES_INVALID") from exc
    return result


@dataclass(frozen=True)
class ResearchSpec:
    adx_period_min: int
    adx_period_max: int
    er_lookback_min: int
    er_lookback_max: int
    threshold_quantiles: tuple[Decimal, ...]
    minimum_trades_per_block: int
    train_sessions: int
    validation_sessions: int
    oos_sessions: int
    ast_configs: tuple[AstConfig, ...] = DEFAULT_AST_CONFIGS

    def __post_init__(self) -> None:
        if not 2 <= self.adx_period_min <= self.adx_period_max <= 100:
            raise ValueError("AST_RESEARCH_ADX_PERIOD_RANGE_INVALID")
        if not 1 <= self.er_lookback_min <= self.er_lookback_max <= 200:
            raise ValueError("AST_RESEARCH_ER_LOOKBACK_RANGE_INVALID")
        if min(self.minimum_trades_per_block, self.train_sessions, self.validation_sessions, self.oos_sessions) < 1:
            raise ValueError("AST_RESEARCH_POSITIVE_EVIDENCE_AND_BLOCK_LENGTHS_REQUIRED")
        if (
            not self.threshold_quantiles
            or any(not q.is_finite() or not 0 <= q <= 1 for q in self.threshold_quantiles)
            or tuple(sorted(set(self.threshold_quantiles))) != self.threshold_quantiles
        ):
            raise ValueError("AST_RESEARCH_QUANTILES_INVALID")
        if not self.ast_configs or len({row.key for row in self.ast_configs}) != len(self.ast_configs):
            raise ValueError("AST_RESEARCH_DUPLICATE_OR_EMPTY_CONFIGS")

    def payload(self) -> dict:
        return {
            **asdict(self),
            "threshold_quantiles": [_q(q) for q in self.threshold_quantiles],
            "ast_configs": [row.payload() for row in self.ast_configs],
        }


def _q(value: Decimal) -> str:
    return format(value.normalize(), "f")


def quantile(sorted_values: list[float], q: Decimal) -> float | None:
    """Linear R7 interpolation over sorted, training-only observed values."""
    if not sorted_values:
        return None
    index = Decimal(len(sorted_values) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + float(index - lower) * (sorted_values[upper] - sorted_values[lower])


def _templates(spec: ResearchSpec) -> list[dict]:
    return [
        {
            "policy_id": f"ADX{p}_Q{_q(a)}_ER{lookback}_Q{_q(e)}",
            "adx_period": p,
            "adx_threshold_quantile": _q(a),
            "er_lookback": lookback,
            "er_threshold_quantile": _q(e),
        }
        for p in range(spec.adx_period_min, spec.adx_period_max + 1)
        for lookback in range(spec.er_lookback_min, spec.er_lookback_max + 1)
        for a in spec.threshold_quantiles
        for e in spec.threshold_quantiles
    ]


def chronological_blocks(days: list[date], spec: ResearchSpec) -> list[dict[str, list[date]]]:
    if days != sorted(set(days)):
        raise ValueError("AST_RESEARCH_ORDERED_UNIQUE_SESSIONS_REQUIRED")
    width = spec.train_sessions + spec.validation_sessions + spec.oos_sessions
    if len(days) < width:
        raise ValueError("AST_RESEARCH_INSUFFICIENT_SESSIONS")
    result = []
    for start in range(0, len(days) - width + 1, spec.oos_sessions):
        validation = start + spec.train_sessions
        oos = validation + spec.validation_sessions
        result.append(
            {
                "training": days[start:validation],
                "validation": days[validation:oos],
                "oos": days[oos: oos + spec.oos_sessions],
            }
        )
    return result


class Prepared:
    """Per-fold causal arrays, shared across policies; no full-prefix copies per observation."""

    def __init__(self, bars: list[Bar], spec: ResearchSpec) -> None:
        self.segments = _segments(bars)
        self.directions: list[dict[str, list[dict]]] = []
        self.adxs: list[dict[int, list[float | None]]] = []
        self.ers: list[dict[int, list[float | None]]] = []
        for segment in self.segments:
            atrs = {p: atr(segment, p) for p in {config.atr_period for config in spec.ast_configs}}
            self.directions.append(
                {
                    config.key: supertrend(
                        segment, config.atr_period, config.multiplier_tenths, atrs[config.atr_period]
                    )
                    for config in spec.ast_configs
                }
            )
            self.adxs.append({p: adx(segment, p) for p in range(spec.adx_period_min, spec.adx_period_max + 1)})
            self.ers.append(
                {
                    lookback: [
                        efficiency_ratio(segment[max(0, i - lookback): i + 1], lookback) for i in range(len(segment))
                    ]
                    for lookback in range(spec.er_lookback_min, spec.er_lookback_max + 1)
                }
            )
        self.unfiltered_masks = [bytes([1]) * len(segment) for segment in self.segments]

        @lru_cache(maxsize=4096)
        def cached(segment_index: int, config_id: str, mask: bytes) -> list[dict]:
            return signal_trades(
                self.segments[segment_index],
                self.directions[segment_index][config_id],
                [bool(value) for value in mask],
            )

        self.cached = cached

    def thresholds(self, training: list[date], templates: list[dict]) -> list[dict]:
        dates = set(training)
        adx_distributions = (
            {
                p: sorted(
                    value
                    for i, segment in enumerate(self.segments)
                    for bar, value in zip(segment, self.adxs[i][p])
                    if bar.start.date() in dates and value is not None
                )
                for p in self.adxs[0]
            }
            if self.adxs
            else {}
        )
        er_distributions = (
            {
                p: sorted(
                    value
                    for i, segment in enumerate(self.segments)
                    for bar, value in zip(segment, self.ers[i][p])
                    if bar.start.date() in dates and value is not None
                )
                for p in self.ers[0]
            }
            if self.ers
            else {}
        )
        result = []
        for template in templates:
            a = adx_distributions.get(template["adx_period"], [])
            e = er_distributions.get(template["er_lookback"], [])
            result.append(
                {
                    **template,
                    "adx_threshold": quantile(a, Decimal(template["adx_threshold_quantile"])),
                    "er_threshold": quantile(e, Decimal(template["er_threshold_quantile"])),
                    "adx_training_observations": len(a),
                    "er_training_observations": len(e),
                    "threshold_source": "TRAINING_ONLY",
                    "quantile_method": "LINEAR_R7",
                }
            )
        return result

    def masks(self, policy: dict) -> list[bytes] | None:
        if policy["adx_threshold"] is None or policy["er_threshold"] is None:
            return None
        return [
            bytes(
                int(a is not None and e is not None and a >= policy["adx_threshold"] and e >= policy["er_threshold"])
                for a, e in zip(self.adxs[i][policy["adx_period"]], self.ers[i][policy["er_lookback"]])
            )
            for i in range(len(self.segments))
        ]

    def trades(self, config_id: str, masks: list[bytes]) -> list[dict]:
        return [trade for i, mask in enumerate(masks) for trade in self.cached(i, config_id, mask)]


def _block_metrics(trades: list[dict], days: list[date]) -> dict:
    dates = [str(day) for day in days]
    allowed = set(dates)
    return metrics([trade for trade in trades if trade["date"] in allowed], len(days), len(days), dates)


def _comparison(filtered: dict | None, baseline: dict) -> dict:
    if filtered is None:
        return {
            "points": None,
            "drawdown": None,
            "trade_count": None,
            "trade_reduction_without_points_or_drawdown_gain": None,
        }
    points = filtered["gross_signal_points"] - baseline["gross_signal_points"]
    drawdown = filtered["max_drawdown"] - baseline["max_drawdown"]
    count = filtered["total_trades"] - baseline["total_trades"]
    return {
        "points": points,
        "drawdown": drawdown,
        "trade_count": count,
        "trade_reduction_without_points_or_drawdown_gain": count < 0 and points <= 0 and drawdown >= 0,
    }


def _robustness(rows: list[dict], left: str = "validation", right: str = "oos") -> dict | None:
    if not rows or any(row["filtered"] is None for row in rows):
        return None
    first = [row["filtered"][left]["gross_signal_points"] for row in rows]
    second = [row["filtered"][right]["gross_signal_points"] for row in rows]
    enough = [row["evidence_sufficient"][left] and row["evidence_sufficient"][right] for row in rows]
    return {
        f"number_positive_{left}": sum(value > 0 for value in first),
        f"number_positive_{right}": sum(value > 0 for value in second),
        f"positive_both_{left}_{right}": sum(a > 0 and b > 0 for a, b in zip(first, second)),
        "qualified_positive_both": sum(a > 0 and b > 0 and valid for a, b, valid in zip(first, second, enough)),
        "minimum_evidence_configurations": sum(enough),
        f"worst_{left}_points": min(first),
        f"worst_{right}_points": min(second),
        f"median_{left}_points": median(first),
        f"median_{right}_points": median(second),
        f"total_{left}_points": sum(first),
        f"total_{right}_points": sum(second),
        f"aggregate_{right}_drawdown": sum(row["filtered"][right]["max_drawdown"] for row in rows),
        f"total_{left}_{right}_trades": sum(
            row["filtered"][block]["total_trades"] for row in rows for block in (left, right)
        ),
        "aggregation_basis": "ALTERNATIVE_AST_SCENARIOS_NOT_A_PORTFOLIO",
    }


def _families(rows: list[dict]) -> dict:
    return {
        family: _robustness([row for row in rows if row["ast"]["family"] == family])
        for family in sorted({row["ast"]["family"] for row in rows})
    }


def _tie(policy: dict) -> tuple:
    return (
        policy["adx_period"],
        Decimal(policy["adx_threshold_quantile"]),
        policy["er_lookback"],
        Decimal(policy["er_threshold_quantile"]),
    )


def _rank_key(row: dict, *, selection: bool = False) -> tuple:
    summary = row["training_validation_robustness"] if selection else row["robustness"]
    right = "validation" if selection else "oos"
    eligible = summary is not None and summary["minimum_evidence_configurations"] > 0
    return (
        not eligible,
        -(summary["qualified_positive_both"] if summary else 0),
        -(summary[f"worst_{right}_points"] if summary else float("-inf")),
        -(summary[f"median_{right}_points"] if summary else float("-inf")),
        summary[f"aggregate_{right}_drawdown"] if summary else float("inf"),
        -(summary[f"total_{'training_validation' if selection else 'validation_oos'}_trades"] if summary else 0),
        *_tie(row["parameters"]),
    )


def _filter_effect(rows: list[dict], summary: dict | None) -> dict:
    if summary is None:
        return {"status": "THRESHOLDS_UNAVAILABLE", "trade_reduction_without_robustness_gain": None}
    baseline_rows = [
        {**row, "filtered": row["unfiltered"], "evidence_sufficient": row["baseline_evidence_sufficient"]}
        for row in rows
    ]
    baseline = _robustness(baseline_rows)
    assert baseline is not None
    count_delta = sum(row["regime_filter_value"]["trade_count"] for row in rows)
    points_delta = summary["total_oos_points"] - baseline["total_oos_points"]
    dd_delta = summary["aggregate_oos_drawdown"] - baseline["aggregate_oos_drawdown"]
    worst_delta = summary["worst_oos_points"] - baseline["worst_oos_points"]
    median_delta = summary["median_oos_points"] - baseline["median_oos_points"]
    positive_delta = summary["qualified_positive_both"] - baseline["qualified_positive_both"]
    return {
        "status": "COMPARABLE",
        "points": points_delta,
        "drawdown": dd_delta,
        "trade_count": count_delta,
        "worst_oos_points": worst_delta,
        "median_oos_points": median_delta,
        "qualified_positive_both": positive_delta,
        "trade_reduction_without_robustness_gain": count_delta < 0
        and points_delta <= 0
        and dd_delta >= 0
        and worst_delta <= 0
        and median_delta <= 0
        and positive_delta <= 0,
    }


def _ledger() -> dict:
    return {
        "oos": [],
        "validation_points": 0.0,
        "validation_trades": 0,
        "validation_drawdown": 0.0,
        "evidence": {"training": True, "validation": True, "oos": True},
        "folds": 0,
    }


def _accumulate(ledger: dict, trades: list[dict], block_metrics: dict, blocks: dict, minimum: int) -> None:
    allowed = {str(day) for day in blocks["oos"]}
    ledger["oos"].extend(
        {"date": trade["date"], "signal_points": trade["signal_points"]}
        for trade in trades
        if trade["date"] in allowed
    )
    ledger["validation_points"] += block_metrics["validation"]["gross_signal_points"]
    ledger["validation_trades"] += block_metrics["validation"]["total_trades"]
    ledger["validation_drawdown"] += block_metrics["validation"]["max_drawdown"]
    for block, value in block_metrics.items():
        ledger["evidence"][block] &= value["total_trades"] >= minimum
    ledger["folds"] += 1


def _aggregate_metrics(ledger: dict, oos_days: list[date]) -> dict:
    return {
        "validation": {
            "gross_signal_points": ledger["validation_points"],
            "total_trades": ledger["validation_trades"],
            "sum_fold_max_drawdowns": ledger["validation_drawdown"],
            "aggregation_basis": "SUM_OF_FOLD_EVALUATIONS_VALIDATION_DATES_MAY_OVERLAP",
        },
        "oos": _block_metrics(ledger["oos"], oos_days),
    }


def run_research(bars: list[Bar], spec: ResearchSpec) -> dict:
    if not bars or any(left.end > right.start for left, right in zip(bars, bars[1:])):
        raise ValueError("AST_RESEARCH_CHRONOLOGICAL_BARS_REQUIRED")
    bars_by_day: dict[date, list[Bar]] = {}
    for bar in bars:
        bars_by_day.setdefault(bar.start.date(), []).append(bar)
    days = sorted({bar.start.date() for bar in bars if not isinstance(bar, SessionGap)})
    folds = chronological_blocks(days, spec)
    template_count = (
        (spec.adx_period_max - spec.adx_period_min + 1)
        * (spec.er_lookback_max - spec.er_lookback_min + 1)
        * len(spec.threshold_quantiles) ** 2
    )
    if len(folds) * template_count * len(spec.ast_configs) > MAX_CONFIG_EVALUATIONS:
        raise ValueError("AST_RESEARCH_GRID_TOO_LARGE_REDUCE_GRID_OR_BLOCKS")
    templates = _templates(spec)
    accumulators = {p["policy_id"]: {config.key: _ledger() for config in spec.ast_configs} for p in templates}
    baseline_ledgers = {config.key: _ledger() for config in spec.ast_configs}
    selected_ledgers = {config.key: _ledger() for config in spec.ast_configs}
    selected_baselines = {config.key: _ledger() for config in spec.ast_configs}
    selected_days: list[date] = []
    outputs: list[dict[str, Any]] = []
    threshold_history: dict[str, list[dict]] = {row["policy_id"]: [] for row in templates}
    cache_totals = {"hits": 0, "misses": 0}
    for fold_index, blocks in enumerate(folds):
        prefix = [
            bar for block in ("training", "validation", "oos") for day in blocks[block] for bar in bars_by_day[day]
        ]
        prepared = Prepared(prefix, spec)
        policies = prepared.thresholds(blocks["training"], templates)
        for parameters in policies:
            threshold_history[parameters["policy_id"]].append({"fold": fold_index + 1, **parameters})
        baseline_trades = {
            config.key: prepared.trades(config.key, prepared.unfiltered_masks) for config in spec.ast_configs
        }
        baselines = {
            config.key: {block: _block_metrics(baseline_trades[config.key], dates) for block, dates in blocks.items()}
            for config in spec.ast_configs
        }
        for config in spec.ast_configs:
            _accumulate(
                baseline_ledgers[config.key],
                baseline_trades[config.key],
                baselines[config.key],
                blocks,
                spec.minimum_trades_per_block,
            )
        candidates: list[dict[str, Any]] = []
        for parameters in policies:
            masks = prepared.masks(parameters)
            rows: list[dict[str, Any]] = []
            for config in spec.ast_configs:
                trades = prepared.trades(config.key, masks) if masks is not None else []
                filtered: dict | None = (
                    {block: _block_metrics(trades, dates) for block, dates in blocks.items()}
                    if masks is not None
                    else None
                )
                baseline = baselines[config.key]
                enough = {
                    block: (
                        filtered[block]["total_trades"] >= spec.minimum_trades_per_block
                        if filtered is not None
                        else False
                    )
                    for block in blocks
                }
                rows.append(
                    {
                        "ast": config.payload(),
                        "filtered": filtered,
                        "unfiltered": baseline,
                        "evidence_sufficient": enough,
                        "baseline_evidence_sufficient": {
                            block: value["total_trades"] >= spec.minimum_trades_per_block
                            for block, value in baseline.items()
                        },
                        "regime_filter_value": _comparison(filtered["oos"] if filtered else None, baseline["oos"]),
                    }
                )
                if filtered is not None:
                    _accumulate(
                        accumulators[parameters["policy_id"]][config.key],
                        trades,
                        filtered,
                        blocks,
                        spec.minimum_trades_per_block,
                    )
            summary = _robustness(rows)
            candidates.append(
                {
                    "parameters": parameters,
                    "ast_results": rows,
                    "threshold_status": "AVAILABLE" if masks is not None else "NO_TRAINING_INDICATOR_VALUES",
                    "robustness": summary,
                    "family_robustness": _families(rows),
                    "training_validation_robustness": _robustness(rows, "training", "validation"),
                    "regime_filter_value": _filter_effect(rows, summary),
                }
            )
        # This selection key receives only training/validation statistics, never OOS performance.
        validation_order = sorted(candidates, key=lambda row: _rank_key(row, selection=True))
        for rank, row in enumerate(validation_order, 1):
            row["validation_selection_rank"] = rank
        best = validation_order[0]
        qualified = (
            best["training_validation_robustness"] is not None
            and best["training_validation_robustness"]["minimum_evidence_configurations"] > 0
        )
        selection = {
            "uses_oos": False,
            "policy_id": best["parameters"]["policy_id"] if qualified else None,
            "parameters": best["parameters"] if qualified else None,
            "status": "SELECTED_FOR_RESEARCH_OOS_ONLY" if qualified else "INSUFFICIENT_TRAIN_VALIDATION_EVIDENCE",
            "ranking": [
                {
                    "policy_id": row["parameters"]["policy_id"],
                    "training_validation_robustness": row["training_validation_robustness"],
                }
                for row in validation_order
            ],
        }
        if qualified:
            masks = prepared.masks(best["parameters"])
            assert masks is not None
            selected_days.extend(blocks["oos"])
            for row in best["ast_results"]:
                key = row["ast"]["config_id"]
                _accumulate(
                    selected_ledgers[key],
                    prepared.trades(key, masks),
                    row["filtered"],
                    blocks,
                    spec.minimum_trades_per_block,
                )
                _accumulate(
                    selected_baselines[key],
                    baseline_trades[key],
                    baselines[key],
                    blocks,
                    spec.minimum_trades_per_block,
                )
        candidates.sort(key=_rank_key)
        for rank, row in enumerate(candidates, 1):
            row["post_oos_research_rank"] = rank
        train_rows = [bar for bar in prefix if bar.start.date() in set(blocks["training"])]
        actual_training = [bar for bar in train_rows if bar.valid]
        outputs.append(
            {
                "fold": fold_index + 1,
                "dates": {key: [str(day) for day in value] for key, value in blocks.items()},
                "training_data_identity": digest(train_rows),
                "training_last_price_bar_at": (
                    max(bar.end for bar in actual_training).isoformat() if actual_training else None
                ),
                "training_thresholds": policies,
                "unfiltered_baseline": baselines,
                "training_validation_selection": selection,
                "candidates": candidates,
            }
        )
        info = prepared.cached.cache_info()
        cache_totals["hits"] += info.hits
        cache_totals["misses"] += info.misses
        prepared.cached.cache_clear()
    oos_days = [day for blocks in folds for day in blocks["oos"]]
    aggregates: list[dict[str, Any]] = []
    for template in templates:
        rows = []
        for config in spec.ast_configs:
            ledger = accumulators[template["policy_id"]][config.key]
            filtered = _aggregate_metrics(ledger, oos_days) if ledger["folds"] == len(folds) else None
            baseline = _aggregate_metrics(baseline_ledgers[config.key], oos_days)
            rows.append(
                {
                    "ast": config.payload(),
                    "filtered": filtered,
                    "unfiltered": baseline,
                    "evidence_sufficient": (
                        ledger["evidence"] if filtered is not None else {block: False for block in folds[0]}
                    ),
                    "baseline_evidence_sufficient": baseline_ledgers[config.key]["evidence"],
                    "regime_filter_value": _comparison(filtered["oos"] if filtered else None, baseline["oos"]),
                }
            )
        summary = _robustness(rows)
        aggregates.append(
            {
                "parameters": template,
                "thresholds_by_fold": threshold_history[template["policy_id"]],
                "ast_results": rows,
                "robustness": summary,
                "family_robustness": _families(rows),
                "regime_filter_value": _filter_effect(rows, summary),
            }
        )
    aggregates.sort(key=_rank_key)
    for rank, row in enumerate(aggregates, 1):
        row["post_oos_research_rank"] = rank
    selected_results: list[dict[str, Any]] = []
    for config in spec.ast_configs:
        filtered = _aggregate_metrics(selected_ledgers[config.key], selected_days)["oos"] if selected_days else None
        selected_baseline = (
            _aggregate_metrics(selected_baselines[config.key], selected_days)["oos"] if selected_days else None
        )
        selected_results.append(
            {
                "ast": config.payload(),
                "oos": filtered,
                "unfiltered_oos": selected_baseline,
                "regime_filter_value": _comparison(filtered, selected_baseline) if selected_baseline else None,
            }
        )
    return {
        "schema_version": VERSION,
        "purpose": "FUTURES_SIGNAL_RESEARCH_ONLY_NO_RUNTIME_POLICY",
        "spec": spec.payload(),
        "dataset": {
            "session_dates": [str(day) for day in days],
            "genuine_bar_rows": sum(not isinstance(bar, SessionGap) for bar in bars),
            "valid_completed_bars": sum(bar.valid for bar in bars),
            "session_gap_boundaries": sum(isinstance(bar, SessionGap) for bar in bars),
            "evaluated_oos_dates": [str(day) for day in oos_days],
            "unused_tail_sessions": [str(day) for day in days if day > oos_days[-1]],
        },
        "folds": outputs,
        "post_oos_research_ranking": aggregates,
        "selected_policy_oos": {
            "uses_oos_for_selection": False,
            "covered_dates": [str(day) for day in selected_days],
            "complete_fold_coverage": len(selected_days) == len(oos_days),
            "ast_results": selected_results,
        },
        "methodology": {
            "quantile_method": "TRAINING_ONLY_LINEAR_R7",
            "indicator_context": "CAUSAL_PREFIX_STARTS_AT_EACH_FOLDS_TRAINING_START",
            "walk_forward_step": "OOS_SESSION_COUNT_NONOVERLAPPING_OOS",
            "consistency": "FRACTION_OF_CHRONOLOGICAL_SESSIONS_WITH_POSITIVE_CLOSED_SIGNAL_POINTS",
            "post_oos_rank": [
                "minimum_evidence_available_first",
                "qualified_positive_validation_and_oos_configs_desc",
                "worst_oos_points_desc",
                "median_oos_points_desc",
                "sum_config_oos_max_drawdown_asc",
                "validation_plus_oos_trades_desc",
                "adx_period_adx_quantile_er_lookback_er_quantile_asc",
            ],
            "selection_rank": "SAME_ORDER_USING_TRAINING_AND_VALIDATION_ONLY",
            "post_oos_ranking_is_ex_post_not_honest_oos_selection": True,
            "aggregate_validation": "SUM_OF_FOLD_EVALUATIONS_DATES_MAY_OVERLAP_NOT_PORTFOLIO_PNL",
            "minimum_trade_counts": "PER_AST_CONFIG_PER_BLOCK_RANKING_COUNTS_ONLY_RELEVANT_TWO_QUALIFIED_BLOCKS",
            "drawdown_delta": "FILTERED_MINUS_UNFILTERED_NEGATIVE_IS_IMPROVEMENT",
            "unpriced_positions": "EXCLUDED_BY_UNCHANGED_SIGNAL_TRADES_NO_SYNTHETIC_CLOSE",
        },
        "precomputation": {
            "cache_capacity": 4096,
            **cache_totals,
            "fold_policy_ast_evaluations": len(folds) * template_count * len(spec.ast_configs),
            "maximum_fold_policy_ast_evaluations": MAX_CONFIG_EVALUATIONS,
        },
    }


def write_csv(result: dict, path: Path) -> None:
    measures = (
        "total_trades",
        "gross_signal_points",
        "expectancy",
        "profit_factor",
        "max_drawdown",
        "sharpe_equivalent",
        "win_rate",
        "trade_frequency",
        "consistency",
        "average_win",
        "average_loss",
        "payoff_ratio",
    )
    with path.open("w", newline="") as stream:
        writer = None
        for fold in result["folds"]:
            for candidate in fold["candidates"]:
                for row in candidate["ast_results"]:
                    flat = {
                        "fold": fold["fold"],
                        **candidate["parameters"],
                        **row["ast"],
                        "post_oos_research_rank": candidate["post_oos_research_rank"],
                        "validation_selection_rank": candidate["validation_selection_rank"],
                        "selected_without_oos": candidate["parameters"]["policy_id"]
                        == fold["training_validation_selection"]["policy_id"],
                    }
                    for kind in ("filtered", "unfiltered"):
                        for block in ("training", "validation", "oos"):
                            for measure in measures:
                                flat[f"{kind}_{block}_{measure}"] = row[kind][block][measure] if row[kind] else None
                    flat.update(
                        {f"regime_filter_value_{key}": value for key, value in row["regime_filter_value"].items()}
                    )
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=list(flat))
                        writer.writeheader()
                    writer.writerow(flat)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument(
        "--ast-configs",
        type=parse_configs,
        default=DEFAULT_AST_CONFIGS,
        help="optional FAMILY:period/multiplier comma list; default FAST/SLOW research candidates",
    )
    for name in (
        "adx-period-min",
        "adx-period-max",
        "er-lookback-min",
        "er-lookback-max",
        "minimum-trades-per-block",
        "train-sessions",
        "validation-sessions",
        "oos-sessions",
    ):
        parser.add_argument(f"--{name}", type=int, required=True)
    parser.add_argument("--threshold-quantiles", type=parse_quantiles, required=True)
    args = parser.parse_args(argv)
    destinations = [path.resolve() for path in (args.json, args.csv_output) if path is not None]
    paths = [args.csv, args.json, args.csv_output]
    collisions = any(
        left.exists() and right.exists() and left.samefile(right)
        for i, left in enumerate(paths)
        if left is not None
        for right in paths[i + 1:]
        if right is not None
    )
    if args.csv.resolve() in destinations or len(set(destinations)) != len(destinations) or collisions:
        parser.error("AST_RESEARCH_OUTPUT_MUST_NOT_OVERWRITE_INPUT_OR_OTHER_OUTPUT")
    try:
        spec = ResearchSpec(**{key: getattr(args, key) for key in ResearchSpec.__dataclass_fields__})
        with args.csv.open("rb") as source:
            input_checksum = hashlib.file_digest(source, "sha256").hexdigest()
        bars = load_bars(args.csv)
        result = run_research(bars, spec)
    except ValueError as exc:
        parser.error(str(exc))
    with args.csv.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if checksum != input_checksum:
        parser.error("AST_RESEARCH_INPUT_CHANGED_DURING_RUN")
    result["source"] = {"path": str(args.csv), "sha256": checksum}
    with args.json.open("w") as output:
        json.dump(result, output, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    if args.csv_output:
        write_csv(result, args.csv_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
