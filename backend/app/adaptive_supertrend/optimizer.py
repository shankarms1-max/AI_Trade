"""Bounded sequential grid and chronological signal research, never synthetic option P&L."""

from __future__ import annotations

from datetime import date, datetime, time
from math import sqrt
from statistics import mean, median, pstdev

from app.adaptive_supertrend.bars import opening_range
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.indicators import adx, atr, efficiency_ratio, supertrend
from app.parallel_paper.bars import Bar
from app.research.manifest import digest, json_value
from app.scalper.repository import IST

GRID = tuple((period, tenths) for period in range(5, 51) for tenths in range(10, 51))


def metrics(trades: list[dict], session_count: int, subperiods: int, session_dates: list[str] | None = None) -> dict:
    """Signal points only; explicit None for unsupported net/option measures."""
    pnls = [row["signal_points"] for row in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    equity = peak = drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    days = sorted(set(session_dates)) if session_dates is not None else sorted({row["date"] for row in trades})
    chunks = [
        [day for index, day in enumerate(days) if index * subperiods // max(len(days), 1) == chunk]
        for chunk in range(subperiods)
    ]
    sub = [sum(row["signal_points"] for row in trades if row["date"] in chunk) for chunk in chunks]
    avg_win, avg_loss = mean(wins) if wins else None, abs(mean(losses)) if losses else None
    return {
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "gross_signal_points": sum(pnls),
        "gross_option_pnl": None,
        "net_option_pnl": None,
        "estimated_net_pnl": None,
        "cost_status": "SIGNAL_ONLY_NO_OPTION_FILLS",
        "expectancy": mean(pnls) if pnls else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "profit_factor_status": "DEFINED" if losses else "NO_LOSSES" if wins else "NO_TRADES",
        "max_drawdown": drawdown,
        "sharpe_equivalent": (
            mean(pnls) / pstdev(pnls) * sqrt(len(pnls)) if len(pnls) > 1 and pstdev(pnls) > 0 else 0.0
        ),
        "win_rate": len(wins) / len(pnls) if pnls else 0,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
        "trade_frequency": len(trades) / session_count if session_count else 0,
        "subperiod_signal_points": sub,
        "consistency": sum(value > 0 for value in sub) / subperiods,
        "subperiod_session_dates": chunks,
    }


def _segments(bars: list[Bar]) -> list[list[Bar]]:
    result: list[list[Bar]] = []
    current: list[Bar] = []
    for bar in bars:
        if current and (getattr(current[-1], "future_id", None), getattr(current[-1], "future_expiry", None)) != (
            getattr(bar, "future_id", None),
            getattr(bar, "future_expiry", None),
        ):
            result.append(current)
            current = []
        if not bar.valid:
            if current:
                result.append(current)
                current = []
        else:
            current.append(bar)
    if current:
        result.append(current)
    return result


def _quality(bars: list[Bar], policy: AstPolicy) -> list[bool]:
    assert policy.regime is not None
    regime = policy.regime
    values = adx(bars, regime.adx_period)
    result = []
    for index, value in enumerate(values):
        er = efficiency_ratio(bars[max(0, index - regime.er_lookback): index + 1], regime.er_lookback)
        result.append(
            value is not None and er is not None and value >= regime.adx_threshold and er >= regime.er_threshold
        )
    return result


def signal_trades(
    bars: list[Bar], directions: list[dict], quality: list[bool], *, exit_policy: str = "DIRECTION_FLIP_OR_SESSION_END"
) -> list[dict]:
    if exit_policy not in {"DIRECTION_FLIP_OR_SESSION_END", "SESSION_END_ONLY"}:
        raise ValueError("AST_RESEARCH_EXIT_POLICY_UNSUPPORTED")
    trades = []
    position: dict | None = None
    pending: dict | None = None
    consumed: set[tuple] = set()
    by_day: dict[date, list[Bar]] = {}
    for index, bar in enumerate(bars):
        day = bar.start.astimezone(IST).date()
        rows = by_day.setdefault(day, [])
        # The previous completed bar may request a next-bar entry/exit, never its own close fill.
        if position is not None and (
            position["date"] != day.isoformat()
            or (
                exit_policy == "DIRECTION_FLIP_OR_SESSION_END"
                and index > 0
                and directions[index - 1]["direction"] != position["direction"]
            )
        ):
            if position["date"] == day.isoformat():
                trades.append(
                    {
                        **position,
                        "exit_at": bar.start.isoformat(),
                        "signal_points": (bar.open - position["price"]) * position["sign"],
                    }
                )
            position = None
        if pending is not None and position is None and pending["date"] == day.isoformat():
            position = {**pending, "price": bar.open, "entry_at": bar.start.isoformat()}
            consumed.add(pending["key"])
        pending = None
        rows.append(bar)
        orb = opening_range(rows, bar.end)
        latest = directions[index]
        direction = (
            "BULL"
            if orb["complete"] and bar.close > orb["opening_high"]
            else "BEAR" if orb["complete"] and bar.close < orb["opening_low"] else None
        )
        key = (day.isoformat(), direction, latest["transition"])
        if position is None and direction is not None and latest["direction"] == direction and quality[index]:
            if key not in consumed:
                pending = {
                    "date": day.isoformat(),
                    "direction": direction,
                    "key": key,
                    "sign": 1 if direction == "BULL" else -1,
                }
        # Scheduled session-end signal mark, explicitly not an executable option fill.
        if position is not None and bar.end.astimezone(IST).time() == time(15, 30):
            trades.append(
                {
                    **position,
                    "exit_at": bar.end.isoformat(),
                    "signal_points": (bar.close - position["price"]) * position["sign"],
                }
            )
            position = None
            pending = None
    # An incomplete research session is marked unavailable, never priced from a future row.
    return trades


def evaluate_grid(
    bars: list[Bar], policy: AstPolicy, *, exit_policy: str = "DIRECTION_FLIP_OR_SESSION_END"
) -> list[dict]:
    if policy.optimizer is None or policy.regime is None:
        raise ValueError("AST_POLICY_UNCONFIGURED")
    if not bars or any(left.end > right.start for left, right in zip(bars, bars[1:])):
        raise ValueError("AST_CHRONOLOGICAL_BARS_REQUIRED")
    segments = _segments(bars)
    prepared = [
        (segment, _quality(segment, policy), {period: atr(segment, period) for period in range(5, 51)})
        for segment in segments
    ]
    sessions = len({bar.start.date() for bar in bars})
    result = []
    for period, tenths in GRID:
        trades = []
        for segment, quality, atrs in prepared:
            trades.extend(
                signal_trades(
                    segment, supertrend(segment, period, tenths, atrs[period]), quality, exit_policy=exit_policy
                )
            )
        result.append(
            {
                "atr_period": period,
                "multiplier_tenths": tenths,
                "metrics": metrics(
                    trades,
                    sessions,
                    policy.optimizer.subperiods,
                    sorted({bar.start.date().isoformat() for bar in bars}),
                ),
            }
        )
    return rank(result, policy)


def rank(rows: list[dict], policy: AstPolicy) -> list[dict]:
    assert policy.optimizer is not None
    weights = policy.optimizer.weights.model_dump()
    eligible = [
        row
        for row in rows
        if row["metrics"]["total_trades"] >= policy.optimizer.minimum_trades
        and row["metrics"]["gross_signal_points"] > 0
    ]
    for row in rows:
        row.update(eligible=row in eligible, base_score=0.0, score=0.0)
    keys = {
        "profitability": "gross_signal_points",
        "drawdown": "max_drawdown",
        "profit_factor": "profit_factor",
        "sharpe": "sharpe_equivalent",
        "frequency": "trade_frequency",
        "consistency": "consistency",
    }
    for name, key in keys.items():

        def value(row):
            raw = row["metrics"][key]
            if raw is None:
                raw = float("inf") if key == "profit_factor" and row["metrics"]["winning_trades"] else 0
            return -raw if name == "drawdown" else raw

        values = sorted({value(row) for row in eligible})
        scores = {val: index / max(len(values) - 1, 1) for index, val in enumerate(values)}
        for row in eligible:
            row["base_score"] += weights[name] * scores[value(row)]
    lookup = {(row["atr_period"], row["multiplier_tenths"]): row for row in rows}
    for row in rows:
        p, m = row["atr_period"], row["multiplier_tenths"]
        neighbours = [lookup[key] for key in ((p - 1, m), (p + 1, m), (p, m - 1), (p, m + 1)) if key in lookup]
        row["neighbour_robustness"] = {
            "count": len(neighbours),
            "eligible_fraction": sum(n["eligible"] for n in neighbours) / len(neighbours),
            "median_base_score": median(n["base_score"] for n in neighbours),
        }
        row["score"] = row["base_score"] + weights["neighbours"] * row["neighbour_robustness"]["median_base_score"]
    rows.sort(key=lambda row: (not row["eligible"], -row["score"], row["atr_period"], row["multiplier_tenths"]))
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows


def optimize(bars: list[Bar], trading_date: date, selected_at: datetime, policy: AstPolicy) -> dict:
    cutoff = datetime.combine(trading_date, time(9, 15), IST)
    if (
        selected_at.tzinfo is None
        or selected_at >= cutoff
        or any(
            bar.end > selected_at or bar.end >= cutoff or bar.start.astimezone(IST).date() >= trading_date
            for bar in bars
        )
    ):
        raise ValueError("AST_TRAINING_CUTOFF_VIOLATION")
    if policy.missing:
        raise ValueError("AST_POLICY_UNCONFIGURED")
    assert policy.optimizer is not None
    days = sorted({bar.start.date() for bar in bars})
    if len(days) < policy.optimizer.lookback_sessions:
        raise ValueError("AST_OPTIMIZER_HISTORY_INSUFFICIENT")
    selected_days = set(days[-policy.optimizer.lookback_sessions:])
    training = [bar for bar in bars if bar.start.date() in selected_days]
    rows = evaluate_grid(training, policy)
    selected = rows[0] if rows[0]["eligible"] else None
    inputs = json_value(training)
    return {
        "strategy_id": "AST_V1",
        "trading_date": trading_date.isoformat(),
        "selected_at": selected_at.isoformat(),
        "data_cutoff": max(bar.end for bar in training).isoformat(),
        "policy_hash": policy.policy_hash,
        "policy": policy.model_dump(mode="json"),
        "training_data_identity": digest(inputs),
        "training_bars": inputs,
        "grid_count": len(rows),
        "ranking": rows,
        "selected": selected,
        "walk_forward_status": "NOT_RUN",
        "result_layer": "SIGNAL_ONLY",
    }


def walk_forward(bars: list[Bar], policy: AstPolicy) -> list[dict]:
    if policy.optimizer is None:
        raise ValueError("AST_POLICY_UNCONFIGURED")
    cfg = policy.optimizer
    days = sorted({bar.start.date() for bar in bars})
    result = []
    cursor = cfg.lookback_sessions
    while cursor + cfg.validation_sessions + cfg.out_of_sample_sessions <= len(days):
        train_days = days[cursor - cfg.lookback_sessions: cursor]
        validation_days = days[cursor: cursor + cfg.validation_sessions]
        test_days = days[
            cursor + cfg.validation_sessions: cursor + cfg.validation_sessions + cfg.out_of_sample_sessions
        ]
        training = [bar for bar in bars if bar.start.date() in train_days]
        ranking = evaluate_grid(training, policy)
        selected = ranking[0] if ranking[0]["eligible"] else None
        record = {
            "train_dates": [str(day) for day in train_days],
            "validation_dates": [str(day) for day in validation_days],
            "test_dates": [str(day) for day in test_days],
            "selected": selected,
            "validation": None,
            "out_of_sample": None,
        }
        if selected:
            for key, group in (("validation", validation_days), ("out_of_sample", test_days)):
                # Warm-up uses the causal training prefix; only exits whose entries belong to this period count.
                end = group[-1]
                prefix = [bar for bar in bars if train_days[0] <= bar.start.date() <= end]
                trades: list[dict] = []
                for segment in _segments(prefix):
                    sequence = supertrend(segment, selected["atr_period"], selected["multiplier_tenths"])
                    trades.extend(
                        row
                        for row in signal_trades(segment, sequence, _quality(segment, policy))
                        if date.fromisoformat(row["date"]) in group
                    )
                record[key] = metrics(trades, len(group), cfg.subperiods, [day.isoformat() for day in group])
        result.append(record)
        cursor += cfg.validation_sessions + cfg.out_of_sample_sessions
    return result
