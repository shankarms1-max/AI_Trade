"""Observed bid/ask fills, marks, accounting, and exit rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.research.costs import CostSchedule
from app.scalper.config import ScalperConfig
from app.scalper.models import (ScalperCandidate, ScalperMarketSnapshot,
                                ScalperSignal)
from app.scalper.strategy import exact_quote, validate_book


@dataclass(frozen=True)
class ExecutionPair:
    short_price: float
    long_price: float
    value: float
    evidence: dict


def executable_pair(snapshot: ScalperMarketSnapshot, candidate: ScalperCandidate,
                    config: ScalperConfig, *,
                    entry: bool) -> tuple[ExecutionPair | None, str | None]:
    short = exact_quote(snapshot, candidate.short_leg)
    long = exact_quote(snapshot, candidate.long_leg)
    if short is None or long is None:
        return None, "MISSING_EXACT_CONTRACT"
    short_side: Literal["bid", "ask"] = "bid" if entry else "ask"
    long_side: Literal["bid", "ask"] = "ask" if entry else "bid"
    short_book = validate_book(short, snapshot.response_received_at, snapshot.lot_size,
                               config, entry=entry, side=short_side)
    long_book = validate_book(long, snapshot.response_received_at, snapshot.lot_size,
                              config, entry=entry, side=long_side)
    if not short_book.valid:
        return None, short_book.reason
    if not long_book.valid:
        return None, long_book.reason
    short_price = getattr(short, short_side)
    long_price = getattr(long, long_side)
    assert short_price is not None and long_price is not None
    value = short_price - long_price
    if not 0 <= value < candidate.spread_width:
        return None, "INVALID_EXECUTABLE_PAYOFF"
    evidence = {
        "short": {"side": short_side, "fill_price": short_price,
                  "validation": short_book.payload(), "quote": short.model_dump(mode="json")},
        "long": {"side": long_side, "fill_price": long_price,
                 "validation": long_book.payload(), "quote": long.model_dump(mode="json")},
    }
    return ExecutionPair(short_price, long_price, value, evidence), None


def exit_trigger(document: dict, snapshot: ScalperMarketSnapshot,
                 signal: ScalperSignal, current_debit: float,
                 config: ScalperConfig) -> str | None:
    entry_credit = document["entry_credit"]
    pnl = entry_credit - current_debit
    at = snapshot.captured_at
    if at.time().replace(tzinfo=None) >= config.forced_exit_time:
        return "FORCED_INTRADAY_CLOSE"
    if current_debit >= entry_credit * config.stop_credit_multiple:
        return "SPREAD_STOP"
    direction = document["candidate"]["direction"]
    reference = document.get("structural_reference")
    if reference is not None and ((direction == "BULL" and snapshot.nifty_spot < reference)
                                  or (direction == "BEAR" and snapshot.nifty_spot > reference)):
        return "STRUCTURE_FAILURE"
    threshold = (signal.applicable_entry_threshold if signal.applicable_entry_threshold is not None
                 else config.signal_min_score)
    if signal.direction.value != direction or signal.score < threshold:
        return "SIGNAL_FAILURE"
    best = max(document.get("best_gross_points", 0.0), pnl)
    activation = entry_credit * config.trailing_activation_pct / 100.0
    giveback = entry_credit * config.trailing_giveback_pct / 100.0
    if best >= activation and pnl <= best - giveback:
        return "TRAILING_EXIT"
    if current_debit <= entry_credit * (1.0 - config.profit_capture_pct / 100.0):
        return "PROFIT_CAPTURE"
    entered = datetime.fromisoformat(document["entry_timestamp"])
    if (at - entered).total_seconds() >= config.time_stop_minutes * 60:
        return "TIME_STOP"
    return None


def cost_schedule(settings) -> CostSchedule:
    return CostSchedule(
        cost_schedule_version=settings.scalper_cost_schedule_version,
        brokerage_per_order=settings.scalper_brokerage_per_order,
        exchange_rate=settings.scalper_exchange_rate,
        stt_rate=settings.scalper_stt_rate,
        gst_rate=settings.scalper_gst_rate,
        stamp_rate=settings.scalper_stamp_rate,
        sebi_rate=settings.scalper_sebi_rate,
        slippage_points_per_leg=settings.scalper_slippage_points_per_leg,
        declared_complete=False,
    )
