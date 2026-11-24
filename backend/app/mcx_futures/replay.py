"""Historical replay of 1-minute bars through the unchanged paper engine.

Input: a DataFrame with ``ts`` (tz-aware, minute start, IST), ``open``,
``high``, ``low``, ``close``, ``volume`` and optional ``bid`` / ``ask``.

Quote synthesis (documented assumptions, never look-ahead):
- each bar becomes observations at +0 s (open), +15 s, +30 s (the two
  extremes) and +45 s (close), stamped fresh (``source_ts == at``);
- when bid/ask are absent the quote is ``ltp -/+ half_spread_ticks x tick``
  (sensitivity scenarios 0.5 / 1 / 2 ticks); when present, the bar's quoted
  half spread (ask - bid) / 2 is applied around every path price;
- the order of the two extremes is chosen CONSERVATIVELY from the engine's
  state after the open observation: a pending LONG entry sees the high first
  (fills at the worst price) and an open LONG sees the low first (stop tested
  first); mirror for SHORT; flat uses O-L-H-C for up bars and O-H-L-C otherwise
  (fills cannot happen while flat, so this only shapes the bar);
- intrabar stops: if a path segment crosses the stop, an observation is
  inserted exactly where the executable side touches the stop, so the stop
  fills at the stop (plus slippage ticks); if the bar OPENS beyond the stop,
  the open observation fills at the worse open price (gap-through).
Decisions still happen only on completed bars and fills only on later
observations, exactly as live.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import isfinite, sqrt
from typing import Any, Iterable

import pandas as pd

from app.mcx_futures.bars import Observation
from app.mcx_futures.config import InstrumentConfig
from app.mcx_futures.costs import CostSchedule
from app.mcx_futures.engine import McxFuturesEngine
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.spec import IST, ContractSpec

DEFAULT_HALF_SPREAD_TICKS = 1.0
SPREAD_SCENARIOS = (0.5, 1.0, 2.0)
REQUIRED_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


class ReplayInputError(ValueError):
    pass


def validate_bars(bars: pd.DataFrame) -> list[dict[str, Any]]:
    missing = [name for name in REQUIRED_COLUMNS if name not in bars.columns]
    if missing:
        raise ReplayInputError(f"MISSING_COLUMNS:{','.join(missing)}")
    rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    has_quote = "bid" in bars.columns and "ask" in bars.columns
    for record in bars.to_dict("records"):
        ts = record["ts"]
        ts = ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else ts
        if not isinstance(ts, datetime) or ts.tzinfo is None:
            raise ReplayInputError("TIMESTAMP_NOT_TZ_AWARE")
        ts = ts.astimezone(IST)
        if ts.second or ts.microsecond:
            raise ReplayInputError("TIMESTAMP_NOT_MINUTE_ALIGNED")
        if previous is not None and ts <= previous:
            raise ReplayInputError("TIMESTAMPS_NOT_STRICTLY_INCREASING")
        o, h, low, c = (float(record[k]) for k in ("open", "high", "low", "close"))
        if not all(isfinite(x) and x > 0 for x in (o, h, low, c)) or h < max(o, c) or low > min(o, c):
            raise ReplayInputError(f"OHLC_INCONSISTENT:{ts.isoformat()}")
        volume = record.get("volume")
        volume = None if volume is None or (isinstance(volume, float) and not isfinite(volume)) else float(volume)
        bid = ask = None
        if has_quote:
            b, a = record.get("bid"), record.get("ask")
            if b is not None and a is not None and isfinite(float(b)) and isfinite(float(a)) and a >= b:
                bid, ask = float(b), float(a)
        rows.append({"ts": ts, "open": o, "high": h, "low": low, "close": c, "volume": volume, "bid": bid,
                     "ask": ask})
        previous = ts
    return rows


class _Feed:
    def __init__(self, engine: McxFuturesEngine, tick: float, half_spread_ticks: float,
                 collect: bool) -> None:
        self.engine = engine
        self.tick = tick
        self.half_spread_ticks = half_spread_ticks
        self.collect = collect
        self.events: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.event_counts: dict[str, int] = defaultdict(int)
        self.cum: dict[str, float] = {}

    def send(self, at: datetime, ltp: float, half: float, cum: float | None) -> None:
        obs = Observation(at=at, ltp=ltp, bid=ltp - half, ask=ltp + half, cum_volume=cum, source_ts=at)
        for event in self.engine.on_observation(obs):
            self.event_counts[event["type"]] += 1
            if event["type"] == "EXIT":
                self.trades.append(event["payload"]["trade"])
            if self.collect:
                self.events.append(event)

    def stop_touch(self, a: float, b: float, half: float) -> float | None:
        position = self.engine.position
        if position is None:
            return None
        stop = position["stop"]
        if position["direction"] == "LONG":
            level = stop + half
            return level if a > level >= b else None
        level = stop - half
        return level if a < level <= b else None

    def bar(self, row: dict[str, Any]) -> None:
        ts: datetime = row["ts"]
        day = ts.date().isoformat()
        before = self.cum.get(day, 0.0)
        volume = row["volume"]
        after = None if volume is None else before + volume
        if after is not None:
            self.cum[day] = after
        cum_before = None if volume is None else before
        half = ((row["ask"] - row["bid"]) / 2 if row["bid"] is not None and row["ask"] is not None
                else self.half_spread_ticks * self.tick)
        self.send(ts, row["open"], half, cum_before)
        pending = self.engine.s["pending_entry"]
        position = self.engine.position
        side = (pending or position or {}).get("direction")
        if pending is not None:
            first, second = (row["high"], row["low"]) if side == "LONG" else (row["low"], row["high"])
        elif position is not None:
            first, second = (row["low"], row["high"]) if side == "LONG" else (row["high"], row["low"])
        else:
            first, second = (row["low"], row["high"]) if row["close"] >= row["open"] else (row["high"], row["low"])
        path = [(15, first, cum_before), (30, second, cum_before), (45, row["close"], after)]
        previous_price, previous_second = row["open"], 0
        for second_offset, price, cum in path:
            touch = self.stop_touch(previous_price, price, half)
            if touch is not None:
                mid = previous_second + (second_offset - previous_second) / 2
                self.send(ts + timedelta(seconds=mid), touch, half, cum_before)
            self.send(ts + timedelta(seconds=second_offset), price, half, cum)
            previous_price, previous_second = price, second_offset


def replay(bars: pd.DataFrame, config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule, *,
           half_spread_ticks: float = DEFAULT_HALF_SPREAD_TICKS, calendar: SessionCalendar | None = None,
           collect_events: bool = False) -> dict[str, Any]:
    if half_spread_ticks < 0:
        raise ReplayInputError("HALF_SPREAD_NEGATIVE")
    rows = validate_bars(bars)
    engine = McxFuturesEngine(config, spec, costs, calendar=calendar)
    feed = _Feed(engine, spec.tick_size, half_spread_ticks, collect_events)
    for row in rows:
        feed.bar(row)
    return {
        "config_id": config.config_id,
        "trading_symbol": spec.trading_symbol,
        "half_spread_ticks": half_spread_ticks,
        "bars": len(rows),
        "trades": feed.trades,
        "metrics": metrics(feed.trades),
        "open_position_at_end": engine.position,
        "event_counts": dict(feed.event_counts),
        "events": feed.events if collect_events else None,
        "assumptions": {
            "quote": "bar bid/ask half spread" if "bid" in bars.columns else f"ltp +/- {half_spread_ticks} ticks",
            "path": "O,+15s extreme,+30s extreme,+45s close; adverse extreme first for pending/open positions",
            "intrabar_stop": "fill at stop (+slippage) when crossed inside the bar; at open when the bar gaps through",
        },
    }


def spread_sensitivity(bars: pd.DataFrame, config: InstrumentConfig, spec: ContractSpec, costs: CostSchedule,
                       scenarios: Iterable[float] = SPREAD_SCENARIOS,
                       calendar: SessionCalendar | None = None) -> dict[str, Any]:
    return {str(item): replay(bars, config, spec, costs, half_spread_ticks=item, calendar=calendar)["metrics"]
            for item in scenarios}


def metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    net = [float(t["net_pnl"]) for t in trades]
    gross = [float(t["gross_pnl"]) for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    equity = peak = drawdown = 0.0
    streak = worst_streak = 0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        streak = streak + 1 if value <= 0 else 0
        worst_streak = max(worst_streak, streak)
    by_day: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_day[datetime.fromisoformat(trade["exit_at"]).astimezone(IST).date().isoformat()] += float(trade["net_pnl"])
    cost_totals: dict[str, float] = defaultdict(float)
    for trade in trades:
        for name, value in trade["costs"]["items"].items():
            cost_totals[name] += value
    n = len(trades)
    mean = sum(net) / n if n else None
    std = sqrt(sum((x - (mean or 0.0)) ** 2 for x in net) / (n - 1)) if n > 1 else None
    loss_sum = -sum(losses)
    worst_day = min(by_day.items(), key=lambda item: item[1]) if by_day else None
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n, 6) if n else None,
        "gross_pnl": round(sum(gross), 4),
        "net_pnl": round(sum(net), 4),
        "expectancy": round(mean, 4) if mean is not None else None,
        "t_stat": round(mean / (std / sqrt(n)), 4) if mean is not None and std else None,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "profit_factor": round(sum(wins) / loss_sum, 4) if loss_sum > 0 else None,
        "max_drawdown": round(drawdown, 4),
        "max_consecutive_losses": worst_streak,
        "avg_holding_minutes": round(sum(t["holding_seconds"] for t in trades) / n / 60, 4) if n else None,
        "avg_mae_rupees": round(sum(t["mae_rupees"] for t in trades) / n, 4) if n else None,
        "avg_mfe_rupees": round(sum(t["mfe_rupees"] for t in trades) / n, 4) if n else None,
        "cost_totals": {name: round(value, 4) for name, value in sorted(cost_totals.items())},
        "total_costs": round(sum(t["costs"]["total"] for t in trades), 4),
        "worst_day": {"date": worst_day[0], "net_pnl": round(worst_day[1], 4)} if worst_day else None,
        "exit_reasons": dict(sorted(_count(t["exit_reason"] for t in trades).items())),
    }


def _count(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for item in items:
        out[item] += 1
    return dict(out)
