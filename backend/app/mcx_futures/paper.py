"""Conservative paper execution for MCX outright futures (no broker, no network).

- Market entry fills at the NEXT observation after the decision, at the ask
  (LONG) / bid (SHORT), only when that quote is fresh, two-sided, inside the
  spread limit and within the entry latency; otherwise ENTRY_REJECTED with the
  exact reason (the cycle reservation is released, not consumed).
- Protective stop is checked on every fresh observation against the bid (LONG)
  / ask (SHORT) and fills at the WORSE of the stop and the observed executable
  price (gap-through), plus ``stop_slippage_ticks``.
- Stale quotes are never marked (MARK_UNAVAILABLE); a stale-feed exit happens
  only on the next fresh quote.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.mcx_futures.bars import Observation
from app.mcx_futures.costs import CostSchedule, round_trip
from app.mcx_futures.spec import ContractSpec, round_to_tick

LONG, SHORT = "LONG", "SHORT"


def quote_problem(obs: Observation, cfg: Any, spec: ContractSpec) -> str | None:
    if obs.bid is None or obs.ask is None:
        return "BID_ASK_UNAVAILABLE"
    age = obs.quote_age_seconds()
    if age is None:
        return "QUOTE_AGE_UNAVAILABLE"
    if age < 0:
        return "FUTURE_QUOTE_TIMESTAMP"
    if age > cfg.stale_quote_seconds:
        return "STALE_QUOTE"
    return None


def depth_problem(obs: Observation, direction: str, lots: int, spec: ContractSpec,
                  *, exit_side: bool = False) -> str | None:
    """Kotak best-level quantities are underlying units; require enough on the side we hit."""
    side = "bid" if (direction == SHORT) != exit_side else "ask"
    quantity = obs.bid_qty if side == "bid" else obs.ask_qty
    if quantity is None:
        return f"{side.upper()}_DEPTH_UNAVAILABLE"
    if quantity < lots * spec.lot_size:
        return f"{side.upper()}_DEPTH_INSUFFICIENT"
    return None


def spread_ticks(obs: Observation, spec: ContractSpec) -> float | None:
    if obs.bid is None or obs.ask is None:
        return None
    return (obs.ask - obs.bid) / spec.tick_size


def entry_fill(pending: dict[str, Any], obs: Observation, cfg: Any, spec: ContractSpec,
               extra_blockers: list[str]) -> tuple[float | None, str | None]:
    """(fill price, None) or (None, rejection reason)."""
    latency = (obs.at - datetime.fromisoformat(pending["decided_at"])).total_seconds()
    if latency > cfg.max_entry_latency_seconds:
        return None, "ENTRY_LATENCY_EXCEEDED"
    problem = quote_problem(obs, cfg, spec)
    if problem:
        return None, problem
    problem = depth_problem(obs, pending["direction"], cfg.lots, spec)
    if problem:
        return None, problem
    spread = spread_ticks(obs, spec)
    if spread is None or spread > cfg.max_spread_ticks:
        return None, "SPREAD_TOO_WIDE"
    if extra_blockers:
        return None, extra_blockers[0]
    slip = cfg.entry_slippage_ticks * spec.tick_size
    assert obs.bid is not None and obs.ask is not None  # guaranteed by quote_problem
    price = obs.ask + slip if pending["direction"] == LONG else obs.bid - slip
    return price, None


def open_position(pending: dict[str, Any], obs: Observation, price: float, cfg: Any,
                  spec: ContractSpec) -> dict[str, Any]:
    sign = 1 if pending["direction"] == LONG else -1
    points = float(pending["stop_points"])
    stop = round_to_tick(price - sign * points, spec.tick_size, "DOWN" if sign > 0 else "UP")
    return {
        "direction": pending["direction"],
        "sign": sign,
        "lots": cfg.lots,
        "entry_at": obs.at.isoformat(),
        "decided_at": pending["decided_at"],
        "entry_price": price,
        "stop": stop,
        "initial_stop": stop,
        "stop_points": points,
        "entry_atr": pending["atr"],
        "best_price": price,
        "mae_points": 0.0,
        "mfe_points": 0.0,
        "trail_active": False,
        "entry_reason": pending["entry_reason"],
        "cycle_id": pending["cycle_id"],
        "score": pending["score"],
        "entry_evidence": pending.get("entry_evidence", {}),
        "stale_since": None,
        "exit_pending": None,
    }


def executable(position: dict[str, Any], obs: Observation) -> float | None:
    return obs.bid if position["direction"] == LONG else obs.ask


def update_excursions(position: dict[str, Any], price: float) -> None:
    move = (price - position["entry_price"]) * position["sign"]
    position["mfe_points"] = round(max(position["mfe_points"], move), 10)
    position["mae_points"] = round(max(position["mae_points"], -move), 10)
    if position["sign"] * (price - position["best_price"]) > 0:
        position["best_price"] = price


def stop_fill(position: dict[str, Any], obs: Observation, cfg: Any, spec: ContractSpec) -> float | None:
    price = executable(position, obs)
    if price is None:
        return None
    slip = cfg.stop_slippage_ticks * spec.tick_size
    eps = spec.tick_size * 1e-6  # float tolerance only; far below one tick
    if position["direction"] == LONG and price <= position["stop"] + eps:
        return min(position["stop"], price) - slip
    if position["direction"] == SHORT and price >= position["stop"] - eps:
        return max(position["stop"], price) + slip
    return None


def update_trailing(position: dict[str, Any], atr: float | None, cfg: Any, spec: ContractSpec) -> bool:
    """On a completed bar: activate after MFE >= activation x entry ATR, then trail from the best price."""
    if atr is None or atr <= 0:
        return False
    if not position["trail_active"] and position["mfe_points"] >= cfg.trail_activation_atr * position["entry_atr"]:
        position["trail_active"] = True
    if not position["trail_active"]:
        return False
    sign = position["sign"]
    candidate = round_to_tick(position["best_price"] - sign * cfg.trail_atr * atr, spec.tick_size,
                              "DOWN" if sign > 0 else "UP")
    if sign * (candidate - position["stop"]) > 0:
        position["stop"] = candidate
        return True
    return False


def close(position: dict[str, Any], at: datetime, price: float, reason: str, schedule: CostSchedule,
          spec: ContractSpec) -> dict[str, Any]:
    update_excursions(position, price)
    lots = position["lots"]
    gross = round((price - position["entry_price"]) * position["sign"] * spec.lot_size * lots, 6)
    costs = round_trip(schedule, position["entry_price"], price, position["direction"], spec.lot_size, lots)
    entry_at = datetime.fromisoformat(position["entry_at"])
    return {
        "instrument": spec.instrument,
        "trading_symbol": spec.trading_symbol,
        "direction": position["direction"],
        "lots": lots,
        "lot_size": spec.lot_size,
        "entry_at": position["entry_at"],
        "exit_at": at.isoformat(),
        "entry_price": position["entry_price"],
        "exit_price": round(price, 6),
        "initial_stop": position["initial_stop"],
        "final_stop": position["stop"],
        "exit_reason": reason,
        "entry_reason": position["entry_reason"],
        "cycle_id": position["cycle_id"],
        "score": position["score"],
        "holding_seconds": (at - entry_at).total_seconds(),
        "mae_points": position["mae_points"],
        "mfe_points": position["mfe_points"],
        "mae_rupees": round(position["mae_points"] * spec.lot_size * lots, 4),
        "mfe_rupees": round(position["mfe_points"] * spec.lot_size * lots, 4),
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": round(gross - costs["total"], 6),
        "entry_evidence": position.get("entry_evidence", {}),
    }
