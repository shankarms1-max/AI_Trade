"""Pre-declared research harness: chronological splits, walk-forward, baselines, fragility.

Nothing here may manufacture evidence:
- data sufficiency is checked first; missing bars or too few sessions return
  ``INSUFFICIENT_DATA`` with reasons and no performance numbers;
- the configuration grid is declared up front and EVERY evaluated config is
  registered (``evaluated_config_count``);
- selection uses the TRAIN segment only (penalised by fragility = dispersion of
  train P&L across the config and its one-step grid neighbours); validation and
  out-of-sample (OOS) are reported, never used for selection;
- each config is replayed once over the full history (the strategy is causal
  and has no in-sample fitting), and trades are attributed to the segment of
  their ENTRY session;
- baselines with the same cost model: no-trade, and a simple EMA-cross
  directional benchmark;
- a verdict of CREDIBLE_EDGE_CANDIDATE needs every pre-declared check to pass;
  anything else is NO_CREDIBLE_EDGE.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from statistics import pstdev
from typing import Any, Sequence

import pandas as pd

from app.mcx_futures.config import InstrumentConfig
from app.mcx_futures.costs import CostSchedule, round_trip
from app.mcx_futures.features import ema_series
from app.mcx_futures.replay import ReplayInputError, metrics, replay, validate_bars
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.spec import ContractSpec

DEFAULT_GRID: dict[str, tuple[Any, ...]] = {
    "entry_score_floor": (55.0, 60.0, 65.0, 70.0),
    "stop_atr_multiple": (1.25, 1.5, 2.0),
    "feature_timeframe_minutes": (3, 5),
}
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class ResearchPlan:
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    walk_forward_folds: int = 4
    min_sessions: int = 20
    min_session_coverage: float = 0.95
    min_train_trades: int = 10
    min_oos_trades: int = 30
    min_oos_t_stat: float = 2.0
    min_oos_profit_factor: float = 1.2
    min_positive_fold_fraction: float = 0.75
    fragility_lambda: float = 1.0
    half_spread_ticks: float = 1.0
    ema_baseline_fast: int = 9
    ema_baseline_slow: int = 21


def grid_configs(base: InstrumentConfig,
                 grid: dict[str, Sequence[Any]]) -> list[tuple[dict[str, Any], InstrumentConfig]]:
    names = sorted(grid)
    out = []
    for values in product(*(grid[name] for name in names)):
        params = dict(zip(names, values))
        out.append((params, base.with_overrides(**params)))
    return out


def check_data(bars: pd.DataFrame, calendar: SessionCalendar, plan: ResearchPlan) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        rows = validate_bars(bars)
    except ReplayInputError as exc:
        return {"ok": False, "reasons": [f"INVALID_INPUT:{exc}"], "sessions": []}
    per_day: dict[str, int] = defaultdict(int)
    outside = 0
    for row in rows:
        day = calendar.day(row["ts"].date())
        if day is None or not day.open_at <= row["ts"] < day.close_at:
            outside += 1
            continue
        per_day[day.session_id] += 1
    sessions = sorted(per_day)
    if outside:
        reasons.append(f"BARS_OUTSIDE_SESSION_IGNORED:{outside}")
    short = []
    for session_id in sessions:
        day = calendar.day(datetime.fromisoformat(session_id).date())
        assert day is not None
        expected = int((day.close_at - day.open_at).total_seconds() // 60)
        coverage = per_day[session_id] / expected
        if coverage < plan.min_session_coverage:
            short.append(f"{session_id}:{coverage:.3f}")
    ok = True
    if short:
        ok = False
        reasons.append(f"MISSING_BARS:{len(short)} sessions below coverage {plan.min_session_coverage}: "
                       + ",".join(short[:10]))
    if len(sessions) < plan.min_sessions:
        ok = False
        reasons.append(f"TOO_FEW_SESSIONS:{len(sessions)}<{plan.min_sessions}")
    return {"ok": ok, "reasons": reasons, "sessions": sessions}


def _session_of(trade: dict[str, Any]) -> str:
    return datetime.fromisoformat(trade["entry_at"]).date().isoformat()


def _subset(trades: list[dict[str, Any]], sessions: set[str]) -> list[dict[str, Any]]:
    return [trade for trade in trades if _session_of(trade) in sessions]


def _neighbours(index: int, params_list: list[dict[str, Any]], grid: dict[str, Sequence[Any]]) -> list[int]:
    me = params_list[index]
    out = []
    for j, other in enumerate(params_list):
        if j == index:
            continue
        diffs = [name for name in grid if other[name] != me[name]]
        if len(diffs) == 1:
            name = diffs[0]
            values = list(grid[name])
            if abs(values.index(other[name]) - values.index(me[name])) == 1:
                out.append(j)
    return out


def _select(trade_sets: list[list[dict[str, Any]]], params_list: list[dict[str, Any]],
            grid: dict[str, Sequence[Any]], sessions: set[str], plan: ResearchPlan) -> tuple[int | None, list[dict]]:
    nets = []
    counts = []
    for trades in trade_sets:
        subset = _subset(trades, sessions)
        nets.append(sum(float(t["net_pnl"]) for t in subset))
        counts.append(len(subset))
    ranking = []
    best: int | None = None
    best_score = float("-inf")
    for i in range(len(trade_sets)):
        group = [nets[i]] + [nets[j] for j in _neighbours(i, params_list, grid)]
        fragility = pstdev(group) if len(group) > 1 else 0.0
        eligible = counts[i] >= plan.min_train_trades
        penalised = nets[i] - plan.fragility_lambda * fragility
        ranking.append({"index": i, "train_net": round(nets[i], 4), "train_trades": counts[i],
                        "fragility": round(fragility, 4), "penalised": round(penalised, 4), "eligible": eligible})
        if eligible and penalised > best_score:
            best, best_score = i, penalised
    return best, ranking


def ema_cross_baseline(bars: pd.DataFrame, config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule,
                       calendar: SessionCalendar, plan: ResearchPlan) -> list[dict[str, Any]]:
    """Always-in-market EMA(fast) vs EMA(slow) on feature-timeframe closes, flat at forced exit.

    The signal at a bucket close is executed at the NEXT 1-minute bar open with
    the same half-spread assumption and the same cost schedule.
    """
    rows = validate_bars(bars)
    half = plan.half_spread_ticks * spec.tick_size
    minutes = config.feature_timeframe_minutes
    closes: list[float] = []
    trades: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    target = 0
    for row in rows:
        ts = row["ts"]
        day = calendar.day(ts.date())
        if day is None or not day.open_at <= ts < day.close_at:
            continue
        last_entry, forced = day.cutoffs(config.last_entry_minutes_before_close,
                                         config.forced_exit_minutes_before_close)

        def close_position(price: float, at: datetime) -> None:
            nonlocal position
            assert position is not None
            sign = 1 if position["direction"] == "LONG" else -1
            exit_price = price - sign * half
            gross = (exit_price - position["entry_price"]) * sign * spec.lot_size * config.lots
            cost = round_trip(costs, position["entry_price"], exit_price, position["direction"], spec.lot_size,
                              config.lots)
            trades.append({"entry_at": position["entry_at"], "exit_at": at.isoformat(),
                           "direction": position["direction"], "gross_pnl": round(gross, 6), "costs": cost,
                           "net_pnl": round(gross - cost["total"], 6), "exit_reason": "BASELINE",
                           "holding_seconds": (at - datetime.fromisoformat(position["entry_at"])).total_seconds(),
                           "mae_rupees": 0.0, "mfe_rupees": 0.0})
            position = None

        if position is not None and (ts >= forced or position["session"] != day.session_id):
            close_position(row["open"], ts)
        if ts < forced:
            if position is not None and target != (1 if position["direction"] == "LONG" else -1):
                close_position(row["open"], ts)
            if position is None and target != 0 and ts < last_entry:
                direction = "LONG" if target > 0 else "SHORT"
                position = {"direction": direction, "entry_at": ts.isoformat(), "session": day.session_id,
                            "entry_price": row["open"] + (half if target > 0 else -half)}
        if (ts.hour * 60 + ts.minute) % minutes == minutes - 1:
            closes.append(row["close"])
            fast = ema_series(closes[-200:], plan.ema_baseline_fast)[-1]
            slow = ema_series(closes[-200:], plan.ema_baseline_slow)[-1]
            target = 0 if fast is None or slow is None or fast == slow else (1 if fast > slow else -1)
    return trades


def _regime(trade: dict[str, Any], config: InstrumentConfig) -> str:
    evidence = trade.get("entry_evidence") or {}
    pct, er = evidence.get("atr_percentile"), evidence.get("efficiency_ratio")
    if pct is not None and pct >= 0.8:
        return "HIGH_VOL"
    if er is not None and er >= config.chop_er_threshold:
        return "TREND"
    if er is None:
        return "UNKNOWN"
    return "CHOP"


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    m = metrics(trades)
    keys = ("trades", "win_rate", "net_pnl", "gross_pnl", "expectancy", "t_stat", "profit_factor", "max_drawdown",
            "max_consecutive_losses", "total_costs", "worst_day")
    return {key: m[key] for key in keys}


def run_research(bars: pd.DataFrame, base_config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule, *,
                 grid: dict[str, Sequence[Any]] | None = None, plan: ResearchPlan | None = None,
                 calendar: SessionCalendar | None = None) -> dict[str, Any]:
    grid = dict(DEFAULT_GRID if grid is None else grid)
    plan = plan or ResearchPlan()
    calendar = calendar or SessionCalendar()
    data = check_data(bars, calendar, plan)
    base = {"strategy": base_config.config_id, "calibration_status": base_config.calibration_status,
            "grid": {name: list(values) for name, values in grid.items()}, "plan": asdict(plan),
            "data_reasons": data["reasons"], "sessions": len(data["sessions"])}
    if not data["ok"]:
        return {**base, "status": INSUFFICIENT_DATA, "evaluated_config_count": 0, "registry": [],
                "verdict": INSUFFICIENT_DATA}
    sessions = data["sessions"]
    n = len(sessions)
    n_train = int(n * plan.train_fraction)
    n_val = int(n * plan.validation_fraction)
    segments = {"train": set(sessions[:n_train]), "validation": set(sessions[n_train:n_train + n_val]),
                "oos": set(sessions[n_train + n_val:])}
    candidates = grid_configs(base_config, grid)
    params_list = [params for params, _ in candidates]
    trade_sets: list[list[dict[str, Any]]] = []
    registry = []
    for params, cfg in candidates:
        result = replay(bars, cfg, spec, costs, half_spread_ticks=plan.half_spread_ticks, calendar=calendar)
        trade_sets.append(result["trades"])
        registry.append({"config_id": cfg.config_id, "config_hash": cfg.config_hash, "params": params,
                         **{name: _summary(_subset(result["trades"], segment)) for name, segment in segments.items()}})
    selected, ranking = _select(trade_sets, params_list, grid, segments["train"], plan)
    for row in ranking:
        registry[row["index"]]["selection"] = {k: v for k, v in row.items() if k != "index"}
    # Walk-forward (expanding train window, next block as test).
    blocks: list[list[str]] = []
    size = max(1, n // (plan.walk_forward_folds + 1))
    for k in range(plan.walk_forward_folds + 1):
        blocks.append(sessions[k * size:(k + 1) * size] if k < plan.walk_forward_folds else sessions[k * size:])
    folds: list[dict[str, Any]] = []
    for k in range(1, plan.walk_forward_folds + 1):
        train = {s for block in blocks[:k] for s in block}
        test = set(blocks[k])
        chosen, _ = _select(trade_sets, params_list, grid, train, plan)
        folds.append({"fold": k, "train_sessions": len(train), "test_sessions": len(test),
                      "selected": registry[chosen]["config_id"] if chosen is not None else None,
                      "test": _summary(_subset(trade_sets[chosen], test)) if chosen is not None else None})
    reference = candidates[selected][1] if selected is not None else base_config
    baseline_trades = ema_cross_baseline(bars, reference, spec, costs, calendar, plan)
    baselines = {
        "no_trade": {name: _summary([]) for name in segments},
        "ema_cross": {name: _summary(_subset(baseline_trades, segment)) for name, segment in segments.items()},
    }
    out: dict[str, Any] = {**base, "status": "OK", "evaluated_config_count": len(registry), "registry": registry,
                           "segments": {name: sorted(seg)[:1] + sorted(seg)[-1:] for name, seg in segments.items()},
                           "walk_forward": folds, "baselines": baselines}
    if selected is None:
        out.update(selected=None, verdict="NO_CREDIBLE_EDGE", failed_checks=["NO_CONFIG_MET_MIN_TRAIN_TRADES"])
        return out
    oos_trades = _subset(trade_sets[selected], segments["oos"])
    oos = _summary(oos_trades)
    regimes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in oos_trades:
        regimes[_regime(trade, reference)].append(trade)
    tested: list[dict[str, Any]] = [fold["test"] for fold in folds if fold["test"] is not None]
    positive = sum(1 for test in tested if test["net_pnl"] > 0)
    checks = {
        "OOS_MIN_TRADES": oos["trades"] >= plan.min_oos_trades,
        "OOS_NET_POSITIVE": oos["net_pnl"] > 0,
        "OOS_T_STAT": (oos["t_stat"] or 0.0) >= plan.min_oos_t_stat,
        "OOS_PROFIT_FACTOR": (oos["profit_factor"] or 0.0) >= plan.min_oos_profit_factor,
        "WALK_FORWARD_CONSISTENCY": bool(tested) and positive / len(tested) >= plan.min_positive_fold_fraction,
        "BEATS_EMA_BASELINE_OOS": oos["net_pnl"] > baselines["ema_cross"]["oos"]["net_pnl"],
        "BEATS_NO_TRADE_OOS": oos["net_pnl"] > 0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    out.update(
        selected={"config_id": registry[selected]["config_id"], "params": params_list[selected],
                  "train": registry[selected]["train"], "validation": registry[selected]["validation"], "oos": oos},
        regimes={name: _summary(items) for name, items in sorted(regimes.items())},
        checks=checks,
        failed_checks=failed,
        verdict="CREDIBLE_EDGE_CANDIDATE" if not failed else "NO_CREDIBLE_EDGE",
        disclaimer="Research output on the supplied bars only; PRE_REGISTERED_UNVALIDATED configs; not a forecast.",
    )
    return out


def walk_forward_dates(sessions: list[str], folds: int) -> list[tuple[str, str]]:
    """Helper for reports: (first, last) session of each walk-forward block."""
    size = max(1, len(sessions) // (folds + 1))
    out = []
    for k in range(folds + 1):
        block = sessions[k * size:(k + 1) * size] if k < folds else sessions[k * size:]
        if block:
            out.append((block[0], block[-1]))
    return out


__all__ = ["DEFAULT_GRID", "INSUFFICIENT_DATA", "ResearchPlan", "check_data", "ema_cross_baseline", "grid_configs",
           "run_research", "walk_forward_dates"]
