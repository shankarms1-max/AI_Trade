"""Frozen 15-minute ORB + latest completed-bar AST/ADX/ER decisions."""

from __future__ import annotations

from datetime import datetime
from math import isfinite

from app.adaptive_supertrend.bars import opening_range
from app.adaptive_supertrend.config import AstPolicy
from app.adaptive_supertrend.indicators import adx, efficiency_ratio, supertrend
from app.parallel_paper.bars import Bar
from app.research.manifest import digest
from app.scalper.repository import IST


def decide(
    bars: list[Bar],
    at: datetime,
    price: float | None,
    parameters: dict | None,
    state: dict,
    policy: AstPolicy,
    *,
    future_identity: tuple | None = None,
) -> dict:
    if at.tzinfo is None:
        raise ValueError("AST_AWARE_CLOCK_REQUIRED")
    at = at.astimezone(IST)
    if any(bar.end > at for bar in bars):
        raise ValueError("AST_FUTURE_BAR")
    day = at.date().isoformat()
    if state.get("date") != day:
        state.clear()
        state.update(date=day, consumed=[])
    features: dict = {"current_future_price": price, "adx": None, "efficiency_ratio": None}
    blockers = []
    if future_identity is not None:
        identity = [str(value) for value in future_identity]
        if "future_identity" not in state:
            state["future_identity"] = identity
        if state["future_identity"] != identity or not all(value not in ("None", "") for value in identity):
            blockers.append("AST_SESSION_FUTURES_IDENTITY_CHANGED_OR_MISSING")
    if policy.missing:
        blockers.append("AST_POLICY_UNCONFIGURED")
        features["missing_policy"] = policy.missing
    if parameters is None:
        blockers.append("AST_PARAMETERS_UNAVAILABLE")
    elif parameters["trading_date"] != day or parameters["policy_hash"] != policy.policy_hash:
        blockers.append("AST_FROZEN_PARAMETER_POLICY_MISMATCH")
    if "opening_range" not in state:
        candidate = opening_range(bars, at)
        if candidate["complete"]:
            state["opening_range"] = candidate
    orb = state.get("opening_range", opening_range(bars, at))
    features.update(orb)
    if not orb["complete"]:
        blockers.append("AST_OPENING_RANGE_UNAVAILABLE")
    usable: list[Bar] = []
    for bar in bars:
        if not bar.valid:
            usable = []
        else:
            usable.append(bar)
    ast_direction = None
    transition = None
    if parameters is not None:
        sequence = supertrend(usable, parameters["atr_period"], parameters["multiplier_tenths"])
        latest = sequence[-1] if sequence else {"direction": None, "value": None, "atr": None, "transition": None}
        features.update(
            ast_direction=latest["direction"],
            supertrend=latest["value"],
            atr=latest["atr"],
            ast_transition=latest["transition"],
            atr_period=parameters["atr_period"],
            multiplier_tenths=parameters["multiplier_tenths"],
            completed_bar_at=usable[-1].end.isoformat() if usable else None,
        )
        ast_direction, transition = latest["direction"], latest["transition"]
        if ast_direction is None:
            blockers.append("AST_WARMUP_REQUIRED")
    # A gap at the right edge cannot expose an older indicator as current.
    if not bars or not bars[-1].valid or (at - bars[-1].end).total_seconds() >= 300:
        blockers.append("AST_CURRENT_COMPLETED_BAR_UNAVAILABLE")
    if policy.regime is not None and policy.regime.mode == "ADX_ER":
        adxs = adx(usable, policy.regime.adx_period)
        features["adx"] = adxs[-1] if adxs else None
        features["efficiency_ratio"] = efficiency_ratio(usable, policy.regime.er_lookback)
        if features["adx"] is None:
            blockers.append("AST_ADX_WARMUP_REQUIRED")
        elif features["adx"] < policy.regime.adx_threshold:
            blockers.append("AST_ADX_THRESHOLD_NOT_MET")
        if features["efficiency_ratio"] is None:
            blockers.append("AST_ER_WARMUP_REQUIRED")
        elif features["efficiency_ratio"] < policy.regime.er_threshold:
            blockers.append("AST_ER_THRESHOLD_NOT_MET")
    direction = None
    if price is None or not isfinite(price) or price <= 0:
        blockers.append("AST_FUTURE_PRICE_UNAVAILABLE")
    elif orb["complete"]:
        direction = "BULL" if price > orb["opening_high"] else "BEAR" if price < orb["opening_low"] else None
        if direction is None:
            blockers.append("ORB_NOT_BROKEN")
        elif ast_direction is not None and ast_direction != direction:
            blockers.append("AST_ORB_DIRECTION_CONFLICT")
    opportunity = None
    if direction is not None and parameters is not None and orb["complete"]:
        opportunity = digest(
            {
                "date": day,
                "direction": direction,
                "range": orb,
                "parameters": parameters,
                "transition": transition,
                "future_identity": future_identity,
            }
        )
        if opportunity in state.get("consumed", []):
            blockers.append("AST_OPPORTUNITY_CONSUMED")
    features.update(breakout_direction=direction, opportunity_id=opportunity)
    return {
        "strategy_id": "AST_V1",
        "strategy_version": "AST_V1",
        "timestamp": at.isoformat(),
        "direction": direction,
        "status": "ENTER" if not blockers else "NO_SETUP",
        "features": features,
        "blockers": list(dict.fromkeys(blockers)),
        "opportunity_id": opportunity,
    }
