"""Adaptive real-strike candidates and atomic observed books, with no broker access."""
from decimal import Decimal

from app.research.manifest import digest
from app.scalper.models import ScalperCandidate, ScalperLeg
from app.scalper.strategy import validate_book
from app.strategy.construction import construction_key, ordinal_score, short_leg_quality, premium_retention
from app.commodity_acts.paper_policy import margin_comparison, margin_for


def book(quote, snapshot, policy, known_at, *, entry, side, after=None):
    if quote is None:
        return None, "MISSING_EXACT_CONTRACT"
    if (quote.expiry != policy.evidence.option_expiry or quote.expiry < snapshot.timestamp_ist.date()
            or not policy.evidence.verified_at <= known_at <= policy.evidence.valid_until):
        return None, "EXECUTION_CONTRACT_EVIDENCE_EXPIRED_OR_CHANGED"
    source = quote.source_market_timestamp
    if source is None or source.tzinfo is None:
        return None, "MISSING_QUOTE_TIMESTAMP"
    if (source > snapshot.timestamp_ist or not 0 <= (known_at - source).total_seconds() <= policy.max_quote_age_seconds
            or after is not None and source <= after):
        return None, "STALE_OR_NONCAUSAL_QUOTE"
    if quote.lot_size != policy.evidence.lot_size:
        return None, "CONTRACT_QUANTITY_CHANGED"
    # Raw capture remains UNKNOWN. This transient copy interprets it ONLY under
    # dated, exact-contract operator evidence, preserved in the trade policy.
    interpreted = quote.model_copy(update={"depth_unit": policy.evidence.depth_unit, "tick_size": policy.evidence.tick_size})
    class Quality:
        max_bid_ask_spread_pct = policy.max_entry_spread_pct
        max_quote_age_seconds = policy.max_quote_age_seconds
        lots = 1
        min_open_interest = 0
        min_volume = 0
    validation = validate_book(interpreted, known_at, snapshot.lot_size, Quality(), entry=entry,
                               side=side, allow_wide_exit=not entry)
    if not validation.valid:
        return None, validation.reason
    return {"quote": quote.model_dump(mode="json"), "side": side, "price": getattr(quote, side),
            "validation": validation.payload(), "depth_interpretation": policy.evidence.model_dump(mode="json")}, None


def exact(snapshot, leg):
    keys = ("exchange", "instrument_token", "expiry", "strike", "option_type", "trading_symbol")
    identity = tuple(str(leg[key]) for key in keys)
    matches = [q for q in snapshot.options if tuple(str(getattr(q, key).value if key == "option_type" else getattr(q, key))
                                                   for key in keys) == identity]
    return matches[0] if len(matches) == 1 else None


def pair(snapshot, candidate, policy, known_at, *, entry, after=None):
    short, hedge = exact(snapshot, candidate["short_leg"]), exact(snapshot, candidate["long_leg"])
    short_book, failure = book(short, snapshot, policy, known_at, entry=entry, side="bid" if entry else "ask", after=after)
    if failure:
        return None, failure
    hedge_book, failure = book(hedge, snapshot, policy, known_at, entry=entry, side="ask" if entry else "bid", after=after)
    if failure:
        return None, failure
    value = float(Decimal(str(short_book["price"])) - Decimal(str(hedge_book["price"])))
    # A wide exit can cost more than theoretical expiry loss; never clamp away
    # an observed adverse debit. Negative debit / arbitrage data remain invalid.
    if value < 0 or entry and not 0 < value < candidate["spread_width"]:
        return None, "INVALID_EXECUTABLE_PAYOFF"
    return {"value": value, "short": short_book, "long": hedge_book, "atomic": True}, None


