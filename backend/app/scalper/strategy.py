"""Executable vertical construction and ranking for the fast scalper."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from math import isfinite
from typing import Any, Literal

from app.research.manifest import digest
from app.scalper.config import ScalperConfig
from app.scalper.models import (ScalperCandidate, ScalperDirection, ScalperLeg,
                                ScalperMarketSnapshot, ScalperOptionQuote,
                                ScalperSignal)


@dataclass(frozen=True)
class BookResult:
    valid: bool
    reason: str | None
    spread_pct: float | None
    available_units: int | None

    def payload(self) -> dict[str, Any]:
        return {"valid": self.valid, "reason": self.reason,
                "spread_pct": self.spread_pct,
                "available_units": self.available_units}


@dataclass(frozen=True)
class CandidateBuildResult:
    candidates: list[ScalperCandidate]
    rejection_counts: dict[str, int]


def validate_book(quote: ScalperOptionQuote, observed_at, lot_size: int,
                  config: ScalperConfig, *, entry: bool = True,
                  side: Literal["bid", "ask"] | None = None) -> BookResult:
    """Validate the real book; no LTP substitution or inferred quantity unit."""
    if quote.bid is None or quote.ask is None:
        return BookResult(False, "MISSING_EXECUTABLE_SIDE", None, None)
    if not all(isfinite(value) and value > 0 for value in (quote.bid, quote.ask)):
        return BookResult(False, "NONPOSITIVE_EXECUTABLE_PRICE", None, None)
    if quote.ask < quote.bid:
        return BookResult(False, "CROSSED_BOOK", None, None)
    if quote.tick_size is not None:
        if not isfinite(quote.tick_size) or quote.tick_size <= 0:
            return BookResult(False, "INVALID_TICK_SIZE", None, None)
        for price in (quote.bid, quote.ask):
            ticks = price / quote.tick_size
            if abs(ticks - round(ticks)) > 1e-6:
                return BookResult(False, "INVALID_TICK_PRICE", None, None)
    midpoint = (quote.ask + quote.bid) / 2.0
    spread = (quote.ask - quote.bid) / midpoint * 100.0
    if spread > config.max_bid_ask_spread_pct:
        return BookResult(False, "BID_ASK_SPREAD_TOO_WIDE", spread, None)
    if quote.source_market_timestamp is None:
        return BookResult(False, "MISSING_QUOTE_TIMESTAMP", spread, None)
    source = quote.source_market_timestamp
    observed = observed_at
    if source.tzinfo is None:
        source = source.replace(tzinfo=timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = (observed - source).total_seconds()
    if age < -1 or age > config.max_quote_age_seconds:
        return BookResult(False, "STALE_OR_FUTURE_QUOTE", spread, None)
    quantity = ((quote.bid_quantity or 0) if side == "bid" else
                (quote.ask_quantity or 0) if side == "ask" else
                min(quote.bid_quantity or 0, quote.ask_quantity or 0))
    if quote.depth_unit == "LOTS":
        quantity *= lot_size
    elif quote.depth_unit != "UNITS":
        return BookResult(False, "UNKNOWN_REQUIRED_DEPTH", spread, None)
    required = lot_size * config.lots
    if quantity < required:
        return BookResult(False, "INSUFFICIENT_DEPTH", spread, quantity)
    if entry and (quote.open_interest is None
                  or quote.open_interest < config.min_open_interest):
        return BookResult(False, "OPEN_INTEREST_TOO_LOW", spread, quantity)
    if entry and (quote.volume is None or quote.volume < config.min_volume):
        return BookResult(False, "VOLUME_TOO_LOW", spread, quantity)
    return BookResult(True, None, spread, quantity)


def _leg(quote: ScalperOptionQuote) -> ScalperLeg:
    return ScalperLeg(expiry=quote.expiry, strike=quote.strike,
                      option_type=quote.option_type, exchange=quote.exchange,
                      trading_symbol=quote.trading_symbol,
                      instrument_token=quote.instrument_token)


def build_candidates(snapshot: ScalperMarketSnapshot, signal: ScalperSignal,
                     config: ScalperConfig) -> CandidateBuildResult:
    if signal.direction == ScalperDirection.NEUTRAL:
        return CandidateBuildResult([], {"NEUTRAL_SIGNAL": 1})
    option_type = "PE" if signal.direction == ScalperDirection.BULL else "CE"
    strategy_type: Literal["BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"] = (
        "BULL_PUT_SPREAD" if option_type == "PE" else "BEAR_CALL_SPREAD")
    by_key = {(item.strike, item.option_type): item for item in snapshot.quotes}
    candidates: list[ScalperCandidate] = []
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for short in snapshot.quotes:
        if short.option_type != option_type:
            continue
        distance = ((snapshot.nifty_spot - short.strike) if option_type == "PE"
                    else (short.strike - snapshot.nifty_spot))
        if distance < config.min_short_distance_points:
            reject("SHORT_STRIKE_TOO_CLOSE")
            continue
        short_book = validate_book(short, snapshot.response_received_at,
                                   snapshot.lot_size, config, side="bid")
        if not short_book.valid:
            reject(short_book.reason or "SHORT_BOOK_INVALID")
            continue
        for width in config.allowed_widths:
            long_strike = short.strike - width if option_type == "PE" else short.strike + width
            long = by_key.get((long_strike, option_type))
            if long is None:
                reject("LONG_LEG_NOT_CAPTURED")
                continue
            long_book = validate_book(long, snapshot.response_received_at,
                                      snapshot.lot_size, config, side="ask")
            if not long_book.valid:
                reject(long_book.reason or "LONG_BOOK_INVALID")
                continue
            credit = short.bid - long.ask  # type: ignore[operator]
            if not isfinite(credit) or credit < config.min_credit:
                reject("EXECUTABLE_CREDIT_TOO_LOW")
                continue
            ratio = credit / width
            if ratio < config.min_credit_to_width or credit >= width:
                reject("CREDIT_WIDTH_RISK_INVALID")
                continue
            max_loss = width - credit
            max_loss_lot = max_loss * snapshot.lot_size * config.lots
            execution = max(0.0, 30.0 - ((short_book.spread_pct or 0)
                                         + (long_book.spread_pct or 0)) / 2.0)
            liquidity = min(20.0, 10.0 * min(
                (short.open_interest or 0) / max(config.min_open_interest, 1),
                (long.open_interest or 0) / max(config.min_open_interest, 1)))
            economics = min(20.0, ratio / max(config.min_credit_to_width, .000001) * 8.0)
            proximity = max(0.0, 20.0 - distance / 20.0)
            signal_fit = signal.score / 10.0
            components = {"execution": execution, "liquidity": liquidity,
                          "economics": economics, "proximity": proximity,
                          "signal": signal_fit}
            candidate_key = [snapshot.expiry.isoformat(), strategy_type,
                             short.instrument_token, long.instrument_token, width]
            candidates.append(ScalperCandidate(
                candidate_id=digest(candidate_key), strategy_type=strategy_type,
                direction=signal.direction, short_leg=_leg(short), long_leg=_leg(long),
                spread_width=float(width), short_distance_points=distance,
                executable_credit=credit, credit_to_width=ratio,
                defined_max_loss_per_unit=max_loss,
                defined_max_loss_per_lot=max_loss_lot,
                ranking_score=round(sum(components.values()), 6),
                ranking_components={key: round(value, 6)
                                    for key, value in components.items()},
                holding_horizon_minutes=config.time_stop_minutes,
                quote_evidence={"short": {**short.model_dump(mode="json"),
                                          "validation": short_book.payload(), "side": "bid"},
                                "long": {**long.model_dump(mode="json"),
                                         "validation": long_book.payload(), "side": "ask"}},
            ))
    candidates.sort(key=lambda item: (-item.ranking_score,
                                      item.defined_max_loss_per_lot,
                                      item.short_distance_points,
                                      item.candidate_id))
    return CandidateBuildResult(candidates, rejected)


def exact_quote(snapshot: ScalperMarketSnapshot, leg: ScalperLeg) -> ScalperOptionQuote | None:
    matches = [item for item in snapshot.quotes
               if (item.exchange, item.instrument_token, item.expiry, item.strike,
                   item.option_type, item.trading_symbol) ==
               (leg.exchange, leg.instrument_token, leg.expiry, leg.strike,
                leg.option_type, leg.trading_symbol)]
    if len(matches) > 1:
        raise ValueError("SCALPER_AMBIGUOUS_EXACT_CONTRACT")
    return matches[0] if matches else None
