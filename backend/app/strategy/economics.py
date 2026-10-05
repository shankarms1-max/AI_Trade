"""Deterministic, uncalibrated credit-spread research economics.

VIX is annualized percentage context; straddle is an observed premium proxy.
No distribution, IV, Greeks, expected return or fill probability is inferred.
"""

from datetime import datetime, time
from math import isfinite, sqrt
from zoneinfo import ZoneInfo
from app.strategy.liquidity import bid_ask_spread_pct, malformed_quotes
from app.strategy.strike_selection import structural_reference

IST = ZoneInfo("Asia/Kolkata")


def clip(value):
    return max(0.0, min(1.0, value))


def weighted(components, weights):
    return round(
        100
        * sum(components[key] * weight for key, weight in weights.items())
        / sum(weights.values()),
        4,
    )


def expiry_context(snapshot, policy):
    observed = snapshot.timestamp_ist.astimezone(IST)
    close = datetime.combine(snapshot.expiry, time(15, 30), IST)
    fractional = (close - observed).total_seconds() / 86400
    days = (snapshot.expiry - observed.date()).days
    # Calendar-day bucket boundaries are policy, independent of fractional days.
    bucket = next(
        (f"LE_{boundary:g}_DTE" for boundary in policy.dte_buckets if days <= boundary),
        f"GT_{policy.dte_buckets[-1]:g}_DTE",
    )
    session_close = datetime.combine(observed.date(), time(15, 30), IST)
    return dict(
        days_to_expiry=days,
        fractional_time_to_expiry=fractional,
        dte_bucket=bucket,
        time_remaining_in_session_minutes=max(
            0, (session_close - observed).total_seconds() / 60
        ),
    )


def expected_move(snapshot, feature, fractional_days, source="AUTO", max_quote_age=600, *, quality_policy=None):
    if quality_policy is not None:
        from app.research.moves import fixed_expected_move
        return fixed_expected_move(snapshot, feature, fractional_days, source, quality_policy)
    if fractional_days <= 0 or not isfinite(fractional_days):
        return None
    if source in {"AUTO", "ATM_STRADDLE"}:
        contracts = [
            c
            for c in snapshot.options
            if c.strike == snapshot.atm_strike and c.expiry == snapshot.expiry
        ]
        ce = [c for c in contracts if c.option_type.value == "CE"]
        pe = [c for c in contracts if c.option_type.value == "PE"]
        if len(ce) == len(pe) == 1:
            legs = (ce[0], pe[0])
            valid = all(
                c.bid is not None
                and c.ask is not None
                and not malformed_quotes(c)
                and isfinite(c.bid)
                and isfinite(c.ask)
                and c.instrument_token
                and c.exchange == "nse_fo"
                and c.source_market_timestamp is not None
                and 0
                <= (snapshot.timestamp_ist - c.source_market_timestamp).total_seconds()
                <= max_quote_age
                for c in legs
            )
            valid = (
                valid
                and abs(
                    (
                        legs[0].source_market_timestamp
                        - legs[1].source_market_timestamp
                    ).total_seconds()
                )
                <= 10
            )
            if valid:
                points = sum((c.bid + c.ask) / 2 for c in legs)
                if points > 0:
                    return dict(
                        expected_move_source="ATM_STRADDLE",
                        expected_move_points=points,
                        expected_move_percent=100 * points / snapshot.nifty_spot,
                    )
        if source == "ATM_STRADDLE":
            return None
    if source in {"AUTO", "VIX"}:
        vix = feature.volatility_features.india_vix
        if (
            feature.volatility_features.vix_available
            and vix is not None
            and isfinite(vix)
            and vix > 0
        ):
            points = snapshot.nifty_spot * vix / 100 * sqrt(fractional_days / 365)
            return dict(
                expected_move_source="VIX",
                expected_move_points=points,
                expected_move_percent=100 * points / snapshot.nifty_spot,
            )
    return None


