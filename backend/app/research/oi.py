"""Current OI structure and broker baseline changes are different evidence."""
from app.research.quotes import ContractIdentity, aware, exact_contract, finite
from datetime import time


def local_delta(current, previous, contract, max_interval_seconds=240):
    if previous is None:
        return None, "MISSING_PRIOR"
    if not aware(current.timestamp_ist) or not aware(previous.timestamp_ist):
        return None, "INVALID_TIMESTAMP"
    if current.timestamp_ist.date() != previous.timestamp_ist.date():
        return None, "SESSION_CHANGED"
    elapsed = (current.timestamp_ist - previous.timestamp_ist).total_seconds()
    if not 0 < elapsed <= max_interval_seconds:
        return None, "INVALID_OI_INTERVAL"
    identity = ContractIdentity.of(contract)
    now, reason = exact_contract(current, identity)
    if now is None:
        return None, reason
    prior, reason = exact_contract(previous, identity)
    if prior is None:
        return None, reason
    for point, row in ((current, now), (previous, prior)):
        if not finite(row.open_interest) or row.open_interest < 0:
            return None, "INVALID_CURRENT_OR_PRIOR_OI"
        if row.source_market_timestamp is not None:
            if not aware(row.source_market_timestamp):
                return None, "INVALID_OI_SOURCE_TIMESTAMP"
            age = (point.timestamp_ist - row.source_market_timestamp).total_seconds()
            if not 0 <= age <= 30:
                return None, "STALE_OI_SOURCE"
    return now.open_interest - prior.open_interest, "VALID_ZERO" if now.open_interest == prior.open_interest else "VALID_DELTA"


def oi_quality(snapshot, previous=None, *, minimum_coverage=.8, max_interval_seconds=240):
    identities = [ContractIdentity.of(item) for item in snapshot.options]
    identity_ok = all(item.valid() for item in identities) and len(set(identities)) == len(identities)
    session_ok = (aware(snapshot.timestamp_ist) and snapshot.timestamp_ist.weekday() < 5
                  and time(9, 15) <= snapshot.timestamp_ist.time().replace(tzinfo=None) <= time(15, 30)
                  and snapshot.timestamp_ist.date() <= snapshot.expiry
                  and all(row.expiry == snapshot.expiry for row in snapshot.options))
    rows = snapshot.options
    current = [row for row in rows if finite(row.open_interest) and row.open_interest >= 0]
    side_coverage = all(
        len([row for row in current if row.option_type.value == side]) >= 1
        and len([row for row in current if row.option_type.value == side]) /
        max(1, len([row for row in rows if row.option_type.value == side])) >= minimum_coverage
        for side in ("CE", "PE"))
    changes = [row.change_in_open_interest for row in rows if finite(row.change_in_open_interest)]
    deltas = [local_delta(snapshot, previous, row, max_interval_seconds) for row in rows]
    usable = bool(identity_ok and session_ok and rows and len(current)/len(rows) >= minimum_coverage and side_coverage)
    return {"static_oi_usable": usable,
            "static_oi_coverage": len(current)/len(rows) if rows else 0,
            "broker_oi_change_available": bool(changes),
            "broker_oi_change_nonzero": any(value != 0 for value in changes),
            "local_delta_oi_usable": bool(deltas and all(value is not None for value, _ in deltas)),
            "local_delta_oi_reason": "VALID" if deltas and all(value is not None for value, _ in deltas)
            else next((reason for value, reason in deltas if value is None), "NO_CONTRACTS")}


def static_oi_usable(feature):
    quality = feature.data_quality
    # Old Phase 3 rows can supply static coverage without changing legacy intraday semantics.
    if quality.static_oi_usable is not None:
        return quality.static_oi_usable
    return bool(quality.contracts_total and quality.calls_total and quality.puts_total
                and quality.contracts_with_open_interest / quality.contracts_total >= .8)
