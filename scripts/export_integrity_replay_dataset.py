"""Export stored raw/feature/alpha rows for the isolated replay, without writes to SQL."""

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from sqlalchemy import and_, inspect, or_, select
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data.models import MarketSnapshot, OptionContractSnapshot  # noqa: E402
from app.db.models import (  # noqa: E402
    AlphaFeatureSnapshotRecord,
    MarketFeatureSnapshotRecord,
    MarketSnapshotRecord,
    OptionContractSnapshotRecord,
)
from app.db.session import get_session_factory  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
BATCH_SIZE = 100


class DuplicateRelatedRowsError(ValueError):
    def __init__(self, kind: str, snapshot_id: int):
        self.kind = kind
        self.snapshot_id = snapshot_id
        super().__init__(f"duplicate {kind} rows for snapshot_id={snapshot_id}")


def _json_default(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite stored decimal cannot be exported as JSON")
        # A decimal string preserves the database's precision; the replay loader
        # accepts it as the corresponding numeric model field.
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"unsupported stored value type: {type(value).__name__}")


def _stored_payload(record, model, *, aliases=None, omitted=frozenset()):
    aliases = aliases or {}
    return {
        name: getattr(record, aliases.get(name, name))
        for name in model.model_fields
        if name != "options" and name not in omitted
    }


def _market_payload(record: MarketSnapshotRecord, *, omitted_option_fields=frozenset()):
    payload = _stored_payload(
        record, MarketSnapshot, aliases={"snapshot_persisted_at": "created_at"}
    )
    payload["options"] = [
        _stored_payload(option, OptionContractSnapshot, omitted=omitted_option_fields)
        for option in sorted(record.options, key=lambda item: item.id)
    ]
    return payload


def _unique_related(session, model, ids, kind):
    result = {}
    for record in session.scalars(
        select(model).where(model.market_snapshot_id.in_(ids)).order_by(model.market_snapshot_id, model.id)
    ):
        if record.market_snapshot_id in result:
            raise DuplicateRelatedRowsError(kind, record.market_snapshot_id)
        result[record.market_snapshot_id] = record
    return result


def export_dataset(session, start_date: date, end_date: date, output: str | Path):
    """Stream deterministic JSON from SELECTs and publish only a complete file."""
    if start_date > end_date:
        raise ValueError("start date must not be after end date")
    if end_date == date.max:
        raise ValueError("end date is outside the supported exclusive-bound range")
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise FileExistsError("output file already exists")
    if not target.parent.is_dir():
        raise FileNotFoundError("output directory does not exist")

    lower = datetime.combine(start_date, time.min, IST)
    upper = datetime.combine(end_date + timedelta(days=1), time.min, IST)
    stored_option_columns = {column["name"] for column in
                             inspect(session.get_bind()).get_columns("option_contract_snapshots")}
    optional_option_fields = frozenset({"depth_unit", "tick_size"})
    omitted_option_fields = optional_option_fields - stored_option_columns
    option_loader = selectinload(MarketSnapshotRecord.options)
    for name in sorted(omitted_option_fields):
        option_loader = option_loader.defer(getattr(OptionContractSnapshotRecord, name))
    counts = {"snapshots_exported": 0, "snapshots_with_features": 0,
              "snapshots_missing_features": 0, "snapshots_with_alpha": 0,
              "snapshots_missing_alpha": 0}
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("[")
            cursor = None
            while True:
                conditions = [MarketSnapshotRecord.timestamp_ist >= lower,
                              MarketSnapshotRecord.timestamp_ist < upper]
                if cursor is not None:
                    timestamp, snapshot_id = cursor
                    conditions.append(or_(MarketSnapshotRecord.timestamp_ist > timestamp,
                                          and_(MarketSnapshotRecord.timestamp_ist == timestamp,
                                               MarketSnapshotRecord.id > snapshot_id)))
                records = session.scalars(
                    select(MarketSnapshotRecord)
                    .options(option_loader)
                    .where(*conditions)
                    .order_by(MarketSnapshotRecord.timestamp_ist, MarketSnapshotRecord.id)
                    .limit(BATCH_SIZE)
                ).all()
                if not records:
                    break
                ids = [record.id for record in records]
                features = _unique_related(session, MarketFeatureSnapshotRecord, ids, "feature")
                alphas = _unique_related(session, AlphaFeatureSnapshotRecord, ids, "alpha")
                for record in records:
                    feature = features.get(record.id)
                    alpha = alphas.get(record.id)
                    item = {"snapshot_id": record.id,
                            "snapshot": _market_payload(record, omitted_option_fields=omitted_option_fields),
                            "feature_id": None if feature is None else feature.id,
                            "feature": None if feature is None else feature.feature_json,
                            "alpha": None if alpha is None else alpha.result_json}
                    if counts["snapshots_exported"]:
                        stream.write(",")
                    stream.write("\n")
                    stream.write(json.dumps(item, sort_keys=True, ensure_ascii=False,
                                            allow_nan=False, separators=(",", ":"), default=_json_default))
                    counts["snapshots_exported"] += 1
                    counts["snapshots_with_features" if feature else "snapshots_missing_features"] += 1
                    counts["snapshots_with_alpha" if alpha else "snapshots_missing_alpha"] += 1
                cursor = records[-1].timestamp_ist, records[-1].id
                session.expunge_all()
            if counts["snapshots_exported"]:
                stream.write("\n")
            stream.write("]\n")
            stream.flush()
            os.fsync(stream.fileno())
        # The temporary file is already complete. A same-directory hard link
        # publishes it atomically and refuses to overwrite an existing target.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            **counts, "output": str(target)}


def _date_argument(value):
    try:
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv=None, *, session_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=_date_argument)
    parser.add_argument("--end-date", required=True, type=_date_argument)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        factory = session_factory or get_session_factory()
        with factory() as session:
            summary = export_dataset(session, args.start_date, args.end_date, args.output)
    except DuplicateRelatedRowsError as exc:
        print(f"Export aborted: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Driver exceptions may contain connection strings or server messages.
        print(f"Export failed ({type(exc).__name__}); no dataset was published.", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
