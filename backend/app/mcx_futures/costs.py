"""Versioned, dated transaction-cost schedule for MCX non-agricultural futures.

Every rate carries a ``status``: STATUTORY (set by law/regulator), or an
UNVERIFIED_ASSUMPTION / ASSUMPTION that must be checked against the current
MCX circular and the broker's tariff before any result is trusted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BROKERAGE_SCENARIOS = (0.0, 10.0, 20.0)


@dataclass(frozen=True)
class Rate:
    name: str
    value: float
    unit: str
    basis: str
    status: str
    source: str


@dataclass(frozen=True)
class CostSchedule:
    version: str
    effective_from: date
    ctt: Rate
    stamp_duty: Rate
    sebi_fee: Rate
    gst: Rate
    exchange_txn: Rate
    brokerage_per_order: Rate

    def with_brokerage(self, rupees_per_order: float) -> CostSchedule:
        return replace(
            self,
            version=f"{self.version}+BROKERAGE_{rupees_per_order:g}",
            brokerage_per_order=replace(self.brokerage_per_order, value=float(rupees_per_order)),
        )

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["effective_from"] = self.effective_from.isoformat()
        return data


DEFAULT_SCHEDULE = CostSchedule(
    version="MCX_NONAGRI_FUT_COSTS_2026_V2",
    effective_from=date(2026, 11, 1),
    ctt=Rate("CTT", 0.0001, "FRACTION_OF_VALUE", "SELL_SIDE_VALUE", "STATUTORY",
             "Finance Act 2013 Commodity Transaction Tax, non-agricultural futures 0.01% on sell side"),
    stamp_duty=Rate("STAMP_DUTY", 0.00002, "FRACTION_OF_VALUE", "BUY_SIDE_VALUE", "STATUTORY",
                    "Indian Stamp Act (Finance Act 2019 amendment, effective 2020) 0.002% on buy side"),
    sebi_fee=Rate("SEBI_TURNOVER_FEE", 10.0 / 1e7, "FRACTION_OF_VALUE", "TURNOVER", "STATUTORY",
                  "SEBI turnover fee Rs 10 per crore"),
    gst=Rate("GST", 0.18, "FRACTION_OF_CHARGES", "BROKERAGE+EXCHANGE+SEBI", "STATUTORY", "GST 18% on services"),
    exchange_txn=Rate("MCX_TRANSACTION_CHARGE", 2.10 / 1e5, "FRACTION_OF_VALUE", "TURNOVER",
                      "PUBLISHED_RATE_NOT_RECONFIRMED_FOR_2026 - Kotak product page and the MCX Sep-2024 revision both "
                      "list Rs 2.10 per lakh (0.0021%) for futures; no 2026 circular found",
                      "Rs 2.10 per lakh of futures turnover (kotaksecurities.com product page; MCX revision effective "
                      "2024-10-01)"),
    brokerage_per_order=Rate("BROKERAGE", 0.0, "RUPEES_PER_EXECUTED_ORDER", "ORDER",
                             "PUBLISHED_SCHEDULE_ACCOUNT_PLAN_UNCONFIRMED",
                             "Kotak Neo Trade API pricing page (kotakneo.com/trade-api): API orders carry no brokerage, "
                             "only exchange/SEBI/GST/statutory charges. Earlier versions assumed Rs 20 per order "
                             "(V1); scenarios Rs 0 / 10 / 20 remain available via with_brokerage()")
)


def _q(value: Decimal, places: str = "0.000001") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def contract_value(price: float, lot_size: int, lots: int) -> float:
    return float(Decimal(str(price)) * lot_size * lots)


def round_trip(schedule: CostSchedule, entry_price: float, exit_price: float, direction: str, lot_size: int,
               lots: int) -> dict[str, Any]:
    """Itemised charges for one round trip (2 executed orders).  direction: LONG or SHORT."""
    if direction not in ("LONG", "SHORT"):
        raise ValueError("DIRECTION_INVALID")
    entry = Decimal(str(entry_price)) * lot_size * lots
    exit_ = Decimal(str(exit_price)) * lot_size * lots
    buy, sell = (entry, exit_) if direction == "LONG" else (exit_, entry)
    turnover = buy + sell
    rate = {name: Decimal(str(getattr(schedule, name).value))
            for name in ("ctt", "stamp_duty", "sebi_fee", "gst", "exchange_txn", "brokerage_per_order")}
    ctt = sell * rate["ctt"]
    stamp = buy * rate["stamp_duty"]
    sebi = turnover * rate["sebi_fee"]
    exchange = turnover * rate["exchange_txn"]
    brokerage = 2 * rate["brokerage_per_order"]
    gst = (brokerage + exchange + sebi) * rate["gst"]
    total = ctt + stamp + sebi + exchange + brokerage + gst
    return {
        "schedule_version": schedule.version,
        "buy_value": _q(buy, "0.01"),
        "sell_value": _q(sell, "0.01"),
        "turnover": _q(turnover, "0.01"),
        "items": {
            "ctt": _q(ctt), "stamp_duty": _q(stamp), "sebi_fee": _q(sebi), "exchange_txn": _q(exchange),
            "brokerage": _q(brokerage), "gst": _q(gst),
        },
        "statuses": {
            "ctt": schedule.ctt.status, "stamp_duty": schedule.stamp_duty.status, "sebi_fee": schedule.sebi_fee.status,
            "exchange_txn": schedule.exchange_txn.status, "brokerage": schedule.brokerage_per_order.status,
            "gst": schedule.gst.status,
        },
        "total": _q(total, "0.01"),
    }
