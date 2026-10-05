from dataclasses import dataclass
from datetime import time
from itertools import product
from statistics import mean
from typing import Sequence

from app.shadow.models import ShadowTrade


@dataclass(frozen=True)
class ExperimentParameters:
    alpha_threshold: float
    confirmations: int
    entry_start: time
    entry_end: time
    alpha1_upper: float | None = None
    alpha1_lower: float | None = None
    alpha2_upper: float | None = None
    alpha2_lower: float | None = None
    spread_widths: tuple[float, ...] = (50, 100, 150, 200)
    short_strike_buffer: float = 100
    minimum_credit_to_width: float = 0.03
    volatility_distance_multiplier: float = 1.0
    profit_target_credit_capture_pct: float | None = None
    stop_loss_credit_multiple: float | None = None
    force_exit_time: time | None = None
    expected_move_min_distance_units: float | None = None
    minimum_carry_score: float | None = None
    directional_min_strength: str | None = None
    strategy_family_mode: str | None = None
    dte_bucket: str | None = None


@dataclass(frozen=True)
class ExperimentObservation:
    timestamp: object
    alpha_1: float | None
    alpha_2: float | None
    joint_direction: str
    confirmation_count: int
    candidate_count: int
    approved_count: int


def bounded_parameter_grid(
    thresholds: Sequence[float], confirmations: Sequence[int],
    windows: Sequence[tuple[time, time]], maximum: int,
    *, spread_width_sets: Sequence[tuple[float, ...]] = ((50, 100, 150, 200),),
    short_strike_buffers: Sequence[float] = (100,),
    minimum_credit_to_widths: Sequence[float] = (.03,),
    volatility_distance_multipliers: Sequence[float] = (1.0,),
    profit_targets: Sequence[float | None] = (None,),
    stop_multiples: Sequence[float | None] = (None,),
    force_exit_times: Sequence[time | None] = (None,),
    expected_move_minimums: Sequence[float | None] = (None,),
    carry_thresholds: Sequence[float | None] = (None,),
    directional_strengths: Sequence[str | None] = (None,),
    strategy_family_modes: Sequence[str | None] = (None,),
    dte_buckets: Sequence[str | None] = (None,),
) -> list[ExperimentParameters]:
    axes = (sorted(set(thresholds)), sorted(set(confirmations)), windows,
            spread_width_sets, short_strike_buffers, minimum_credit_to_widths,
            volatility_distance_multipliers, profit_targets, stop_multiples,
            force_exit_times, expected_move_minimums, carry_thresholds, directional_strengths, strategy_family_modes, dte_buckets)
    size = 1
    for axis in axes:
        size *= len(axis)
    if size == 0:
        raise ValueError("experiment grid is empty")
    if size > maximum:
        raise ValueError(f"experiment grid has {size} combinations; maximum is {maximum}")
    if (any(not .5 < value < 1 for value in thresholds)
            or any(value < 1 for value in confirmations)
            or any(start > end for start, end in windows)
            or any(not widths or any(width <= 0 for width in widths) for widths in spread_width_sets)
            or any(value < 0 for value in short_strike_buffers)
            or any(value <= 0 for value in minimum_credit_to_widths)
            or any(value < 1 for value in volatility_distance_multipliers)
            or any(value is not None and value <= 0 for value in profit_targets)
            or any(value is not None and value <= 0 for value in stop_multiples)
            or any(value is not None and value < 0 for value in expected_move_minimums)
            or any(value is not None and not 0 <= value <= 100 for value in carry_thresholds)
            or any(value not in {None, "MODERATE", "STRONG"} for value in directional_strengths)
            or any(value not in {None, "BOTH", "DIRECTIONAL_ONLY", "THETA_CARRY_ONLY"} for value in strategy_family_modes)):
        raise ValueError("experiment parameters are invalid")
    combinations = [
        ExperimentParameters(
            threshold, confirmation, start, end, spread_widths=tuple(widths),
            short_strike_buffer=buffer, minimum_credit_to_width=credit,
            volatility_distance_multiplier=volatility, profit_target_credit_capture_pct=target,
            stop_loss_credit_multiple=stop, force_exit_time=force_exit,
            expected_move_min_distance_units=move_min, minimum_carry_score=carry_min,
            directional_min_strength=strength, strategy_family_mode=family, dte_bucket=dte)
        for threshold, confirmation, (start, end), widths, buffer, credit, volatility, target, stop, force_exit, move_min, carry_min, strength, family, dte
        in product(*axes)
    ]
    return combinations


