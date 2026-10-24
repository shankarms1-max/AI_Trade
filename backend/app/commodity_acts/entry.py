"""Commodity direction and single-short eligibility, never spread compatibility.

Context components use the unchanged ACTS functions and configured percentage bands.
Execution earns no direction points. Native eligibility is a conjunction of dated
book/risk/session rules, not an invented MCX score or a bounded-loss assertion.
"""

from typing import Literal

from app.research.manifest import digest

from pydantic import BaseModel, ConfigDict, Field

from app.acts_paper import acts_v2

VERSION: Literal["COMMODITY_SINGLE_SHORT_ENTRY_V1"] = "COMMODITY_SINGLE_SHORT_ENTRY_V1"
DIRECTION_VERSION = "COMMODITY_ACTS_DIRECTIONAL_CONTEXT_V1"
EXECUTION_KEYS = frozenset({"execution_spread", "execution_depth", "execution_credit"})


class EntryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal["COMMODITY_SINGLE_SHORT_ENTRY_V1"] = VERSION
    min_open_interest: float | None = Field(default=None, gt=0)
    min_volume: float | None = Field(default=None, gt=0)
    min_otm_observed_steps: float | None = Field(default=None, gt=0)
    min_premium_to_stress_loss: float | None = Field(default=None, gt=0)


def directional_parameters(config):
    payload = config.payload()
    payload["weights"] = {key: value for key, value in config.weights.items() if key not in EXECUTION_KEYS}
    payload.pop("max_bid_ask_spread_pct")
    payload.pop("min_credit_to_width")
    return {"version": DIRECTION_VERSION, "score_basis": "CONTEXT_ONLY_NORMALIZED_BY_AVAILABLE_WEIGHT", **payload}


def direction(ctx, watch, config):
    # No candidate is supplied: the shared scorer can only consume context. Its
    # missing-spread blocker belongs to the old executor, not this intelligence.
    result = acts_v2.decide(ctx, watch, None, {}, [], config)
    hard = [item for item in result["hard_blockers"] if item != "NO_EXECUTABLE_CANDIDATE"]
    raw_score = result.pop("opportunity_score")
    maximum = sum(value for key, value in config.weights.items() if key not in EXECUTION_KEYS)
    if maximum <= 0:
        raise ValueError("COMMODITY_DIRECTIONAL_WEIGHTS_REQUIRED")
    score = round(100 * raw_score / maximum, 4)
    components = result.pop("score_components")
    components.pop("execution")
    qualified = not hard and result["direction"] is not None and not result["market_state"].endswith("_PULLBACK")
    status = (
        "QUALIFIED"
        if qualified and score >= config.tradeable_score
        else (
            "WATCH"
            if not hard
            and result["direction"] is not None
            and (result["market_state"].endswith("_PULLBACK") or score >= config.watch_score)
            else "NO_SETUP"
        )
    )
    result.update(
        strategy_id=DIRECTION_VERSION,
        status=status,
        directional_score=score,
        directional_score_max=100,
        raw_context_points=raw_score,
        raw_context_max=maximum,
        score_band=(
            "VERY_STRONG"
            if score >= config.very_strong_score
            else (
                "STRONG"
                if score >= config.strong_score
                else (
                    "TRADEABLE"
                    if score >= config.tradeable_score
                    else "WATCH" if score >= config.watch_score else "NO_TRADE"
                )
            )
        ),
        directional_components=components,
        directional_parameters=directional_parameters(config),
        hard_blockers=hard,
        blockers=hard,
        thesis=None,
    )
    return result


