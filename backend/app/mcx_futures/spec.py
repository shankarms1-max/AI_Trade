"""MCX outright futures contract identity, resolved only from Kotak scrip-master rows.

Lot size and tick size are never hardcoded: they are read from the master row
(``lLotSize``, ``dTickSize`` / 10**``lPrecision``) and validated.  A row that
cannot be validated is refused rather than repaired.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SEGMENT = "mcx_fo"
SUPPORTED_INSTRUMENTS = ("CRUDEOILM", "NATGASMINI")
QUOTATION_UNITS = {"CRUDEOILM": "BBL", "NATGASMINI": "MMBTU"}


class SpecError(ValueError):
    """Contract metadata is unavailable, ambiguous or malformed."""


@dataclass(frozen=True)
class ContractSpec:
    instrument: str
    trading_symbol: str
    token: str
    segment: str
    expiry: date
    lot_size: int
    tick_size: float
    precision: int
    quotation_unit: str
    rupees_per_point_per_lot: float
    source: str

    def __post_init__(self) -> None:
        if self.instrument not in SUPPORTED_INSTRUMENTS:
            raise SpecError(f"UNSUPPORTED_INSTRUMENT:{self.instrument}")
        if self.segment != SEGMENT:
            raise SpecError("SEGMENT_NOT_MCX_FO")
        if not isinstance(self.lot_size, int) or isinstance(self.lot_size, bool) or self.lot_size <= 0:
            raise SpecError("LOT_SIZE_INVALID")
        if not self.tick_size > 0:
            raise SpecError("TICK_SIZE_INVALID")
        if self.rupees_per_point_per_lot != self.lot_size:
            raise SpecError("RUPEES_PER_POINT_MUST_EQUAL_LOT_SIZE")
        if self.quotation_unit != QUOTATION_UNITS[self.instrument]:
            raise SpecError("QUOTATION_UNIT_MISMATCH")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expiry"] = self.expiry.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ContractSpec:
        data = dict(payload)
        data["expiry"] = date.fromisoformat(str(data["expiry"]))
        return cls(**data)

    def days_to_expiry(self, on: date) -> int:
        return (self.expiry - on).days


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SpecError(f"MASTER_FIELD_INVALID:{name}")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SpecError(f"MASTER_FIELD_INVALID:{name}") from None
    if not result.is_finite():
        raise SpecError(f"MASTER_FIELD_INVALID:{name}")
    return result


def tick_from_row(row: Mapping[str, Any]) -> tuple[float, int]:
    """Return (tick in rupees, precision) = dTickSize / 10**lPrecision."""
    if "lPrecision" not in row or "dTickSize" not in row:
        raise SpecError("MASTER_TICK_FIELDS_MISSING")
    precision = _decimal(row["lPrecision"], "lPrecision")
    raw = _decimal(row["dTickSize"], "dTickSize")
    if precision != precision.to_integral_value() or not 0 <= precision <= 8:
        raise SpecError("MASTER_FIELD_INVALID:lPrecision")
    if raw <= 0 or raw != raw.to_integral_value():
        raise SpecError("MASTER_FIELD_INVALID:dTickSize")
    return float(raw / (Decimal(10) ** int(precision))), int(precision)


def _lot(row: Mapping[str, Any]) -> int:
    value = _decimal(row.get("lLotSize"), "lLotSize")
    if value <= 0 or value != value.to_integral_value():
        raise SpecError("MASTER_FIELD_INVALID:lLotSize")
    return int(value)


def expiry_from_row(row: Mapping[str, Any]) -> date:
    """``pExpiryDate`` (e.g. 19Nov2026); ``lExpiryDate`` must agree when present.

    In the Kotak MCX master ``lExpiryDate`` is the expiry day 23:59:59 expressed
    as UTC epoch seconds (verified on the 2026-10-20 fixture), so its UTC date is
    the expiry date.
    """
    text = row.get("pExpiryDate")
    epoch = row.get("lExpiryDate")
    parsed: date | None = None
    if isinstance(text, str) and text.strip():
        try:
            parsed = datetime.strptime(text.strip(), "%d%b%Y").date()
        except ValueError:
            raise SpecError("MASTER_FIELD_INVALID:pExpiryDate") from None
    from_epoch: date | None = None
    if epoch not in (None, 0, "0", ""):
        value = _decimal(epoch, "lExpiryDate")
        if value <= 0:
            raise SpecError("MASTER_FIELD_INVALID:lExpiryDate")
        from_epoch = datetime.fromtimestamp(int(value), timezone.utc).date()
    if parsed and from_epoch and parsed != from_epoch:
        raise SpecError("MASTER_EXPIRY_FIELDS_DISAGREE")
    result = parsed or from_epoch
    if result is None:
        raise SpecError("MASTER_EXPIRY_UNAVAILABLE")
    return result


def spec_from_row(row: Mapping[str, Any], instrument: str, master_date: date) -> ContractSpec:
    if str(row.get("pExchSeg", SEGMENT)).strip().lower() != SEGMENT:
        raise SpecError("MASTER_SEGMENT_NOT_MCX_FO")
    token = str(row.get("pSymbol", "")).strip()
    symbol = str(row.get("pTrdSymbol", "")).strip()
    if not token or not symbol:
        raise SpecError("MASTER_IDENTITY_MISSING")
    tick, precision = tick_from_row(row)
    lot = _lot(row)
    return ContractSpec(
        instrument=instrument,
        trading_symbol=symbol,
        token=token,
        segment=SEGMENT,
        expiry=expiry_from_row(row),
        lot_size=lot,
        tick_size=tick,
        precision=precision,
        quotation_unit=QUOTATION_UNITS[instrument],
        rupees_per_point_per_lot=float(lot),
        source=f"KOTAK_SCRIP_MASTER:{master_date.isoformat()}",
    )


def _is_future(row: Mapping[str, Any], instrument: str) -> bool:
    return (
        str(row.get("pSymbolName", "")) == instrument
        and str(row.get("pInstType", "")).strip().upper() == "FUTCOM"
        and str(row.get("pOptionType", "")).strip().upper() == "XX"
    )


def resolve_futures(
    rows: Iterable[Mapping[str, Any]],
    instrument: str,
    today: date,
    min_days_to_expiry: int,
    master_date: date | None = None,
) -> ContractSpec:
    """Nearest FUTCOM with expiry >= today + min_days_to_expiry.

    Rollover rule: a contract already inside its expiry window is never chosen;
    the next listed contract is used instead.  Any malformed candidate row for
    the instrument refuses the whole resolution (the master is not trusted
    partially), and two contracts with the same expiry are ambiguous.
    """
    if instrument not in SUPPORTED_INSTRUMENTS:
        raise SpecError(f"UNSUPPORTED_INSTRUMENT:{instrument}")
    if min_days_to_expiry < 0:
        raise SpecError("MIN_DAYS_TO_EXPIRY_NEGATIVE")
    source_date = master_date or today
    candidates = [spec_from_row(row, instrument, source_date) for row in rows if _is_future(row, instrument)]
    if not candidates:
        raise SpecError("NO_FUTCOM_ROWS_FOR_INSTRUMENT")
    expiries = [item.expiry for item in candidates]
    if len(set(expiries)) != len(expiries):
        raise SpecError("AMBIGUOUS_FUTCOM_EXPIRY")
    cutoff = today + timedelta(days=min_days_to_expiry)
    eligible = sorted((item for item in candidates if item.expiry >= cutoff), key=lambda item: item.expiry)
    if not eligible:
        raise SpecError("NO_FUTCOM_OUTSIDE_EXPIRY_WINDOW")
    return eligible[0]


def round_to_tick(price: float, tick: float, side: str = "NEAREST") -> float:
    """Round to the tick grid.  side: UP (ceil), DOWN (floor) or NEAREST (half-up)."""
    if not tick > 0:
        raise SpecError("TICK_SIZE_INVALID")
    mode = {"UP": ROUND_CEILING, "DOWN": ROUND_FLOOR, "NEAREST": ROUND_HALF_UP}.get(side)
    if mode is None:
        raise ValueError(f"ROUND_SIDE_INVALID:{side}")
    step = Decimal(str(tick))
    units = (Decimal(str(price)) / step).quantize(Decimal(1), rounding=mode)
    return float(units * step)


def on_tick_grid(price: float, tick: float) -> bool:
    step = Decimal(str(tick))
    return (Decimal(str(price)) % step) == 0