def chronological_split(items: Sequence, train: float = 0.6, validation: float = 0.2):
    if train <= 0 or validation < 0 or train + validation >= 1:
        raise ValueError("chronological split fractions are invalid")
    ordered = sorted(items, key=lambda item: item.entry_timestamp)
    train_end = int(len(ordered) * train)
    validation_end = train_end + int(len(ordered) * validation)
    boundaries = [ordered[train_end].entry_timestamp if train_end < len(ordered) else None,
                  ordered[validation_end].entry_timestamp if validation_end < len(ordered) else None]
    def purged(rows, upper):
        return [item for item in rows if upper is None or getattr(item, "exit_timestamp", None) is None
                or item.exit_timestamp < upper]
    return {
        "TRAIN": purged(ordered[:train_end], boundaries[0]),
        "VALIDATION": purged(ordered[train_end:validation_end], boundaries[1]),
        "FINAL_TEST": ordered[validation_end:],
    }


def chronological_folds(items: Sequence, folds: int = 3):
    if folds < 2:
        raise ValueError("at least two chronological folds are required")
    ordered = sorted(items, key=lambda item: item.entry_timestamp)
    size = max(1, len(ordered) // folds)
    return [ordered[index * size: (index + 1) * size if index < folds - 1 else None]
            for index in range(folds)]


def _eligible(trade: ShadowTrade, parameters: ExperimentParameters) -> bool:
    observed = trade.entry_timestamp.timetz().replace(tzinfo=None)
    if not parameters.entry_start <= observed <= parameters.entry_end:
        return False
    if trade.entry_alpha_confirmation_count < parameters.confirmations:
        return False
    if trade.entry_alpha_1 is None or trade.entry_alpha_2 is None:
        return False
    joint = trade.entry_joint_alpha_direction or ""
    alpha1_upper = parameters.alpha1_upper or parameters.alpha_threshold
    alpha2_upper = parameters.alpha2_upper or parameters.alpha_threshold
    alpha1_lower = parameters.alpha1_lower if parameters.alpha1_lower is not None else 1 - parameters.alpha_threshold
    alpha2_lower = parameters.alpha2_lower if parameters.alpha2_lower is not None else 1 - parameters.alpha_threshold
    if "BULLISH" in joint:
        return trade.entry_alpha_1 >= alpha1_upper and trade.entry_alpha_2 >= alpha2_upper
    if "BEARISH" in joint:
        return trade.entry_alpha_1 <= alpha1_lower and trade.entry_alpha_2 <= alpha2_lower
    return False


def evaluate_parameters(
    trades: list[ShadowTrade], parameters: ExperimentParameters, minimum_trades: int,
    observations: list[ExperimentObservation] | None = None,
) -> dict:
    # Recorded shadow trades cannot price an altered strike/width/time/exit.
    # This compatibility API reports diagnostic frequency only, never P&L.
    observation_rows = observations or []
    signals = [item for item in observation_rows if _observation_eligible(item, parameters)]
    candidates = [item for item in signals if item.candidate_count > 0]
    approvals = [item for item in candidates if item.approved_count > 0]
    return {
        "parameters": {
            "alpha_threshold": parameters.alpha_threshold,
            "alpha1_upper": parameters.alpha1_upper or parameters.alpha_threshold,
            "alpha1_lower": parameters.alpha1_lower if parameters.alpha1_lower is not None else 1 - parameters.alpha_threshold,
            "alpha2_upper": parameters.alpha2_upper or parameters.alpha_threshold,
            "alpha2_lower": parameters.alpha2_lower if parameters.alpha2_lower is not None else 1 - parameters.alpha_threshold,
            "confirmations": parameters.confirmations,
            "entry_window": f"{parameters.entry_start:%H:%M}-{parameters.entry_end:%H:%M}",
            "spread_widths": parameters.spread_widths,
            "short_strike_buffer": parameters.short_strike_buffer,
            "minimum_credit_to_width": parameters.minimum_credit_to_width,
            "volatility_distance_multiplier": parameters.volatility_distance_multiplier,
            "profit_target_credit_capture_pct": parameters.profit_target_credit_capture_pct,
            "stop_loss_credit_multiple": parameters.stop_loss_credit_multiple,
            "force_exit_time": parameters.force_exit_time,
            "expected_move_min_distance_units": parameters.expected_move_min_distance_units,
            "minimum_carry_score": parameters.minimum_carry_score,
            "directional_min_strength": parameters.directional_min_strength,
            "strategy_family_mode": parameters.strategy_family_mode,
            "dte_bucket": parameters.dte_bucket,
        },
        "status": "NOT_EVALUABLE",
        "reason": "RECORDED_SHADOW_OUTCOMES_CANNOT_PRICE_ALTERNATIVE_TRADES",
        "sample_status": "INSUFFICIENT_SAMPLE",
        "signal_frequency": None if not observation_rows else len(signals) / len(observation_rows),
        "candidate_frequency": None if not signals else len(candidates) / len(signals),
        "approval_frequency": None if not candidates else len(approvals) / len(candidates),
        "trades": 0,
        "win_rate": None,
        "net_pnl": None,
        "expectancy": None,
        "profit_factor": None,
        "max_drawdown": None,
        "average_mae": None,
        "average_mfe": None,
        "stop_loss_frequency": None,
        "profit_target_frequency": None,
        "average_holding_minutes": None,
    }


def _observation_eligible(item: ExperimentObservation, parameters: ExperimentParameters) -> bool:
    observed = item.timestamp.timetz().replace(tzinfo=None)
    if not parameters.entry_start <= observed <= parameters.entry_end:
        return False
    if item.confirmation_count < parameters.confirmations:
        return False
    if item.alpha_1 is None or item.alpha_2 is None:
        return False
    alpha1_upper = parameters.alpha1_upper or parameters.alpha_threshold
    alpha2_upper = parameters.alpha2_upper or parameters.alpha_threshold
    alpha1_lower = parameters.alpha1_lower if parameters.alpha1_lower is not None else 1 - parameters.alpha_threshold
    alpha2_lower = parameters.alpha2_lower if parameters.alpha2_lower is not None else 1 - parameters.alpha_threshold
    if "BULLISH" in item.joint_direction:
        return item.alpha_1 >= alpha1_upper and item.alpha_2 >= alpha2_upper
    if "BEARISH" in item.joint_direction:
        return item.alpha_1 <= alpha1_lower and item.alpha_2 <= alpha2_lower
    return False


def adjacent_robustness(results: list[dict]) -> list[dict]:
    def policy(item):
        params = item["parameters"]
        return tuple(str(params.get(key)) for key in (
            "spread_widths", "short_strike_buffer", "minimum_credit_to_width",
            "volatility_distance_multiplier", "profit_target_credit_capture_pct",
            "stop_loss_credit_multiple", "force_exit_time", "expected_move_min_distance_units",
            "minimum_carry_score", "directional_min_strength", "strategy_family_mode", "dte_bucket"))
    ordered = sorted(results, key=lambda item: (
        item["parameters"]["confirmations"], item["parameters"]["entry_window"],
        policy(item), item["parameters"]["alpha_threshold"],
    ))
    output = []
    for index, item in enumerate(ordered):
        neighbors = [candidate for candidate in ordered[max(0, index - 1):index + 2]
                     if candidate["parameters"]["confirmations"] == item["parameters"]["confirmations"]
                     and candidate["parameters"]["entry_window"] == item["parameters"]["entry_window"]
                     and policy(candidate) == policy(item)]
        pnl = [candidate["net_pnl"] for candidate in neighbors if candidate["net_pnl"] is not None]
        output.append({
            "parameters": item["parameters"],
            "neighbor_count": len(neighbors),
            "adjacent_mean_net_pnl": None if not pnl else mean(pnl),
            "fragile_peak": bool(pnl and item["net_pnl"] is not None
                                  and item["net_pnl"] > max(mean(pnl) * 2, mean(pnl) + 1)),
        })
    return output