def candidates(snapshot, direction, ctx, policy, known_at):
    rejected, choices = {}, []
    def reject(reason): rejected[reason] = rejected.get(reason, 0) + 1
    failure = policy.metadata_reason(snapshot, known_at)
    if failure:
        return None, {failure: 1}
    side = "PE" if direction == "BULL" else "CE"
    rows = [q for q in snapshot.options if q.option_type.value == side]
    for short in rows:
        distance = (snapshot.reference_price - short.strike) if direction == "BULL" else (short.strike - snapshot.reference_price)
        if distance <= 0:
            continue
        short_book, failure = book(short, snapshot, policy, known_at, entry=True, side="bid")
        if failure:
            reject(failure)
            continue
        for hedge in rows:
            if not (hedge.strike < short.strike if side == "PE" else hedge.strike > short.strike):
                continue
            hedge_book, failure = book(hedge, snapshot, policy, known_at, entry=True, side="ask")
            if failure:
                reject(failure)
                continue
            width = abs(short.strike - hedge.strike)
            credit = float(Decimal(str(short.bid)) - Decimal(str(hedge.ask)))
            risk = (width - credit) * policy.evidence.rupees_per_point_per_lot
            if not policy.min_credit < credit < width or risk > policy.max_loss_per_trade:
                reject("DEFINED_RISK_OR_CREDIT_LIMIT")
                continue
            if short.ask - hedge.bid >= credit * policy.stop_credit_multiple:
                reject("ENTRY_ALREADY_AT_STOP")
                continue
            def leg(q):
                return ScalperLeg(expiry=q.expiry, strike=q.strike, option_type=q.option_type.value,
                    exchange=q.exchange, trading_symbol=q.trading_symbol, instrument_token=q.instrument_token)
            margin = margin_for(policy, short.instrument_token, hedge.instrument_token, snapshot.expiry, known_at)
            naked = margin_for(policy, short.instrument_token, None, snapshot.expiry, known_at)
            evidence = margin_comparison(naked, margin, credit=credit, hedge_ask=hedge.ask,
                                        width=width, multiplier=policy.evidence.rupees_per_point_per_lot)
            if policy.max_broker_margin is not None and (margin is None or margin["required_margin"] > policy.max_broker_margin):
                reject("MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED")
                continue
            # Quantity is normalized ONLY for the original ACTS execution score;
            # original raw quantities and their confirmed interpretation survive.
            short_book["bid_quantity"] = short_book["validation"]["available_units"]
            hedge_book["ask_quantity"] = hedge_book["validation"]["available_units"]
            choice = ScalperCandidate(candidate_id=digest({"instrument": snapshot.instrument_id,
                    "short": short.identity, "hedge": hedge.identity}), direction=direction,
                strategy_type="BULL_PUT_SPREAD" if side == "PE" else "BEAR_CALL_SPREAD",
                short_leg=leg(short), long_leg=leg(hedge), spread_width=width, short_distance_points=distance,
                executable_credit=credit, credit_to_width=credit / width,
                defined_max_loss_per_unit=width-credit, defined_max_loss_per_lot=risk,
                ranking_score=0, ranking_components={}, holding_horizon_minutes=policy.max_holding_minutes,
                quote_evidence={"short": short_book, "long": hedge_book},
                construction_method="OBSERVED_MCX_ADAPTIVE_DEFINED_RISK", construction_evidence={"margin": evidence})
            # Balanced, deterministic economics: distance + credit retained per
            # defined risk + liquidity/quality, not maximum raw premium alone.
            retention = premium_retention(short.bid, hedge.ask)
            choices.append(choice.model_copy(update={"premium_retention_ratio": retention}))
    if not choices:
        return None, rejected
    premium_reference = max(row.quote_evidence["short"]["price"] for row in choices)
    strikes = sorted({q.strike for q in snapshot.options})
    step = min(b-a for a, b in zip(strikes, strikes[1:]))
    wall = ctx.get("options", {}).get("walls", {}).get("put_support" if direction == "BULL" else "call_resistance")
    position = ctx.get("structure", {}).get("range_position")
    ranked = []
    for row in choices:
        short = row.quote_evidence["short"]
        structure = ((wall["strike"]-row.short_leg.strike if direction == "BULL" else row.short_leg.strike-wall["strike"]) / step
                     if wall else None)
        quality = short_leg_quality(price=short["price"], premium_reference=premium_reference,
            distance=row.short_distance_points, distance_scale=step, oi=short["quote"]["open_interest"], min_oi=0,
            volume=short["quote"]["volume"], min_volume=0, spread_pct=short["validation"]["spread_pct"],
            max_spread_pct=policy.max_entry_spread_pct, structure=structure,
            directional_fit=position if direction == "BULL" or position is None else 1-position)
        row.construction_evidence.update(quality)
        rank = construction_key(short_quality=quality["short_quality_score"], retention=row.premium_retention_ratio,
                                width=row.spread_width, identity=row.candidate_id)
        ranked.append((rank, row))
    ranked.sort(key=lambda item: item[0])
    for index, (_, row) in enumerate(ranked):
        row.ranking_score = ordinal_score(index, len(ranked))
        row.ranking_components = {"short_quality": row.construction_evidence["short_quality_score"],
                                  "premium_retention": row.premium_retention_ratio}
    return ranked[0][1], rejected
