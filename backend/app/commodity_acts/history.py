"""Stream original causal history while retaining exactly its strategy inputs.

Alpha already samples the first capture in each three-minute bucket. Retain
those samples for its unchanged dependency window, all fast-horizon rows, and
opening/session extrema. Original rows are validated before selection. No new
history-length rejection is introduced into position management.
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.alpha.clock import IST, lookback_window, using_session
from app.commodity_acts.domain import CommoditySnapshot
from app.commodity_acts.models import CommodityMarketSnapshotRecord


def _bucket(at):
    at = at.astimezone(IST)
    return at.replace(minute=at.minute // 3 * 3, second=0, microsecond=0)


def dependency_minutes(config):
    return (
        max(
            config.alpha1_lookback_minutes,
            config.alpha2_lookback_minutes + config.volatility_lookback_minutes,
            config.alpha2_lookback_minutes + config.volume_lookback_minutes,
            config.volume_lookback_minutes,
            config.volatility_lookback_minutes,
        )
        + config.max_horizon_seconds // 60
        + 2
    )


def select_history(descending, current, spec, alpha_config):
    firsts, recent, opening, extrema = {}, {}, {}, {}
    start = datetime.combine(current.timestamp_ist.astimezone(IST).date(), spec.market_session.start, IST)
    end = start + timedelta(minutes=15)
    identity = (current.future_token, current.future_instrument_id, current.future_expiry, current.expiry)

    def trim():
        with using_session(spec.market_session.clock(spec.instrument_id)):
            eligible = lookback_window(
                list(firsts.values()),
                current.timestamp_ist,
                dependency_minutes(alpha_config),
                alpha_config.lookback_clock_mode,
                holidays=alpha_config.holiday_dates,
            )
        needed = {_bucket(row.timestamp_ist) for row in eligible}
        # Keep each context anchor's TRUE first bucket capture, even if that
        # capture lies outside Alpha's window. Otherwise a later anchor could
        # accidentally replace the original Alpha sample at the window edge.
        needed.update(_bucket(row.timestamp_ist) for row in (*recent.values(), *opening.values(), *extrema.values()))
        for key in list(firsts):
            if key not in needed:
                del firsts[key]

    for row in descending:
        if row.timestamp_ist >= current.timestamp_ist:
            raise ValueError("COMMODITY_NONCAUSAL_HISTORY")
        if (
            row.instrument_id != current.instrument_id
            or row.snapshot_persisted_at is None
            or row.snapshot_persisted_at > current.timestamp_ist
            or (row.future_token, row.future_instrument_id, row.future_expiry, row.expiry) != identity
        ):
            continue
        # Descending source order means overwriting keeps the earliest capture.
        firsts[_bucket(row.timestamp_ist)] = row
        if row.session_id == current.session_id:
            if current.timestamp_ist - row.timestamp_ist <= timedelta(seconds=922.5):
                recent[row.timestamp_ist] = row
            for key, op in (("low", min), ("high", max)):
                prior = extrema.get(key)
                if prior is None or op(prior.reference_price, row.reference_price) == row.reference_price:
                    extrema[key] = row
            if start <= row.timestamp_ist < end:
                for key, op, field in (
                    ("first", min, "timestamp_ist"),
                    ("last", max, "timestamp_ist"),
                    ("low", min, "reference_price"),
                    ("high", max, "reference_price"),
                ):
                    prior = opening.get(key)
                    if prior is None or op(getattr(prior, field), getattr(row, field)) == getattr(row, field):
                        opening[key] = row
        if len(firsts) > 512:
            trim()
    trim()
    chosen = {
        row.timestamp_ist: row for row in (*firsts.values(), *recent.values(), *opening.values(), *extrema.values())
    }
    return [chosen[at] for at in sorted(chosen)]


def load_history(research, current):
    query = (
        select(CommodityMarketSnapshotRecord)
        .where(
            CommodityMarketSnapshotRecord.instrument_id == research.spec.instrument_id,
            CommodityMarketSnapshotRecord.captured_at < current.timestamp_ist,
        )
        .order_by(CommodityMarketSnapshotRecord.captured_at.desc())
        .limit(research.config.history_limit)
    )
    with research.sessions() as session:
        records = session.scalars(query.execution_options(yield_per=16))
        # Every source row receives the original complete domain validation;
        # malformed unused rows cannot silently start passing after this change.
        rows = (CommoditySnapshot.model_validate_json(record.snapshot_json) for record in records)
        return select_history(rows, current, research.spec, research.alpha_config)
