"""One fail-closed contract for observed books and executable size.

Depth UNKNOWN is intentional: neither a token nor a numeric quantity proves its
unit. Only confirmed UNITS or LOTS metadata permits executable-size comparison.
"""
from dataclasses import asdict, dataclass
from datetime import date, datetime
from math import isfinite


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def information_time(snapshot):
    receipt = snapshot.response_received_at
    return max(snapshot.timestamp_ist, receipt) if aware(receipt) else snapshot.timestamp_ist


@dataclass(frozen=True)
class ContractIdentity:
    exchange: str
    token: str | None
    expiry: object
    strike: float
    option_type: str

    @classmethod
    def of(cls, item):
        return cls(getattr(item, "exchange", "nse_fo"), item.instrument_token,
                   item.expiry, item.strike, str(getattr(item.option_type, "value", item.option_type)))

    def valid(self):
        return bool(isinstance(self.exchange, str) and self.exchange.strip()
                    and isinstance(self.token, str) and self.token.strip()
                    and isinstance(self.expiry, date) and not isinstance(self.expiry, datetime)
                    and finite(self.strike) and self.strike > 0
                    and self.option_type in {"CE", "PE"})


@dataclass(frozen=True)
class QuotePolicy:
    max_observation_age_seconds: float = 30
    max_source_age_seconds: float = 30
    max_leg_skew_seconds: float = 10
    future_tolerance_seconds: float = 0
    source_timestamp_required: bool = True
    max_spread_absolute: float | None = None
    max_spread_percent: float | None = 30
    allow_unknown_depth: bool = False

    def __post_init__(self):
        values = (self.max_observation_age_seconds, self.max_source_age_seconds,
                  self.max_leg_skew_seconds, self.future_tolerance_seconds)
        if any(not finite(x) or x < 0 for x in values):
            raise ValueError("invalid quote time policy")
        if any(x is not None and (not finite(x) or x < 0)
               for x in (self.max_spread_absolute, self.max_spread_percent)):
            raise ValueError("invalid quote spread policy")


def policy_quote_contract(policy, *, max_spread_percent=None, atm=False):
    """Reuse the run's time contract; consumers may tighten, never relax spreads."""
    from dataclasses import replace
    base = policy.research_quote_policy or QuotePolicy(
        max_source_age_seconds=policy.source_quote_max_age_seconds,
        max_leg_skew_seconds=policy.quote_max_leg_skew_seconds)
    limits = [value for value in (base.max_spread_percent, max_spread_percent,
                                  policy.atm_max_spread_percent if atm else None) if value is not None]
    absolute = [value for value in (base.max_spread_absolute, policy.atm_max_spread_absolute if atm else None)
                if value is not None]
    return replace(base, source_timestamp_required=True,
                   max_spread_percent=min(limits) if limits else None,
                   max_spread_absolute=min(absolute) if absolute else None)


@dataclass(frozen=True)
class QuoteResult:
    identity: ContractIdentity
    valid: bool
    reason: str
    fill_price: float | None = None
    quote_basis: str = "OBSERVED_BID_ASK"
    observation_age: float | None = None
    source_age: float | None = None
    source_timestamp: datetime | None = None
    requested_lots: int | None = None
    requested_units: int | None = None
    available_units: int | None = None
    depth_unit: str = "UNKNOWN"
    depth_status: str = "NOT_CHECKED"
    fill_validity_state: str = "INVALID_FILL"

    def payload(self):
        return asdict(self)


def exact_contract(snapshot, identity):
    if not identity.valid():
        return None, "INVALID_EXACT_CONTRACT_IDENTITY"
    matches = [item for item in snapshot.options if ContractIdentity.of(item) == identity]
    if len(matches) != 1:
        return None, "EXACT_CONTRACT_AMBIGUOUS" if matches else "MISSING_EXACT_CONTRACT"
    return matches[0], "OK"


