"""Read-only ACTS paper diagnostics; never used to qualify or manage trades."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.acts_paper.alpha import ACTS_ALPHA_SOURCE, ALPHA_CAPTURE_MINUTES
from app.alpha.engine import AlphaConfig
from app.alpha.models import (ALPHA_VERSION, AlphaFeatureSnapshot, AlphaValidity,
                              AlphaStatus, CalculationMode)
from app.db.models import AlphaFeatureSnapshotRecord, MarketSnapshotRecord
from app.scalper_lab import acts
from app.scalper_lab.context import ResearchObservation


def alpha_readiness(observation: ResearchObservation,
                    sessions: sessionmaker[Session], config: AlphaConfig
                    ) -> dict[str, Any]:
    """Explain the last local original Alpha independently of ACTS blockers."""
    snapshot = observation.snapshot
    at = snapshot.captured_at
    with sessions() as session:
        payload = session.scalar(select(AlphaFeatureSnapshotRecord.result_json).join(
            MarketSnapshotRecord,
            AlphaFeatureSnapshotRecord.market_snapshot_id == MarketSnapshotRecord.id
        ).where(MarketSnapshotRecord.source == ACTS_ALPHA_SOURCE,
                AlphaFeatureSnapshotRecord.alpha_version == ALPHA_VERSION,
                AlphaFeatureSnapshotRecord.calculation_mode ==
                CalculationMode.LIVE_ORIGINAL.value,
                AlphaFeatureSnapshotRecord.timestamp <= at).order_by(
                    AlphaFeatureSnapshotRecord.timestamp.desc(),
                    AlphaFeatureSnapshotRecord.id.desc()).limit(1))
    latest = None
    if payload is not None:
        try:
            latest = AlphaFeatureSnapshot.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            pass
    selected = observation.alpha
    source = selected or latest
    if selected is not None:
        state = "ALPHA_AVAILABLE"
    elif payload is not None and latest is None:
        state = "ALPHA_INVALID"
    elif latest is None:
        state = "ALPHA_WARMING_UP"
    elif (at - latest.timestamp).total_seconds() > int(acts.PARAMETERS["alpha_max_age_seconds"]):
        state = "ALPHA_STALE"
    elif (latest.reference_instrument_id != snapshot.future_instrument_id
          or latest.reference_expiry != snapshot.future_expiry
          or latest.expiry != snapshot.expiry):
        state = "ALPHA_IDENTITY_MISMATCH"
    elif latest.feature_calculated_at is not None and latest.feature_calculated_at > at:
        state = "ALPHA_FUTURE_REJECTED"
    elif latest.status == AlphaStatus.WARMING_UP:
        state = "ALPHA_WARMING_UP"
    elif latest.validity_state == AlphaValidity.INVALID:
        state = "ALPHA_INVALID"
    elif latest.validity_state == AlphaValidity.PARTIAL:
        state = ("ALPHA_WARMING_UP" if "INSUFFICIENT_ALPHA_HISTORY" in latest.warnings
                 else "ALPHA_INVALID")
    else:
        state = "ALPHA_UNAVAILABLE"
    return {
        "alpha_status": state,
        "alpha_engine_status": source.status.value if source else None,
        "alpha_validity_state": source.validity_state.value if source else None,
        "alpha_timestamp": source.timestamp.isoformat() if source else None,
        "alpha_1": source.alpha_1 if source else None,
        "alpha_2": source.alpha_2 if source else None,
        "alpha_1_direction": source.alpha_1_direction.value if source else None,
        "alpha_2_direction": source.alpha_2_direction.value if source else None,
        "joint_alpha_direction": source.joint_alpha_direction.value if source else None,
        "rank_observations_alpha1": source.rank_observations_alpha1 if source else 0,
        "rank_observations_alpha2": source.rank_observations_alpha2 if source else 0,
        "volume_observations": source.volume_baseline_observations if source else 0,
        "volatility_observations": source.volatility_return_observations if source else 0,
        "alpha1_required_minutes": config.alpha1_lookback_minutes,
        "alpha2_required_minutes": config.alpha2_lookback_minutes,
        # The first sampled point inside an 800/300-minute window can be up to
        # one 3-minute bucket after its boundary. This is display-only.
        "alpha1_ready": bool(source and source.alpha_1 is not None
                             and source.effective_history_minutes >=
                             config.alpha1_lookback_minutes - ALPHA_CAPTURE_MINUTES),
        "alpha2_ready": bool(source and source.alpha_2 is not None
                             and source.effective_history_minutes >=
                             config.alpha2_lookback_minutes - ALPHA_CAPTURE_MINUTES),
        "warnings": source.warnings if source else [],
    }


def regime_readiness(ctx: dict[str, Any]) -> dict[str, Any]:
    """Mirror the frozen ACTS regime gates solely for auditable status output."""
    returns = ctx["futures_returns_bps"]
    structure = ctx["structure"]
    high, low = ctx["opening_high"], ctx["opening_low"]
    midpoint = (high + low) / 2 if high is not None and low is not None else None
    vwap = ctx["vwap"]
    outcomes = {}
    for name, sign, boundary in (("BULL", 1, acts.PARAMETERS["bull_range_position"]),
                                 ("BEAR", -1, acts.PARAMETERS["bear_range_position"])):
        three, five, fifteen = (returns.get(key) for key in ("3m", "5m", "15m"))
        three_pass = three is not None and sign * three >= 2
        five_pass = five is not None and sign * five >= acts.PARAMETERS["futures_5m_bps"]
        fifteen_pass = (fifteen is not None and sign * fifteen >=
                        acts.PARAMETERS["futures_15m_bps"])
        position = structure["range_position"]
        range_pass = (position is not None and
                      (position >= boundary if sign > 0 else position <= boundary))
        opening_pass = (structure["opening_range_complete"] and midpoint is not None
                        and sign * (ctx["spot"] - midpoint) > 0)
        vwap_applied = vwap["status"] == "AVAILABLE"
        vwap_pass = (not vwap_applied or
                     (ctx["future"] is not None and vwap["value"] is not None
                      and vwap["slope_bps_per_minute"] is not None
                      and sign * (ctx["future"] - vwap["value"]) > 0
                      and sign * vwap["slope_bps_per_minute"] >= 0))
        failures = []
        if not structure["opening_range_complete"]:
            failures.append("OPENING_RANGE_INCOMPLETE")
        if not five_pass:
            failures.append("FUTURES_5M_THRESHOLD_NOT_MET")
        if not fifteen_pass:
            failures.append("FUTURES_15M_THRESHOLD_NOT_MET")
        if not range_pass:
            failures.append("RANGE_POSITION_NOT_MET")
        if not opening_pass and structure["opening_range_complete"]:
            failures.append("OPENING_RANGE_ALIGNMENT_NOT_MET")
        if not vwap_pass:
            failures.append("VWAP_ALIGNMENT_NOT_MET")
        outcomes[name] = {
            "context_3m_trend_pass": three_pass,
            "context_3m_failure": None if three_pass else "FUTURES_3M_THRESHOLD_NOT_MET",
            "futures_5m_pass": five_pass, "futures_15m_pass": fifteen_pass,
            "trend_pass": five_pass and fifteen_pass,
            "structure_pass": range_pass,
            "opening_range_pass": opening_pass,
            "vwap_alignment_pass": vwap_pass,
            "vwap_gate_applied": vwap_applied,
            "vwap_note": None if vwap_applied else "VWAP_UNAVAILABLE",
            "final_pass": (five_pass and fifteen_pass and range_pass
                           and opening_pass and vwap_pass),
            "failures": failures,
        }
    direction = ctx["acts"]["direction"]
    closest = max(outcomes, key=lambda name: sum((
        outcomes[name]["futures_5m_pass"], outcomes[name]["futures_15m_pass"],
        outcomes[name]["structure_pass"], outcomes[name]["opening_range_pass"])))
    failures = outcomes[closest]["failures"]
    return {
        "spot": ctx["spot"], "future": ctx["future"],
        "futures_returns_bps": {key: returns.get(key) for key in ("3m", "5m", "15m")},
        "range_position": structure["range_position"],
        "session_open": structure["session_open"],
        "session_high": structure["session_high"],
        "session_low": structure["session_low"],
        "session_move_bps": structure["session_move_bps"],
        "session_range_bps": structure["session_range_bps"],
        "opening_range_complete": structure["opening_range_complete"],
        "opening_high_distance_bps": structure["opening_high_distance_bps"],
        "opening_low_distance_bps": structure["opening_low_distance_bps"],
        "opening_midpoint": midpoint,
        "vwap": {key: vwap.get(key) for key in (
            "status", "value", "slope_bps_per_minute", "cumulative_volume")},
        "direction_tests": outcomes,
        "direction": direction,
        "regime": direction if direction is not None else "RANGE",
        "primary_failure_reason": None if direction is not None else (
            failures[0] if failures else "NO_DIRECTIONAL_REGIME"),
    }
