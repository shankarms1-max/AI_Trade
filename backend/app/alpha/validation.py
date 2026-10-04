"""Pure offline Phase 14.1 session audits; no data-source calls or mutations."""

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from app.alpha.atm import select_atm
from app.alpha.clock import IST, session_id
from app.alpha.engine import AlphaConfig, build_alpha_features
from app.alpha.models import AlphaFeatureSnapshot
from app.alpha.volume_alpha import VolumeState, interval_volume
from app.data.models import MarketSnapshot


def volume_reconciliation(snapshots: list[MarketSnapshot], max_interval_seconds: int = 420) -> dict:
    """Check telescoping cumulative deltas only within uninterrupted valid segments."""
    observations = defaultdict(list)
    ordered_snapshots = sorted(snapshots, key=lambda item: item.timestamp_ist)
    position = {id(snapshot): index for index, snapshot in enumerate(ordered_snapshots)}
    for snapshot in ordered_snapshots:
        for contract in snapshot.options:
            if contract.instrument_token:
                key = (session_id(snapshot.timestamp_ist), contract.exchange,
                       contract.instrument_token, contract.expiry, contract.strike,
                       contract.option_type.value)
                observations[key].append((snapshot, contract))
    checked = discrepancies = breaks = 0
    for points in observations.values():
        baseline = None
        total = 0
        prior_snapshot = None
        for snapshot, contract in points:
            continuous = (prior_snapshot is not None and
                          position[id(snapshot)] == position[id(prior_snapshot)] + 1)
            interval = interval_volume(contract, prior_snapshot if continuous else None,
                                       snapshot, max_interval_seconds)
            if interval.value is not None and baseline is not None:
                total += interval.value
                expected = contract.volume - baseline
                checked += 1
                if expected != total:
                    discrepancies += 1
            else:
                if prior_snapshot is not None:
                    breaks += 1
                baseline = contract.volume
                total = 0
            prior_snapshot = snapshot
    return {"checked_intervals": checked, "discrepancies": discrepancies,
            "classified_segment_breaks": breaks}


def prefix_replay_equivalent(snapshot_id: int, current: MarketSnapshot,
                             all_snapshots: list[MarketSnapshot],
                             config: AlphaConfig = AlphaConfig()) -> bool:
    prior = [item for item in all_snapshots if item.timestamp_ist < current.timestamp_ist]
    first = build_alpha_features(snapshot_id, current, prior, [], config)
    second = build_alpha_features(snapshot_id, current, all_snapshots, [], config)
    exclude = {"feature_calculated_at"}
    return first.model_dump(exclude=exclude) == second.model_dump(exclude=exclude)