def validate_book(contract, observation_at, policy=QuotePolicy(), *, evaluated_at=None):
    """Quality is independent of depth; used by context, construction and risk."""
    identity = ContractIdentity.of(contract)
    source = contract.source_market_timestamp
    fields = dict(identity=identity, source_timestamp=source,
                  depth_unit=getattr(contract, "depth_unit", "UNKNOWN"))
    def result(reason, **extra):
        return QuoteResult(valid=reason == "OK", reason=reason, **(fields | extra))
    if not identity.valid():
        return result("INVALID_EXACT_CONTRACT_IDENTITY")
    evaluated_at = observation_at if evaluated_at is None else evaluated_at
    if not aware(observation_at) or not aware(evaluated_at):
        return result("INVALID_OBSERVATION_TIMESTAMP")
    age = (evaluated_at - observation_at).total_seconds()
    fields["observation_age"] = age
    if age < -policy.future_tolerance_seconds or age > policy.max_observation_age_seconds:
        return result("STALE_OR_FUTURE_OBSERVATION")
    if source is None and policy.source_timestamp_required:
        return result("MISSING_QUOTE_TIMESTAMP")
    if source is not None:
        if not aware(source):
            return result("INVALID_SOURCE_TIMESTAMP")
        source_age = (evaluated_at - source).total_seconds()
        fields["source_age"] = source_age
        if source_age < -policy.future_tolerance_seconds:
            return result("FUTURE_SOURCE_TIMESTAMP")
        if source_age > policy.max_source_age_seconds:
            return result("STALE_LEG_QUOTE")
    bid, ask = contract.bid, contract.ask
    if bid is None or ask is None:
        return result("MISSING_EXECUTABLE_SIDE")
    if not finite(bid) or not finite(ask):
        return result("NONFINITE_EXECUTABLE_PRICE")
    if bid <= 0 or ask <= 0:
        return result("NONPOSITIVE_EXECUTABLE_PRICE")
    if bid > ask:
        return result("CROSSED_BOOK")
    tick = getattr(contract, "tick_size", None)
    if tick is not None:
        if not finite(tick) or tick <= 0 or any(abs(p / tick - round(p / tick)) > 1e-6 for p in (bid, ask)):
            return result("INVALID_TICK_PRICE")
    spread, percent = ask - bid, 100 * (ask - bid) / ((ask + bid) / 2)
    if ((policy.max_spread_absolute is not None and spread > policy.max_spread_absolute)
            or (policy.max_spread_percent is not None and percent > policy.max_spread_percent)):
        return result("BOOK_SPREAD_TOO_WIDE")
    return result("OK", fill_validity_state="VALID_BOOK")


def executable_quote(snapshot, leg, side, lots, policy=QuotePolicy(), *, evaluated_at=None):
    if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
        raise ValueError("requested lots must be a positive integer")
    lot_size = snapshot.lot_size
    requested_units = lots * lot_size if isinstance(lot_size, int) and not isinstance(lot_size, bool) and lot_size > 0 else None
    identity = ContractIdentity.of(leg)
    contract, reason = exact_contract(snapshot, identity)
    if contract is None:
        return QuoteResult(identity, False, reason, requested_lots=lots, requested_units=requested_units)
    book = validate_book(contract, snapshot.timestamp_ist, policy, evaluated_at=evaluated_at)
    if not book.valid:
        from dataclasses import replace
        return replace(book, requested_lots=lots, requested_units=requested_units)
    if side not in {"bid", "ask"}:
        raise ValueError("executable side must be bid or ask")
    fields = book.payload()
    fields["identity"] = book.identity
    fields.update(fill_price=getattr(contract, side), requested_lots=lots,
                  requested_units=requested_units)
    def fail(reason, status):
        return QuoteResult(**(fields | dict(valid=False, reason=reason, depth_status=status,
                                           fill_validity_state="NOT_EVALUABLE")))
    if fields["requested_units"] is None:
        return fail("LOT_SIZE_UNAVAILABLE", "UNKNOWN")
    depth = getattr(contract, f"{side}_quantity")
    unit = fields["depth_unit"]
    # Even unknown-unit zero is known insufficient for any positive order.
    if depth is not None and (not finite(depth) or depth < 0 or depth != int(depth)):
        return fail("INVALID_DEPTH", "INVALID")
    if depth == 0:
        fields["available_units"] = 0
        return fail("INSUFFICIENT_DEPTH", "INSUFFICIENT")
    if depth is not None and unit in {"UNITS", "LOTS"}:
        available = int(depth) * (lot_size if unit == "LOTS" else 1)
        fields["available_units"] = available
        if available < fields["requested_units"]:
            return fail("INSUFFICIENT_DEPTH", "INSUFFICIENT")
        fields.update(depth_status="EXECUTABLE_DEPTH", fill_validity_state="EXECUTABLE_DEPTH")
    elif policy.allow_unknown_depth:
        fields.update(depth_status="UNKNOWN_DEPTH_SIMULATION",
                      fill_validity_state="UNKNOWN_DEPTH_SIMULATION")
    else:
        return fail("UNKNOWN_REQUIRED_DEPTH", "UNKNOWN")
    return QuoteResult(**fields)


def execution_pair(snapshot, candidate, *, entry, lots, policy=QuotePolicy(), evaluated_at=None):
    short = executable_quote(snapshot, candidate.short_leg, "bid" if entry else "ask", lots, policy, evaluated_at=evaluated_at)
    long = executable_quote(snapshot, candidate.long_leg, "ask" if entry else "bid", lots, policy, evaluated_at=evaluated_at)
    # Check BOTH required sides, preserving insufficiency even if another side is unknown.
    invalid = [item for item in (short, long) if not item.valid]
    if invalid:
        first = min(invalid, key=lambda x: x.reason != "INSUFFICIENT_DEPTH")
        return None, first.reason, (short, long)
    if short.source_timestamp and long.source_timestamp:
        skew = abs((short.source_timestamp - long.source_timestamp).total_seconds())
        if skew > policy.max_leg_skew_seconds:
            return None, "LEG_TIME_SKEW", (short, long)
    return (short, long), "OK", (short, long)