def required_buffer(config, strength, family, volatility, fractional_days, spot):
    p = config.credit_spread_policy
    multiplier = (
        p.theta_buffer_multiplier
        if family == "THETA_CARRY_CREDIT_SPREAD"
        else (
            p.strong_buffer_multiplier
            if strength == "STRONG"
            else p.moderate_buffer_multiplier
        )
    )
    multiplier *= (
        p.high_vol_buffer_multiplier
        if volatility == "HIGH"
        else p.elevated_vol_buffer_multiplier if volatility == "ELEVATED" else 1
    )
    if fractional_days <= p.gamma_high_dte:
        multiplier *= p.low_dte_buffer_multiplier
    return (
        max(
            config.min_short_distance_points, config.min_short_distance_pct / 100 * spot
        )
        * multiplier
    )


def side_safety(feature, move, policy):
    """Structural asymmetry, with recent movement stressing the adverse side.

    A closer, strong observed OI cluster supplies more nearby structure. This is
    context, never a claim that OI is writing or that a level will hold.
    """
    points = move["expected_move_points"]
    motion = feature.price_structure_features.spot_change_from_previous_snapshot
    if motion is None:
        return dict(
            downside_safety=0.0,
            upside_safety=0.0,
            selected_bias=None,
            reason="MISSING_OBSERVED_MOVEMENT",
        )
    volatility = {"LOW": 1.0, "NORMAL": 0.9, "ELEVATED": 0.65, "HIGH": 0.4}.get(
        feature.volatility_features.vix_regime, 0
    )
    result = {}
    details = {}
    for bias, name, clusters in (
        (
            "BULLISH",
            "downside_safety",
            feature.support_resistance.potential_support_clusters,
        ),
        (
            "BEARISH",
            "upside_safety",
            feature.support_resistance.potential_resistance_clusters,
        ),
    ):
        valid = [
            c
            for c in clusters
            if (
                c.high_strike < feature.spot
                if bias == "BULLISH"
                else c.low_strike > feature.spot
            )
        ]
        if not valid:
            result[name] = 0.0
            details[name] = {"structure_available": False}
            continue
        c = min(valid, key=lambda c: abs(c.center_strike - feature.spot))
        distance = abs(c.center_strike - feature.spot)
        adverse = max(0, -motion if bias == "BULLISH" else motion)
        components = {
            "structure_proximity": 1 / (1 + distance / points),
            "local_oi_structure": clip(
                c.strength_score / 100 if c.strength_score > 1 else c.strength_score
            ),
            "observed_movement": 1 - clip(adverse / policy.movement_stress_points),
            "volatility": volatility,
        }
        result[name] = 100 * (
            0.35 * components["structure_proximity"]
            + 0.35 * components["local_oi_structure"]
            + 0.2 * components["observed_movement"]
            + 0.1 * volatility
        )
        details[name] = components
    down, up = result["downside_safety"], result["upside_safety"]
    selected = (
        ("BULLISH" if down > up else "BEARISH")
        if max(down, up) >= policy.side_safety_min_score
        and abs(down - up) >= policy.side_safety_min_margin
        and down != up
        else None
    )
    return dict(
        **result,
        selected_bias=selected,
        components=details,
        reason="ASYMMETRIC_STRUCTURE" if selected else "NO_CLEARLY_SAFER_SIDE",
    )


def cost_estimate(short_price, long_price, lot_size, policy):
    """Round-trip estimate; exit turnover proxy equals entry turnover.

    Taxes use sell/buy turnover on both legs, not gross turnover for STT/stamp.
    Rates are explicit research inputs, not current statutory or broker rates.
    """
    turnover = 2 * (short_price + long_price)
    brokerage = (
        4 * policy.brokerage_per_order / lot_size if lot_size and lot_size > 0 else 0
    )
    exchange = turnover * policy.exchange_rate
    stt = (short_price + long_price) * policy.stt_rate
    stamp = (short_price + long_price) * policy.stamp_rate
    gst = (brokerage + exchange) * policy.gst_rate
    parts = dict(
        brokerage=brokerage,
        exchange_charges=exchange,
        stt=stt,
        gst=gst,
        stamp_duty=stamp,
        modeled_slippage=4 * policy.slippage_points_per_leg,
        exit_turnover_assumption="EQUAL_TO_ENTRY",
        unit="POINTS_PER_UNIT",
    )
    return sum(v for v in parts.values() if isinstance(v, (int, float))), parts


