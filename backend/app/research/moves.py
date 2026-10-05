"""Fixed, named move context per replay run; never silent AUTO fallback."""
from math import sqrt
from app.research.quotes import ContractIdentity, aware, exact_contract, finite, information_time, policy_quote_contract, validate_book

MOVE_SOURCES = {"VIX_SCALED_EXPIRY_MOVE", "ATM_STRADDLE_PREMIUM"}


def fixed_expected_move(snapshot, feature, fractional_days, source, policy):
    if source not in MOVE_SOURCES:
        raise ValueError("integrity replay requires a fixed named expected-move source; AUTO is inactive")
    if not aware(snapshot.timestamp_ist) or not finite(fractional_days) or fractional_days <= 0:
        return None
    if not finite(snapshot.nifty_spot) or snapshot.nifty_spot <= 0:
        return None
    if source == "VIX_SCALED_EXPIRY_MOVE":
        vix = snapshot.india_vix
        if (not finite(vix) or vix <= 0 or not feature.volatility_features.vix_available
                or feature.volatility_features.india_vix != vix):
            return None
        points = snapshot.nifty_spot * vix / 100 * sqrt(fractional_days / 365)
    else:
        eligible = [row for row in snapshot.options if row.expiry == snapshot.expiry
                    and ContractIdentity.of(row).valid() and row.exchange == "nse_fo"]
        strikes = {row.strike for row in eligible}
        if not strikes or not finite(snapshot.atm_strike):
            return None
        distance = abs(snapshot.atm_strike - snapshot.nifty_spot)
        if (distance > policy.atm_tolerance_points or
                distance > min(abs(strike - snapshot.nifty_spot) for strike in strikes) + 1e-6):
            return None
        selected = [row for row in eligible if row.strike == snapshot.atm_strike]
        legs = []
        qp = policy_quote_contract(policy, atm=True)
        for side in ("CE", "PE"):
            matching = [row for row in selected if row.option_type.value == side]
            if len(matching) != 1:
                return None
            row, _ = exact_contract(snapshot, ContractIdentity.of(matching[0]))
            if row is None or not validate_book(row, snapshot.timestamp_ist, qp, evaluated_at=information_time(snapshot)).valid:
                return None
            legs.append(row)
        if abs((legs[0].source_market_timestamp-legs[1].source_market_timestamp).total_seconds()) > qp.max_leg_skew_seconds:
            return None
        points = sum((row.bid + row.ask) / 2 for row in legs)
    return {"expected_move_source": source, "expected_move_points": points,
            "expected_move_percent": 100 * points / snapshot.nifty_spot}
