"""Trading-charge model for the portfolio read layer. Decimal arithmetic throughout; every component carries a status.

Brokerage is NOT assumed. `PORTFOLIO_BROKERAGE_PER_ORDER` (rupees per executed order) defaults to 0, which is the
Kotak Neo Trade-API schedule per Kotak's published brokerage list - but the actual account plan could not be
verified, so the component is reported as UNVERIFIED_ASSUMPTION. Zero brokerage does not mean zero cost: STT,
exchange, SEBI, stamp duty and GST are always charged. Slippage is not a charge and is never added here (paper
fills already cross the displayed bid/ask).

NSE index-option charges (premium turnover; STT on the SELL side only, stamp on the BUY side only):
  STT   before 2023-04-01 0.05%   2023-04-01 0.0625%   2024-10-01 0.10%   2026-04-01 0.15%
  Exchange transaction charge 0.0495% (APPROX) -> 0.03503% from 2024-10-01 ; SEBI Rs 10 / crore ; stamp 0.003% ; GST 18%
  on brokerage + exchange + SEBI. Source tables: nsd/lab/costs_v25.py, checked 2026-11-24 (exchange rate APPROX).
MCX futures charges come from the engine's versioned schedule (app.mcx_futures.costs) with its own per-rate status.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
import os
from typing import Any, Iterable

from app.mcx_futures.costs import DEFAULT_SCHEDULE as MCX_SCHEDULE

D = Decimal
STT_OPT = [(date(2026, 4, 1), D("0.0015")), (date(2024, 10, 1), D("0.001")), (date(2023, 4, 1), D("0.000625")),
           (date(1900, 1, 1), D("0.0005"))]
EXCH_OPT = [(date(2024, 10, 1), D("0.0003503")), (date(1900, 1, 1), D("0.000495"))]
SEBI = D(10) / D(10_000_000)
STAMP_OPT = D("0.00003")
GST = D("0.18")


def _rate(table: list[tuple[date, Decimal]], on: date) -> Decimal:
    return next(rate for start, rate in table if on >= start)


def money(value: Decimal | float | int, places: str = "0.01") -> float:
    return float(D(str(value)).quantize(D(places), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ChargeModel:
    brokerage_per_order: Decimal = D(0)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChargeModel":
        e = os.environ if env is None else env
        try:
            return cls(D(str(e.get("PORTFOLIO_BROKERAGE_PER_ORDER", "0"))))
        except Exception:  # noqa: BLE001 - a bad value must not break monitoring
            return cls(D(0))

    def status(self) -> dict[str, str]:
        brokerage = ("PUBLISHED_SCHEDULE: Kotak Neo Trade API orders carry no brokerage (kotakneo.com/trade-api); this account's plan is not confirmed"
                     if self.brokerage_per_order == 0 else "CONFIGURED_SCENARIO")
        return {"brokerage": brokerage, "stt": "STATUTORY", "stamp_duty": "STATUTORY", "sebi_fee": "STATUTORY",
                "gst": "STATUTORY", "exchange": "APPROX (NSE options 0.0495% pre-Oct-2024 / 0.03503% after); "
                "MCX 'UNVERIFIED_ASSUMPTION' per app.mcx_futures.costs"}

    # -- NSE index options (credit spread legs) ----------------------------------------------------------------
    def options(self, on: date, fills: Iterable[tuple[float, int]], quantity: int) -> dict[str, Any]:
        """fills: [(premium, +1 BUY / -1 SELL)] for each executed order; quantity = units per order."""
        buys = sum((D(str(p)) for p, s in fills if s > 0), D(0)) * quantity
        sells = sum((D(str(p)) for p, s in fills if s < 0), D(0)) * quantity
        turnover = buys + sells
        orders = sum(1 for _ in fills) if not isinstance(fills, list) else len(fills)
        stt = sells * _rate(STT_OPT, on)
        exch = turnover * _rate(EXCH_OPT, on)
        sebi = turnover * SEBI
        stamp = buys * STAMP_OPT
        brokerage = self.brokerage_per_order * orders
        gst = (brokerage + exch + sebi) * GST
        return self._pack(stt, exch, sebi, stamp, brokerage, gst, orders, turnover)

    # -- MCX futures ---------------------------------------------------------------------------------------------
    def futures(self, price: float, quantity: int, side: str, orders: int = 1) -> dict[str, Any]:
        """Charges for ONE executed futures order (side BUY|SELL) of `quantity` units at `price`."""
        s = MCX_SCHEDULE
        value = D(str(price)) * quantity
        ctt = value * D(str(s.ctt.value)) if side == "SELL" else D(0)
        stamp = value * D(str(s.stamp_duty.value)) if side == "BUY" else D(0)
        sebi = value * D(str(s.sebi_fee.value))
        exch = value * D(str(s.exchange_txn.value))
        brokerage = self.brokerage_per_order * orders
        gst = (brokerage + exch + sebi) * D(str(s.gst.value))
        return self._pack(ctt, exch, sebi, stamp, brokerage, gst, orders, value, stt_name="ctt")

    @staticmethod
    def _pack(stt: Decimal, exch: Decimal, sebi: Decimal, stamp: Decimal, brokerage: Decimal, gst: Decimal,
              orders: int, turnover: Decimal, stt_name: str = "stt") -> dict[str, Any]:
        total = stt + exch + sebi + stamp + brokerage + gst
        return {"total": money(total), "orders": orders, "turnover": money(turnover),
                "items": {stt_name: money(stt), "exchange": money(exch), "sebi_fee": money(sebi),
                          "stamp_duty": money(stamp), "brokerage": money(brokerage), "gst": money(gst)}}


def add(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """Sum two charge packs (None-safe)."""
    a, b = a or {"total": 0.0, "orders": 0, "turnover": 0.0, "items": {}}, b or {"total": 0.0, "orders": 0,
                                                                              "turnover": 0.0, "items": {}}
    items = dict(a["items"])
    for k, v in b["items"].items():
        items[k] = money(D(str(items.get(k, 0))) + D(str(v)))
    return {"total": money(D(str(a["total"])) + D(str(b["total"]))), "orders": a["orders"] + b["orders"],
            "turnover": money(D(str(a["turnover"])) + D(str(b["turnover"]))), "items": items}
