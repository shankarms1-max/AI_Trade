"""Observed contract geometry and quote coverage, never a spread policy."""
from collections import Counter, defaultdict
from statistics import median

from app.commodity_acts.domain import CommoditySnapshot


def book_usable(quote, at, max_age: int) -> bool:
    return (quote.source_market_timestamp is not None
            and 0 <= (at - quote.source_market_timestamp).total_seconds() <= max_age
            and quote.bid is not None and quote.ask is not None and 0 < quote.bid <= quote.ask)


def geometry(snapshot: CommoditySnapshot, max_age: int = 30) -> dict:
    quotes = snapshot.options
    strikes = sorted({q.strike for q in quotes})
    gaps = [round(right - left, 8) for left, right in zip(strikes, strikes[1:])]
    counts = Counter(gaps)
    modes = [gap for gap, count in counts.items() if count == max(counts.values())] if counts else []
    modal = modes[0] if len(modes) == 1 else None
    pairs = {strike: {q.option_type.value for q in quotes if q.strike == strike} for strike in strikes}
    good = [q for q in quotes if book_usable(q, snapshot.timestamp_ist, max_age)]
    depth = [q for q in good if q.bid_quantity is not None and q.bid_quantity > 0
             and q.ask_quantity is not None and q.ask_quantity > 0]
    spreads = [100 * (q.ask - q.bid) / ((q.ask + q.bid) / 2) for q in good]
    credits = defaultdict(list)
    indices = {strike: index for index, strike in enumerate(strikes)}
    for short in good:
        for hedge in good:
            if short.option_type != hedge.option_type:
                continue
            outward = hedge.strike < short.strike if short.option_type.value == "PE" else hedge.strike > short.strike
            if not outward:
                continue
            steps = abs(indices[short.strike] - indices[hedge.strike])
            credits[steps].append({"short_token": short.instrument_token, "hedge_token": hedge.instrument_token,
                "option_type": short.option_type.value, "short_strike": short.strike, "hedge_strike": hedge.strike,
                "observed_width": abs(short.strike - hedge.strike), "short_bid": short.bid, "hedge_ask": hedge.ask,
                "observed_credit": round(short.bid - hedge.ask, 8)})
    return {"strikes": strikes, "adjacent_gaps": gaps, "modal_strike_step": modal,
            "atm_pair_complete": pairs.get(snapshot.atm_strike) == {"CE", "PE"},
            "complete_pairs": sum(sides == {"CE", "PE"} for sides in pairs.values()),
            "contracts": len(quotes), "bid_ask_count": len(good), "depth_count": len(depth),
            "bid_ask_coverage": len(good) / len(quotes), "depth_coverage": len(depth) / len(quotes),
            "oi_coverage": sum(q.open_interest is not None for q in quotes) / len(quotes),
            "volume_coverage": sum(q.volume is not None for q in quotes) / len(quotes),
            "source_timestamp_coverage": sum(q.source_market_timestamp is not None for q in quotes) / len(quotes),
            "median_bid_ask_spread_pct": median(spreads) if spreads else None,
            "max_bid_ask_spread_pct": max(spreads) if spreads else None,
            "depth_unit": "UNKNOWN", "spread_execution_enabled": False,
            "credits_basis": "OBSERVED_SHORT_BID_MINUS_HEDGE_ASK_NOT_ELIGIBILITY_OR_FILL",
            "observed_bid_ask_credits_by_steps": {str(key): credits[key] for key in sorted(credits)}}
