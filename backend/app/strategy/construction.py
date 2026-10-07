"""Small, deterministic economic ordering shared by independent spread engines.

All eligibility/risk checks precede ranking. Width is a constraint, not a reward
for narrowness. Within an eligible short, retain premium first, then prefer the
farther hedge at equal protection cost. Scores are ordinal, not probabilities.
"""
from math import isfinite


def premium_retention(short_sell_price, long_buy_price):
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and isfinite(v) and v > 0 for v in (short_sell_price, long_buy_price)):
        raise ValueError("OBSERVED_POSITIVE_PREMIUMS_REQUIRED")
    return (short_sell_price - long_buy_price) / short_sell_price


def construction_key(*, short_price, short_oi, short_volume, short_spread_pct,
                     short_distance, retention, width, identity):
    # Structure/direction/survival and liquidity are mandatory upstream gates.
    # Premium is the dominant economic decision; no unavailable Greek is inferred.
    return (-short_price, -(short_oi or 0), -(short_volume or 0),
            short_spread_pct if short_spread_pct is not None else float("inf"),
            short_distance, -retention, -width, identity)


def ordinal_score(index, count):
    return 100.0 * (count-index) / count


def defined_risk_rejection(width, credit, lot_size, lots=1, *, max_loss=None,
                           max_capital=None, max_width=None):
    if not all(isfinite(v) for v in (width, credit)) or not 0 < credit < width:
        return "INVALID_DEFINED_RISK_PAYOFF"
    if max_width is not None and width > max_width:
        return "HEDGE_REJECTED_MAX_WIDTH"
    if max_loss is not None or max_capital is not None:
        if lot_size is None or lot_size <= 0 or lots <= 0:
            return "LOT_SIZE_UNAVAILABLE"
        loss = (width-credit) * lot_size * lots
        if max_loss is not None and loss > max_loss:
            return "HEDGE_REJECTED_MAX_LOSS"
        if max_capital is not None and loss > max_capital:
            return "HEDGE_REJECTED_MAX_CAPITAL"
    return None


def phase14_quote_failure(snapshot, short, long, config):
    """Use existing quote/depth policies; never upgrade unknown capacity.

    Legacy context may lack source/depth metadata and remains pending hard risk.
    Authoritative replay uses its registered (possibly stricter) quote contract.
    Confirmed but insufficient depth is always rejected, even outside replay.
    """
    from app.research.quotes import (QuotePolicy, executable_quote, information_time,
                                     policy_quote_contract, validate_book)
    policy = config.credit_spread_policy
    quotes = (policy_quote_contract(policy, max_spread_percent=config.max_bid_ask_spread_pct)
              if policy.replay_integrity_enabled else QuotePolicy(
                  source_timestamp_required=False, max_source_age_seconds=config.max_snapshot_age_seconds,
                  max_observation_age_seconds=config.max_snapshot_age_seconds,
                  max_spread_percent=config.max_bid_ask_spread_pct))
    for contract, side in ((short, "bid"), (long, "ask")):
        result = validate_book(contract, snapshot.timestamp_ist, quotes,
                               evaluated_at=information_time(snapshot))
        if not result.valid:
            return result.reason
        if policy.replay_integrity_enabled or contract.depth_unit in {"UNITS", "LOTS"}:
            result = executable_quote(snapshot, contract, side, config.requested_lots,
                                      quotes, evaluated_at=information_time(snapshot))
            if not result.valid:
                return result.reason
    return None
