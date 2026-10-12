"""Read-only, causal Phase 14 Alpha evidence for the offline strategy lab."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from app.alpha.engine import joint_direction
from app.alpha.models import (ALPHA_VERSION, AlphaFeatureSnapshot, AlphaHypothesis,
                              AlphaValidity, CalculationMode)
from app.db.models import AlphaFeatureSnapshotRecord
from app.scalper_lab import acts
from app.scalper_lab.context import ResearchObservation

MAX_AGE = timedelta(seconds=int(acts.PARAMETERS["alpha_max_age_seconds"]))


@dataclass(frozen=True)
class _AlphaRow:
    id: int
    timestamp: datetime
    alpha: AlphaFeatureSnapshot | None
    reason: str | None


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def _parse(record: Any) -> _AlphaRow:
    payload = record.result_json
    if (isinstance(payload, dict) and "alpha_version" in payload
            and any(not payload.get(field) for field in (
                "reference_instrument_id", "reference_expiry", "expiry"))):
        return _AlphaRow(record.id, record.timestamp, None,
                         "ALPHA_IDENTITY_INCOMPLETE")
    try:
        alpha = AlphaFeatureSnapshot.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        return _AlphaRow(record.id, record.timestamp, None, "INVALID_ALPHA")
    reason = None
    if (not _aware(record.timestamp) or not _aware(alpha.timestamp)
            or alpha.timestamp != record.timestamp or alpha.expiry != record.expiry
            or alpha.alpha_version != record.alpha_version
            or alpha.calculation_mode.value != record.calculation_mode
            or alpha.price_source != record.price_source):
        reason = "INVALID_ALPHA"
    elif (record.alpha_version != ALPHA_VERSION
          or record.calculation_mode != CalculationMode.LIVE_ORIGINAL.value
          or record.price_source != "FUTURE"):
        reason = "INVALID_ALPHA"
    elif (not alpha.reference_instrument_id or alpha.reference_expiry is None
          or alpha.expiry is None):
        reason = "ALPHA_IDENTITY_INCOMPLETE"
    elif (alpha.hypothesis_type != AlphaHypothesis.CONTINUATION
          or alpha.validity_state != AlphaValidity.VALID
          or alpha.alpha_1 is None or alpha.alpha_2 is None
          or joint_direction(alpha.alpha_1_direction, alpha.alpha_2_direction)
          != alpha.joint_alpha_direction
          or not _aware(alpha.feature_calculated_at)
          or any(value is not None and not _aware(value) for value in (
              alpha.response_received_at, alpha.source_market_timestamp,
              alpha.reference_source_timestamp))):
        reason = "INVALID_ALPHA"
    return _AlphaRow(record.id, record.timestamp, alpha, reason)


def _reason(row: _AlphaRow, observation: ResearchObservation) -> str | None:
    alpha = row.alpha
    at = observation.snapshot.captured_at
    if row.reason is not None:
        return row.reason
    assert alpha is not None
    snapshot = observation.snapshot
    if not snapshot.future_instrument_id or snapshot.future_expiry is None:
        return "ALPHA_IDENTITY_INCOMPLETE"
    if alpha.reference_instrument_id != snapshot.future_instrument_id:
        return "FUTURES_IDENTITY_MISMATCH"
    if alpha.reference_expiry != snapshot.future_expiry:
        return "FUTURES_EXPIRY_MISMATCH"
    if alpha.expiry != snapshot.expiry:
        return "OPTION_EXPIRY_MISMATCH"
    if any(value is not None and value > at for value in (
            alpha.feature_calculated_at, alpha.response_received_at,
            alpha.source_market_timestamp, alpha.reference_source_timestamp)):
        return "FUTURE_ALPHA_REJECTED"
    evidence = acts.alpha_evidence(alpha, at, snapshot.future_instrument_id,
                                   snapshot.future_expiry, snapshot.expiry)
    return None if evidence["status"] == "AVAILABLE" else "INVALID_ALPHA"


def _read_rows(connection: Connection, lower: datetime,
               upper: datetime) -> list[_AlphaRow]:
    """One indexed range read, with no cross-database join or ORM write session."""
    records = connection.execute(select(
        AlphaFeatureSnapshotRecord.id, AlphaFeatureSnapshotRecord.timestamp,
        AlphaFeatureSnapshotRecord.expiry, AlphaFeatureSnapshotRecord.alpha_version,
        AlphaFeatureSnapshotRecord.calculation_mode,
        AlphaFeatureSnapshotRecord.price_source,
        AlphaFeatureSnapshotRecord.result_json,
    ).where(AlphaFeatureSnapshotRecord.timestamp >= lower,
            AlphaFeatureSnapshotRecord.timestamp <= upper).order_by(
                AlphaFeatureSnapshotRecord.timestamp,
                AlphaFeatureSnapshotRecord.id)).all()
    return [_parse(record) for record in records]


def _match(observations: list[ResearchObservation], rows: list[_AlphaRow]
           ) -> tuple[list[ResearchObservation], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (row.timestamp, row.id))
    times = [row.timestamp for row in ordered]
    reasons: Counter[str] = Counter()
    matched = 0
    result = []
    for observation in observations:
        at = observation.snapshot.captured_at
        first = bisect_left(times, at - MAX_AGE)
        index = bisect_right(times, at)
        first_reason = None
        selected = None
        for position in range(index - 1, first - 1, -1):
            row = ordered[position]
            reason = _reason(row, observation)
            if reason is None:
                selected = row.alpha
                break
            if first_reason is None:
                first_reason = reason
        if selected is None:
            if first_reason is None and index and at - ordered[index - 1].timestamp > MAX_AGE:
                first_reason = "STALE_ALPHA"
            if first_reason is None and index < len(ordered):
                first_reason = "FUTURE_ALPHA_REJECTED"
            reasons[first_reason or "ALPHA_UNAVAILABLE"] += 1
        else:
            matched += 1
        result.append(replace(observation, alpha=selected))
    diagnostics: dict[str, Any] = {
        "alpha_source": "EXTERNAL_DB",
        "rejection_count_scope": "ONE_REASON_PER_UNMATCHED_OBSERVATION",
        "alpha_rows_loaded": len(ordered),
        "alpha_rows_valid": sum(row.reason is None for row in ordered),
        "matched_observations": matched,
        "unmatched_observations": len(observations) - matched,
        "stale_alpha_count": reasons["STALE_ALPHA"],
        "future_rejected_count": reasons["FUTURE_ALPHA_REJECTED"],
        "futures_identity_mismatch_count": reasons["FUTURES_IDENTITY_MISMATCH"],
        "futures_expiry_mismatch_count": reasons["FUTURES_EXPIRY_MISMATCH"],
        "option_expiry_mismatch_count": reasons["OPTION_EXPIRY_MISMATCH"],
        "invalid_alpha_count": reasons["INVALID_ALPHA"],
        "incomplete_identity_count": reasons["ALPHA_IDENTITY_INCOMPLETE"],
        "unmatched_reason_split": dict(sorted(reasons.items())),
    }
    return result, diagnostics


def load_external_alpha(observations: list[ResearchObservation],
                        database_url: str) -> tuple[list[ResearchObservation], dict[str, Any]]:
    """Use Phase 14 PostgreSQL only for Alpha; never reuse Phase 15 sessions."""
    try:
        if make_url(database_url).get_backend_name() != "postgresql":
            raise ValueError("LAB_ALPHA_POSTGRESQL_URL_REQUIRED")
    except (ValueError, SQLAlchemyError):
        raise ValueError("LAB_ALPHA_POSTGRESQL_URL_REQUIRED") from None
    if not observations:
        return _match([], [])
    lower = min(row.snapshot.captured_at for row in observations) - MAX_AGE
    upper = max(row.snapshot.captured_at for row in observations)
    try:
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    rows = _read_rows(connection, lower, upper)
        finally:
            engine.dispose()
    except SQLAlchemyError:
        raise RuntimeError("LAB_ALPHA_SOURCE_READ_FAILED") from None
    return _match(observations, rows)


def same_db_diagnostics(observations: list[ResearchObservation],
                        meta: dict[str, Any]) -> dict[str, Any]:
    """Describe the existing loader's selections without changing its decisions."""
    statuses = [acts.alpha_evidence(
        row.alpha, row.snapshot.captured_at, row.snapshot.future_instrument_id,
        row.snapshot.future_expiry, row.snapshot.expiry)["status"]
        for row in observations]
    matched = statuses.count("AVAILABLE")
    invalid_selected = sum(row.alpha is not None and status != "AVAILABLE"
                           for row, status in zip(observations, statuses))
    return {
        "alpha_source": meta.get("alpha_source", "NONE"),
        "rejection_count_scope": "SELECTED_ALPHA_ONLY",
        "alpha_rows_loaded": meta.get("alpha_rows_loaded", 0),
        "alpha_rows_valid": len({row.alpha.snapshot_id for row in observations
                                 if row.alpha is not None and acts.alpha_evidence(
                                     row.alpha, row.snapshot.captured_at,
                                     row.snapshot.future_instrument_id,
                                     row.snapshot.future_expiry,
                                     row.snapshot.expiry)["status"] == "AVAILABLE"}),
        "matched_observations": matched,
        "unmatched_observations": len(observations) - matched,
        "stale_alpha_count": 0,
        "future_rejected_count": 0,
        "futures_identity_mismatch_count": 0,
        "futures_expiry_mismatch_count": 0,
        "option_expiry_mismatch_count": 0,
        "invalid_alpha_count": invalid_selected,
        "incomplete_identity_count": 0,
        "unmatched_reason_split": dict(sorted(Counter(
            "INVALID_ALPHA" if row.alpha is not None else "ALPHA_UNAVAILABLE"
            for row, status in zip(observations, statuses)
            if status != "AVAILABLE").items())),
    }
