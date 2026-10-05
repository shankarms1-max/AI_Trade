"""Dated, explicit research charges. Rates are supplied, never inferred."""
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from app.research.quotes import finite


@dataclass(frozen=True)
class CostSchedule:
    cost_schedule_version: str = "UNCONFIGURED"
    effective_from: date | None = None
    effective_to: date | None = None
    brokerage_per_order: float | None = None
    exchange_rate: float | None = None
    stt_rate: float | None = None
    gst_rate: float | None = None
    stamp_rate: float | None = None
    sebi_rate: float | None = None
    slippage_points_per_leg: float | None = None
    declared_complete: bool = False
    brokerage_model: str = "FIXED_PER_ORDER"
    stt_rule: str = "SELL_PREMIUM"
    stamp_rule: str = "BUY_PREMIUM"
    gst_rule: str = "BROKERAGE_EXCHANGE_SEBI"
    slippage_model: str = "POINTS_PER_EXECUTED_LEG"
    rounding_rule: str = "PER_COMPONENT_HALF_UP_2DP"

    def __post_init__(self):
        for name in self.rate_fields():
            value = getattr(self, name)
            if value is not None and (not finite(value) or value < 0):
                raise ValueError(f"invalid cost input: {name}")
        if self.effective_to and (not self.effective_from or self.effective_to < self.effective_from):
            raise ValueError("invalid cost schedule date range")
        if (self.brokerage_model, self.stt_rule, self.stamp_rule, self.gst_rule,
            self.slippage_model, self.rounding_rule) != (
                "FIXED_PER_ORDER", "SELL_PREMIUM", "BUY_PREMIUM", "BROKERAGE_EXCHANGE_SEBI",
                "POINTS_PER_EXECUTED_LEG", "PER_COMPONENT_HALF_UP_2DP"):
            raise ValueError("unsupported cost accounting rule")

    @staticmethod
    def rate_fields():
        return ("brokerage_per_order", "exchange_rate", "stt_rate", "gst_rate", "stamp_rate",
                "sebi_rate", "slippage_points_per_leg")

    def complete_at(self, day):
        rates = [getattr(self, name) for name in self.rate_fields()]
        return bool(self.declared_complete and self.cost_schedule_version != "UNCONFIGURED"
                    and self.effective_from and self.effective_from <= day
                    and (self.effective_to is None or day <= self.effective_to)
                    and all(value is not None for value in rates)
                    and any(value > 0 for value in rates if value is not None))

    def calculate(self, *, day, lot_size, lots, entry_short, entry_long,
                  exit_short, exit_long, turnover_basis="ACTUAL", order_count=4):
        if (not isinstance(lot_size, int) or isinstance(lot_size, bool) or lot_size <= 0 or not isinstance(lots, int)
                or isinstance(lots, bool) or lots <= 0 or not isinstance(order_count, int)
                or isinstance(order_count, bool) or order_count < 4):
            raise ValueError("confirmed lot size, lots and explicit order count are required")
        prices = (entry_short, entry_long, exit_short, exit_long)
        if any(not finite(value) or value <= 0 for value in prices):
            raise ValueError("finite positive actual premiums required for accounting")
        if turnover_basis not in {"ACTUAL", "ESTIMATED"}:
            raise ValueError("invalid turnover basis")
        units = lot_size * lots
        dec = lambda v: Decimal(str(v))
        money = lambda v: float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        sold = (dec(entry_short) + dec(exit_long)) * units
        bought = (dec(entry_long) + dec(exit_short)) * units
        total = sold + bought
        components = {
            "brokerage": None if self.brokerage_per_order is None else money(dec(self.brokerage_per_order) * order_count),
            "exchange_fees": None if self.exchange_rate is None else money(total * dec(self.exchange_rate)),
            "STT": None if self.stt_rate is None else money(sold * dec(self.stt_rate)),
            "stamp": None if self.stamp_rate is None else money(bought * dec(self.stamp_rate)),
            "SEBI_fee": None if self.sebi_rate is None else money(total * dec(self.sebi_rate)),
            "slippage": None if self.slippage_points_per_leg is None else
                money(dec(self.slippage_points_per_leg) * units * 4),
        }
        gst_base = [components[key] for key in ("brokerage", "exchange_fees", "SEBI_fee")]
        components["GST"] = (None if self.gst_rate is None or any(v is None for v in gst_base)
                             else money(sum(dec(v) for v in gst_base) * dec(self.gst_rate)))
        complete = self.complete_at(day) and all(v is not None for v in components.values())
        known = money(sum((dec(v) for v in components.values() if v is not None), Decimal(0)))
        gross_points_decimal = dec(entry_short) - dec(entry_long) - dec(exit_short) + dec(exit_long)
        gross_points = float(gross_points_decimal)
        gross_rupees = money(gross_points_decimal * units)
        return {**components, "gross_points_per_unit": gross_points,
                "gross_rupees_per_lot": money(gross_points_decimal * lot_size),
                "gross_rupees": gross_rupees, "known_partial_cost_rupees": known,
                "total_cost_rupees": known if complete else None,
                "net_rupees": money(dec(gross_rupees) - dec(known)) if complete else None,
                "net_rupees_per_lot_allocated": money((dec(gross_rupees)-dec(known))/lots) if complete else None,
                "net_points_per_unit": gross_points - known / units if complete else None,
                "cost_completeness": "NET_COMPLETE" if complete else "GROSS_ONLY",
                "cost_schedule_version": self.cost_schedule_version,
                "turnover_basis": turnover_basis, "sold_turnover_rupees": float(sold),
                "bought_turnover_rupees": float(bought), "order_count": order_count,
                "requested_lots": lots, "requested_units": units, "unit": "RUPEES_FOR_QUANTITY",
                "per_lot_allocation": "TOTAL_COST_AND_NET_ALLOCATED_ACROSS_REQUESTED_LOTS"}

    def payload(self):
        return asdict(self)


def estimated_cost(schedule, snapshot, short_price, long_price, lots=1):
    if snapshot.lot_size is None:
        return {"cost_completeness": "GROSS_ONLY", "total_cost_rupees": None,
                "turnover_basis": "ESTIMATED", "cost_schedule_version": schedule.cost_schedule_version}
    return schedule.calculate(day=snapshot.timestamp_ist.date(), lot_size=snapshot.lot_size,
                              lots=lots, entry_short=short_price, entry_long=long_price,
                              exit_short=short_price, exit_long=long_price, turnover_basis="ESTIMATED")