def candidate_economics(
    snapshot,
    feature,
    regime,
    short,
    long,
    price,
    payoff,
    reference,
    family,
    side,
    config,
    expiry,
    move,
    safety,
    buffer,
):
    p = config.credit_spread_policy
    distance = abs(short.strike - snapshot.nifty_spot)
    units = distance / move["expected_move_points"]
    beyond = (
        (reference - short.strike if side == "BULLISH" else short.strike - reference)
        if reference is not None
        else 0
    )
    opposing = structural_reference(
        feature, "BEARISH" if side == "BULLISH" else "BULLISH"
    )
    adverse_motion = max(
        0,
        (
            -(feature.price_structure_features.spot_change_from_previous_snapshot or 0)
            if side == "BULLISH"
            else feature.price_structure_features.spot_change_from_previous_snapshot
            or 0
        ),
    )
    observed_range = (
        feature.price_structure_features.collector_observed_high
        - feature.price_structure_features.collector_observed_low
    )
    vix_quality = {"LOW": 1, "NORMAL": 0.9, "ELEVATED": 0.65, "HIGH": 0.4}.get(
        feature.volatility_features.vix_regime, 0
    )
    spreads = [bid_ask_spread_pct(c) for c in (short, long)]
    quote_liquidity = 1 - max(
        (v if v is not None else config.max_bid_ask_spread_pct) for v in spreads
    ) / max(config.max_bid_ask_spread_pct, 1)
    oi_liquidity = clip(
        min(short.open_interest or 0, long.open_interest or 0)
        / max(config.min_short_open_interest, config.min_long_open_interest, 1)
        / 10
    )
    liquidity = clip(0.5 * oi_liquidity + 0.5 * clip(quote_liquidity))
    clusters = (
        feature.support_resistance.potential_support_clusters
        if side == "BULLISH"
        else feature.support_resistance.potential_resistance_clusters
    )
    oi_structure = max(
        (
            clip(c.strength_score / 100 if c.strength_score > 1 else c.strength_score)
            for c in clusters
            if (
                c.low_strike == reference
                if side == "BULLISH"
                else c.high_strike == reference
            )
        ),
        default=0,
    )
    survival = dict(
        opposing_structure=(
            0
            if opposing is None
            else clip(
                abs(short.strike - opposing) / max(move["expected_move_points"] * 3, 1)
            )
        ),
        distance=clip(distance / max(buffer * 3, 1)),
        structure=clip(beyond / max(buffer, 1)),
        expected_move=clip(units / 2),
        volatility=clip(
            vix_quality
            * (1 - 0.25 * clip(observed_range / max(move["expected_move_points"], 1)))
            * (1 - 0.25 * clip(adverse_motion / p.movement_stress_points))
        ),
        oi_structure=oi_structure,
        liquidity=liquidity,
        dte=clip(expiry["fractional_time_to_expiry"] / p.gamma_moderate_dte),
    )
    survival_score = weighted(survival, p.survival_weights)
    cost, cost_parts = cost_estimate(
        price.short_price, price.long_price, snapshot.lot_size, p
    )
    integrity_complete = None
    if p.replay_integrity_enabled:
        from app.research.costs import CostSchedule, estimated_cost
        cost_parts = estimated_cost(p.cost_schedule or CostSchedule(), snapshot, price.short_price, price.long_price, lots=p.requested_lots)
        integrity_complete = cost_parts["cost_completeness"] == "NET_COMPLETE"
        cost = ((cost_parts["total_cost_rupees"] / (snapshot.lot_size*p.requested_lots))
                if integrity_complete else 0)
    after = price.net_credit - cost
    loss_ratio = after / payoff.max_loss
    width_ratio = after / payoff.width
    per_day = after / expiry["fractional_time_to_expiry"]
    retention = after / price.short_price
    carry = dict(
        credit_width=clip(width_ratio / p.carry_credit_width_target),
        credit_loss=clip(loss_ratio / p.carry_credit_loss_target),
        credit_dte=clip(per_day / p.carry_credit_per_day_target),
        premium_retention=clip(retention / p.carry_premium_retention_target),
        survival_distance=clip(units / 2),
    )
    carry_score = weighted(carry, p.carry_weights)
    dte = expiry["fractional_time_to_expiry"]
    gamma = (
        "EXTREME"
        if dte <= p.gamma_extreme_dte and units < p.gamma_extreme_distance_units
        else (
            "HIGH"
            if dte <= p.gamma_high_dte
            or units < p.gamma_high_distance_units
            or adverse_motion >= p.movement_stress_points
            else (
                "MODERATE"
                if dte <= p.gamma_moderate_dte
                or feature.volatility_features.vix_regime in {"ELEVATED", "HIGH"}
                else "LOW"
            )
        )
    )
    gamma_level = {"LOW": 0, "MODERATE": 0.35, "HIGH": 0.7, "EXTREME": 1}[gamma]
    gamma_level = min(
        1, gamma_level + 0.2 * clip(1 - dte / max(p.gamma_high_dte, 0.001))
    )
    balance = (
        "EXTREME_RISK"
        if gamma == "EXTREME"
        else (
            "UNFAVORABLE"
            if gamma == "HIGH"
            else (
                "FAVORABLE"
                if carry_score >= p.theta_min_carry
                and survival_score >= p.theta_min_survival
                else "BALANCED"
            )
        )
    )
    direction = (
        (regime.directional_score or 0) / 100 if side == regime.market_bias.value else 0
    )
    components = dict(
        survival=survival_score / 100,
        carry=carry_score / 100,
        directional=direction,
        structure=survival["structure"],
        liquidity=liquidity,
        credit=carry["credit_width"],
    )
    penalties = dict(
        gamma_risk=p.gamma_penalty * gamma_level,
        volatility_stress=p.volatility_penalty * (1 - vix_quality),
        max_loss_burden=p.max_loss_penalty
        * clip(payoff.max_loss * (snapshot.lot_size or 1) / p.max_loss_scale_per_lot),
        cost_burden=p.cost_penalty * clip(cost / price.net_credit),
        expected_move_distance=p.expected_move_penalty * (1 - clip(units)),
    )
    selection = max(
        0, weighted(components, p.ranking_weights) - sum(penalties.values())
    )
    return dict(
        strategy_logic_version="phase14_2_v1",
        policy_hash=p.policy_hash,
        research_run_id=p.research_run_id,
        execution_mode=p.execution_mode,
        economics_basis=("NET_ESTIMATED" if integrity_complete else "GROSS_ONLY") if p.replay_integrity_enabled else None,
        strategy_family=family,
        market_bias=regime.market_bias.value,
        directional_strength=regime.directional_strength.value,
        directional_score=regime.directional_score,
        survival_score=survival_score,
        carry_score=carry_score,
        survival_components=survival,
        carry_components=carry,
        ranking_components=components,
        ranking_penalties=penalties,
        selection_score=round(selection, 4),
        **move,
        **expiry,
        short_strike_distance_points=distance,
        short_strike_distance_percent=distance / snapshot.nifty_spot * 100,
        short_strike_distance_expected_move_units=units,
        distance_in_expected_move_units=units,
        distance_beyond_structure_points=beyond,
        gap_to_opposing_structure_points=(
            None if opposing is None else abs(short.strike - opposing)
        ),
        required_short_strike_buffer=buffer,
        gamma_risk_state=gamma,
        theta_gamma_balance_state=balance,
        risk_state="PENDING_HARD_RISK",
        spread_width_points=payoff.width,
        net_credit_per_unit=price.net_credit,
        max_loss_per_unit=payoff.max_loss,
        gross_credit=price.net_credit,
        estimated_cost=None if p.replay_integrity_enabled and not integrity_complete else cost,
        net_credit_after_cost=None if p.replay_integrity_enabled and not integrity_complete else after,
        cost_components=cost_parts,
        cost_estimate_complete=integrity_complete if p.replay_integrity_enabled else p.costs_complete and snapshot.lot_size is not None,
        credit_to_max_loss=price.net_credit / payoff.max_loss,
        credit_to_expected_move=price.net_credit / move["expected_move_points"],
        credit_per_dte=price.net_credit / expiry["fractional_time_to_expiry"],
        premium_retention_ratio=retention,
        carry_model="TRANSPARENT_CARRY_PROXY_NO_GREEKS",
        side_safety=safety,
    )