def native_evidence(snapshot, quote, policy, known_at, scenario, step, entry_policy):
    """All numeric values are observed or explicitly labelled operator scenarios."""
    distance = (
        snapshot.future_price - quote.strike
        if quote.option_type.value == "PE"
        else quote.strike - snapshot.future_price
    )
    ratio = (
        quote.bid * quote.lot_size / scenario["stress_loss_estimate"] if scenario["stress_loss_estimate"] > 0 else None
    )
    values = {
        "open_interest": quote.open_interest,
        "volume": quote.volume,
        "otm_observed_steps": distance / step if step is not None and step > 0 else None,
        "premium_to_stress_loss": ratio,
    }
    checks = {}
    reasons = []
    for key, value in values.items():
        threshold = getattr(entry_policy, "min_" + key)
        status = (
            "DIAGNOSTIC_ONLY" if threshold is None else "PASS" if value is not None and value >= threshold else "FAIL"
        )
        checks[key] = {"value": value, "minimum": threshold, "status": status}
        if status == "FAIL":
            reasons.append("SINGLE_SHORT_" + key.upper() + "_BELOW_CONFIGURED_MINIMUM")
    return {
        "version": VERSION,
        "policy": entry_policy.model_dump(mode="json"),
        "checks": checks,
        "reasons": reasons,
        "evidence": {
            "instrument_id": snapshot.instrument_id,
            "future_token": snapshot.future_token,
            "future_instrument_id": snapshot.future_instrument_id,
            "future_expiry": snapshot.future_expiry.isoformat(),
            "option_token": quote.instrument_token,
            "option_type": quote.option_type.value,
            "option_expiry": quote.expiry.isoformat(),
            "strike": quote.strike,
            "future_price": snapshot.future_price,
            "otm_distance_points": distance,
            "observed_strike_step": step,
            "bid": quote.bid,
            "ask": quote.ask,
            "bid_quantity_units": quote.bid_quantity,
            "ask_quantity_units": quote.ask_quantity,
            "required_units": policy.evidence.lot_size,
            "depth_unit": policy.evidence.depth_unit,
            "source_market_timestamp": quote.source_market_timestamp.isoformat(),
            "source_age_seconds": (known_at - quote.source_market_timestamp).total_seconds(),
            "quote_age_limit_seconds": policy.max_quote_age_seconds,
            "open_interest": quote.open_interest,
            "volume": quote.volume,
            "bid_ask_spread_pct": 100 * (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2),
            "max_entry_spread_pct": policy.max_entry_spread_pct,
            "premium_points": quote.bid,
            "premium_rupees": quote.bid * quote.lot_size,
            "stress": scenario,
            "premium_to_stress_loss_estimate": ratio,
            "stress_limit_rupees": policy.max_stress_loss_per_trade,
            "dte_calendar_days": (quote.expiry - snapshot.timestamp_ist.date()).days,
            "session_id": snapshot.session_id,
            "observed_at": snapshot.timestamp_ist.isoformat(),
            "evaluated_at": known_at.isoformat(),
        },
    }


def decide(ctx, watch, candidate, rejected, blockers, config):
    result = direction(ctx, watch, config)
    context_status = result["status"]
    hard = [*result["hard_blockers"], *blockers]
    if candidate is not None and candidate.direction != result["direction"]:
        hard.append("SINGLE_SHORT_DIRECTION_MISMATCH")
    if result["direction"] and candidate is None:
        hard.append("NO_EXECUTABLE_SINGLE_SHORT")
    qualification = (
        candidate.qualification
        if candidate is not None
        else {
            "version": VERSION,
            "status": "REJECTED" if rejected else "UNAVAILABLE",
            "reasons": list(rejected),
        }
    )
    if candidate is not None and qualification.get("status") != "ELIGIBLE":
        hard.append("SINGLE_SHORT_NOT_QUALIFIED")
    qualification = {
        **qualification,
        "candidate_status": qualification.get("status"),
        "entry_blockers": list(dict.fromkeys(hard)),
        "status": "BLOCKED" if hard else "ELIGIBLE" if context_status == "QUALIFIED" else "WAITING_FOR_DIRECTION",
    }
    status = "NO_SETUP" if hard else "ENTER" if context_status == "QUALIFIED" else context_status
    trigger = result["trigger"]
    if status == "ENTER":
        trigger = trigger or (
            "FRESH_OPENING_BREAKOUT"
            if result["score_evidence"].get("opening_breakout")
            else "DIRECTIONAL_CONTINUATION"
        )
        watch.clear()
    result.update(
        strategy_id=VERSION,
        status=status,
        trigger=trigger,
        directional_status=context_status,
        entry_qualification=qualification,
        entry_policy_hash=digest(
            {
                "version": VERSION,
                "direction": directional_parameters(config),
                "native": candidate.qualification.get("policy") if candidate else None,
            }
        ),
        candidate_rejections=dict(rejected),
        candidate_evaluations=sorted(
            getattr(rejected, "evaluations", []),
            key=lambda row: (row["option_token"], row["status"], row.get("reason", "")),
        ),
        hard_blockers=list(dict.fromkeys(hard)),
        blockers=list(dict.fromkeys(hard)),
        watch_state=dict(watch),
        entry_reason=trigger if status == "ENTER" else None,
        thesis=(
            {
                "version": VERSION,
                "direction": result["direction"],
                "market_state": result["market_state"],
                "directional_score": result["directional_score"],
                "directional_parameters": result["directional_parameters"],
                "alpha": result["alpha"],
                "positioning": result["positioning"],
                "entry_qualification": qualification,
                "entry_reason": trigger,
                "opening_high": ctx["opening_high"],
                "opening_low": ctx["opening_low"],
            }
            if status == "ENTER"
            else None
        ),
    )
    return result