def validate_session(snapshots: list[MarketSnapshot],
                     alphas: list[AlphaFeatureSnapshot], expected_interval_seconds: int = 180,
                     collector_start: time = time(9, 18),
                     collector_end: time = time(15, 27)) -> dict:
    if not snapshots:
        return {"status": "NO_SNAPSHOTS"}
    ordered = sorted(snapshots, key=lambda item: item.timestamp_ist)
    ids = {session_id(item.timestamp_ist) for item in ordered}
    if len(ids) != 1:
        raise ValueError("validate one exchange session at a time")
    times = [item.timestamp_ist for item in ordered]
    duplicate = len(times) - len(set(times))
    gaps = [(right - left).total_seconds() for left, right in zip(times, times[1:])]
    day = ordered[0].timestamp_ist.astimezone(IST).date()
    first_slot = datetime.combine(day, collector_start, IST)
    final_slot = datetime.combine(day, collector_end, IST)
    expected_slots = set()
    cursor = first_slot
    while cursor <= final_slot:
        expected_slots.add(cursor)
        cursor += timedelta(seconds=expected_interval_seconds)
    observed_slots = []
    for timestamp in times:
        elapsed = (timestamp.astimezone(IST) - first_slot).total_seconds()
        slot = first_slot + timedelta(seconds=round(elapsed / expected_interval_seconds) * expected_interval_seconds)
        if slot in expected_slots and abs((timestamp - slot).total_seconds()) <= expected_interval_seconds / 2:
            observed_slots.append(slot)
    missing = len(expected_slots - set(observed_slots))
    source_receipt = [
        (item.response_received_at - item.source_market_timestamp).total_seconds()
        for item in ordered if item.response_received_at is not None and item.source_market_timestamp is not None]
    atm_changes = incomplete_atm = token_changes = atm_token_changes = 0
    previous_tokens = None
    previous_contracts = None
    missing_option_tokens = 0
    states = Counter()
    broker_local_oi_mismatch = local_oi_observations = 0
    for index, snapshot in enumerate(ordered):
        contracts = {(item.exchange, item.expiry, item.strike, item.option_type.value): item.instrument_token
                     for item in snapshot.options}
        missing_option_tokens += sum(value is None for value in contracts.values())
        if previous_contracts is not None:
            token_changes += sum(value != previous_contracts[key]
                                 for key, value in contracts.items() if key in previous_contracts)
        previous_contracts = contracts
        atm = select_atm(snapshot, snapshot.nifty_spot)
        if atm is None:
            incomplete_atm += 1
            continue
        tokens = (atm.call.instrument_token, atm.put.instrument_token)
        if previous_tokens is not None and tokens != previous_tokens:
            atm_changes += 1
            atm_token_changes += sum(a != b for a, b in zip(previous_tokens, tokens))
        previous_tokens = tokens
        previous = ordered[index-1] if index else None
        for contract in (atm.call, atm.put):
            interval = interval_volume(contract, previous, snapshot, expected_interval_seconds * 2 + 60)
            states[interval.state.value] += 1
            if previous is not None and contract.open_interest is not None:
                prior = next((item for item in previous.options
                              if item.instrument_token == contract.instrument_token
                              and item.expiry == contract.expiry and item.strike == contract.strike
                              and item.option_type == contract.option_type), None)
                if prior is not None and prior.open_interest is not None:
                    local_oi_observations += 1
                    if contract.change_in_open_interest is not None and (
                            contract.open_interest - prior.open_interest != contract.change_in_open_interest):
                        broker_local_oi_mismatch += 1
    same_alphas = [item for item in alphas if item.session_id == next(iter(ids))]
    horizon = Counter(item.actual_horizon_seconds for item in same_alphas
                      if item.actual_horizon_seconds is not None)
    first_rank1 = next((item.timestamp.isoformat() for item in same_alphas if item.alpha_1 is not None), None)
    first_rank2 = next((item.timestamp.isoformat() for item in same_alphas if item.alpha_2 is not None), None)
    return {
        "session_id": next(iter(ids)), "snapshot_count": len(ordered),
        "missing_snapshots_estimate": missing, "duplicate_timestamps": duplicate,
        "expected_collection_slots": len(expected_slots),
        "duplicate_collection_slots": len(observed_slots) - len(set(observed_slots)),
        "observation_gap_seconds": {"min": min(gaps) if gaps else None,
                                    "max": max(gaps) if gaps else None,
                                    "overlong_count": sum(value > expected_interval_seconds * 1.5 for value in gaps)},
        "source_receipt_gap_seconds": {"known_count": len(source_receipt),
                                       "min": min(source_receipt) if source_receipt else None,
                                       "max": max(source_receipt) if source_receipt else None},
        "option_token_changes": token_changes, "atm_changes": atm_changes,
        "atm_token_changes": atm_token_changes, "missing_option_tokens": missing_option_tokens,
        "atm_incomplete": incomplete_atm,
        "cumulative_volume_baselines": states[VolumeState.SESSION_BASELINE_ONLY.value],
        "invalid_volume_intervals": sum(count for state, count in states.items()
                                        if state not in {VolumeState.VALID_INTERVAL.value,
                                                         VolumeState.VALID_ZERO.value}),
        "negative_resets": states[VolumeState.RESET.value],
        "gap_intervals": states[VolumeState.GAP_VOLUME_INTERVAL.value],
        "volume_states": dict(states), "oi_local_observations": local_oi_observations,
        "oi_broker_vs_local_mismatch": broker_local_oi_mismatch,
        "actual_horizon_distribution_seconds": dict(sorted(horizon.items())),
        "alpha1_history_count_max": max((item.rank_observations_alpha1 for item in same_alphas), default=0),
        "alpha2_history_count_max": max((item.rank_observations_alpha2 for item in same_alphas), default=0),
        "first_alpha1_rank_at": first_rank1, "first_alpha2_rank_at": first_rank2,
        "volatility_floor_rejections": sum("VOLATILITY_TOO_SMALL" in item.warnings for item in same_alphas),
        "stale_data_rejections": sum("STALE_REFERENCE_PRICE" in item.warnings for item in same_alphas),
        "signal_persistence_resets": Counter(item.confirmation_reset_reason for item in same_alphas
                                              if item.confirmation_reset_reason),
        "extreme_alpha_diagnostics": {
            "alpha1_lower_1pct": sum(item.alpha_1 is not None and item.alpha_1 <= .01 for item in same_alphas),
            "alpha1_upper_1pct": sum(item.alpha_1 is not None and item.alpha_1 >= .99 for item in same_alphas),
            "alpha2_lower_1pct": sum(item.alpha_2 is not None and item.alpha_2 <= .01 for item in same_alphas),
            "alpha2_upper_1pct": sum(item.alpha_2 is not None and item.alpha_2 >= .99 for item in same_alphas),
            "rank_sign_conflicts": sum("RANK_SIGN_CONFLICT" in item.warnings for item in same_alphas),
        },
        "volume_reconciliation": volume_reconciliation(ordered),
    }
