"""Offline, transactional import of provenance-labelled scalar futures history only."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterator

from sqlalchemy import Engine, MetaData, Table, create_engine, inspect, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.adaptive_supertrend import BOOTSTRAP_MODE, BOOTSTRAP_PURPOSE, CAPTURE_MODE
from app.research.manifest import canonical, digest
from app.scalper.models import ScalperMarketSnapshotRecord as Snapshot, ScalperOptionQuoteRecord as Quote
from app.scalper.repository import IST, _aware

MAX_ROWS = 100_000
MAX_SESSIONS = 128
BATCH_SIZE = 250
SOURCE_MODE = "ACTS_PAPER"
SCALAR_FIELDS = (
    "captured_at",
    "request_started_at",
    "response_received_at",
    "nifty_future",
    "future_instrument_id",
    "future_expiry",
    "nifty_spot",
    "atm_strike",
    "lot_size",
    "expiry",
)


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_provenance(context: dict) -> None:
    if (
        context.get("mode") != BOOTSTRAP_MODE
        or context.get("source_mode") != SOURCE_MODE
        or context.get("purpose") != BOOTSTRAP_PURPOSE
        or re.fullmatch(r"[0-9a-f]{64}", str(context.get("source_file_sha256", ""))) is None
        or context.get("original_capture_timestamp_preserved") is not True
        or context.get("original_request_response_timestamps_preserved") is not True
        or context.get("option_quotes_imported") is not False
    ):
        raise ValueError("AST_BOOTSTRAP_PROVENANCE_INVALID")


def capture_key(captured_at: datetime) -> str:
    # Full observation instant, NOT a 15s bucket or a file-dependent key. A new
    # source SHA for an overlapping observation must conflict, not duplicate it.
    return digest({"mode": BOOTSTRAP_MODE, "captured_at": captured_at.astimezone(IST).isoformat()})


def _timestamp(value: str) -> datetime:
    fraction = re.search(r"[T ]\d{2}:\d{2}:\d{2}\.(\d+)", value)
    if fraction and any(char != "0" for char in fraction[1][6:]):
        raise ValueError("AST_BOOTSTRAP_TIMESTAMP_PRECISION_UNSUPPORTED")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("AST_BOOTSTRAP_ORIGINAL_AWARE_TIMESTAMPS_REQUIRED")
    return parsed.astimezone(IST)


def _price(value) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number >= Decimal("100000000000000"):
            raise ValueError("AST_BOOTSTRAP_PRICE_INVALID")
        if number != number.quantize(Decimal("0.000001")):
            raise ValueError("AST_BOOTSTRAP_PRICE_PRECISION_UNSUPPORTED")
        return number
    except InvalidOperation as exc:
        raise ValueError("AST_BOOTSTRAP_PRICE_INVALID") from exc


def _records(path: Path, sha256: str, before: date) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    previous = None
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            raw = json.loads(line, parse_float=Decimal)
            if any(name not in raw or raw[name] is None for name in SCALAR_FIELDS):
                raise ValueError("AST_BOOTSTRAP_ORIGINAL_SNAPSHOT_SCALARS_REQUIRED_USE_RAW_JSONL_NOT_FOUR_COLUMN_CSV")
            # A raw model export can omit provenance. When present it must agree
            # with the explicit source declaration and audited archive identity.
            context = raw.get("context_json") or {}
            if isinstance(context, str):
                context = json.loads(context)
            if not isinstance(context, dict) or any(
                declared != SOURCE_MODE
                for declared in (raw.get("source_mode"), raw.get("mode"), context.get("mode"))
                if declared is not None
            ):
                raise ValueError("AST_BOOTSTRAP_CONFLICTING_SOURCE_MODE")
            at, requested, responded = (_timestamp(raw[name]) for name in SCALAR_FIELDS[:3])
            if not requested <= responded <= at or previous is not None and at <= previous:
                raise ValueError("AST_BOOTSTRAP_CAUSAL_STRICT_OBSERVATION_ORDER_REQUIRED")
            if at.date() >= before:
                raise ValueError("AST_BOOTSTRAP_STRICTLY_HISTORICAL_CUTOFF_REQUIRED")
            previous = at
            future_expiry, option_expiry = date.fromisoformat(raw["future_expiry"]), date.fromisoformat(raw["expiry"])
            identity, lot = raw["future_instrument_id"], raw["lot_size"]
            if (
                not isinstance(identity, str)
                or not identity.strip()
                or len(identity) > 160
                or future_expiry < at.date()
                or type(lot) is not int
                or lot <= 0
            ):
                raise ValueError("AST_BOOTSTRAP_EXACT_FUTURES_IDENTITY_AND_SOURCE_METADATA_REQUIRED")
            context = {
                "mode": BOOTSTRAP_MODE,
                "source_mode": SOURCE_MODE,
                "purpose": BOOTSTRAP_PURPOSE,
                "source_file_sha256": sha256,
                "original_capture_timestamp_preserved": True,
                "original_request_response_timestamps_preserved": True,
                "option_quotes_imported": False,
                "original_timestamps": {name: raw[name] for name in SCALAR_FIELDS[:3]},
            }
            yield {
                "capture_key": capture_key(at),
                "captured_at": at,
                "request_started_at": requested,
                "response_received_at": responded,
                "source_market_timestamp": None,
                "nifty_future": _price(raw["nifty_future"]),
                "future_instrument_id": identity,
                "future_expiry": future_expiry,
                # Existing NOT NULL columns are copied from real source scalars,
                # never filled with a made-up spot, strike, lot or option expiry.
                "nifty_spot": _price(raw["nifty_spot"]),
                "atm_strike": _price(raw["atm_strike"]),
                "lot_size": lot,
                "expiry": option_expiry,
                "india_vix": None,
                "source": BOOTSTRAP_MODE,
                "feature_json": "{}",
                "signal_json": "{}",
                "context_json": canonical(context),
            }


def _assert_isolated_target(connection) -> None:
    if connection.dialect.name not in {"postgresql", "sqlite"}:
        raise ValueError("AST_BOOTSTRAP_UNSUPPORTED_DATABASE")
    tables = set(inspect(connection).get_table_names())
    if (
        not {
            "ast_futures_paper_cursor",
            "ast_futures_paper_trades",
            "ast_futures_paper_events",
            Snapshot.__tablename__,
            Quote.__tablename__,
        }
        <= tables
    ):
        raise ValueError("AST_BOOTSTRAP_REQUIRES_EXISTING_ISOLATED_AST_SCHEMA_NO_MIGRATIONS_RUN")
    metadata = MetaData()
    cursor = Table("ast_futures_paper_cursor", metadata, autoload_with=connection)
    if connection.scalar(select(cursor.c.id).where(cursor.c.id == 1).with_for_update()) != 1:
        raise ValueError("AST_BOOTSTRAP_REQUIRES_INITIALIZED_AST_CURSOR")
    allowed = or_(
        Snapshot.context_json.like(f'%"mode":"{CAPTURE_MODE}"%'),
        Snapshot.context_json.like(f'%"mode":"{BOOTSTRAP_MODE}"%'),
    )
    if connection.scalar(select(Snapshot.id).where(~allowed | Snapshot.context_json.is_(None)).limit(1)) is not None:
        raise ValueError("AST_BOOTSTRAP_TARGET_HAS_FOREIGN_CAPTURE_DATA")
    for name in (
        "acts_paper_trades",
        "acts_paper_events",
        "parallel_paper_trades",
        "parallel_paper_events",
        "scalper_trades",
        "scalper_events",
    ):
        if name in tables:
            table = Table(name, metadata, autoload_with=connection)
            if connection.execute(select(table).limit(1)).first() is not None:
                raise ValueError("AST_BOOTSTRAP_TARGET_HAS_FOREIGN_TRADING_DATA")


def _assert_existing(existing, expected: dict) -> None:
    for key, value in expected.items():
        stored = existing[key]
        if isinstance(value, datetime):
            stored = _aware(stored)
        elif key == "context_json":
            stored, value = json.loads(stored), json.loads(value)
        if stored != value:
            raise ValueError("AST_BOOTSTRAP_SOURCE_OR_PROVENANCE_CONFLICT")


def import_history(
    engine: Engine, path: Path, *, source_file_sha256: str, source_mode: str, before_date: date
) -> dict:
    """All-or-nothing scalar inserts. Never initialize schema or write any journal."""
    sha = source_file_sha256.lower()
    if re.fullmatch(r"[0-9a-f]{64}", sha) is None or file_sha256(path) != sha:
        raise ValueError("AST_BOOTSTRAP_SOURCE_SHA256_MISMATCH")
    if source_mode != SOURCE_MODE:
        raise ValueError("AST_BOOTSTRAP_EXPLICIT_ACTS_PAPER_SOURCE_REQUIRED")
    read = inserted = 0
    dates: set[date] = set()
    checked_dates: set[date] = set()
    first = last = None
    with engine.begin() as connection:
        # Legacy sqlite3 transaction control does not begin a transaction for
        # SELECT. Reserve the writer before checking the per-day archive SHA.
        if connection.dialect.name == "sqlite":
            driver = connection.connection.driver_connection
            assert driver is not None
            if not driver.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        _assert_isolated_target(connection)

        def write(batch: list[dict]) -> int:
            for day in sorted({row["captured_at"].date() for row in batch} - checked_dates):
                start = datetime.combine(day, time.min, IST)
                contexts = connection.scalars(
                    select(Snapshot.context_json)
                    .where(
                        Snapshot.captured_at >= start,
                        Snapshot.captured_at < start + timedelta(days=1),
                        or_(
                            Snapshot.context_json.like(f'%"mode":"{BOOTSTRAP_MODE}"%'),
                            Snapshot.source == BOOTSTRAP_MODE,
                        ),
                    )
                    .execution_options(yield_per=BATCH_SIZE)
                )
                for encoded in contexts:
                    context = json.loads(encoded or "{}")
                    validate_provenance(context)
                    if context["source_file_sha256"] != sha:
                        raise ValueError("AST_BOOTSTRAP_SOURCE_OR_PROVENANCE_CONFLICT")
                checked_dates.add(day)
            table = Snapshot.metadata.tables[Snapshot.__tablename__]
            statement = pg_insert(table) if connection.dialect.name == "postgresql" else sqlite_insert(table)
            added = set(
                connection.scalars(
                    statement.values(batch)
                    .on_conflict_do_nothing(index_elements=[table.c.capture_key])
                    .returning(table.c.capture_key)
                )
            )
            stored = {
                row["capture_key"]: row
                for row in connection.execute(
                    select(table).where(table.c.capture_key.in_([r["capture_key"] for r in batch]))
                ).mappings()
            }
            for row in batch:
                _assert_existing(stored[row["capture_key"]], row)
            ids = [row["id"] for row in stored.values()]
            if connection.scalar(select(Quote.id).where(Quote.scalper_snapshot_id.in_(ids)).limit(1)) is not None:
                raise ValueError("AST_BOOTSTRAP_ROWS_MUST_NOT_HAVE_OPTION_QUOTES")
            return len(added)

        batch = []
        for row in _records(path, sha, before_date):
            read += 1
            dates.add(row["captured_at"].date())
            if read > MAX_ROWS or len(dates) > MAX_SESSIONS:
                raise ValueError("AST_BOOTSTRAP_IMPORT_BUDGET_EXCEEDED")
            first = first or row["captured_at"]
            last = row["captured_at"]
            batch.append(row)
            if len(batch) == BATCH_SIZE:
                inserted += write(batch)
                batch = []
        if batch:
            inserted += write(batch)
        if first is None or last is None:
            raise ValueError("AST_BOOTSTRAP_EMPTY_SOURCE")
        if file_sha256(path) != sha:
            raise ValueError("AST_BOOTSTRAP_SOURCE_CHANGED_DURING_IMPORT")
    return {
        "mode": BOOTSTRAP_MODE,
        "source_mode": SOURCE_MODE,
        "purpose": BOOTSTRAP_PURPOSE,
        "source_file_sha256": sha,
        "rows_read": read,
        "rows_inserted": inserted,
        "rows_already_present": read - inserted,
        "distinct_trading_dates": sorted(map(str, dates)),
        "distinct_trading_date_count": len(dates),
        "first_observation": first.isoformat(),
        "last_observation": last.isoformat(),
        "before_date": str(before_date),
        "option_quotes_inserted": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, required=True, help="original raw snapshot JSONL or JSONL.GZ")
    parser.add_argument("--source-file-sha256", required=True)
    parser.add_argument("--source-mode", choices=[SOURCE_MODE], required=True)
    parser.add_argument("--before-date", type=date.fromisoformat, required=True)
    parser.add_argument("--database-url", help="isolated AST DB; alternatively DATABASE_URL (no broker settings)")
    parser.add_argument("--confirm-isolated-ast-database", action="store_true", required=True)
    args = parser.parse_args(argv)
    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        parser.error("DATABASE_URL or --database-url is required for the isolated AST database")
    engine = create_engine(url)
    try:
        result = import_history(
            engine,
            args.snapshots,
            source_file_sha256=args.source_file_sha256,
            source_mode=args.source_mode,
            before_date=args.before_date,
        )
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    except SQLAlchemyError as exc:
        parser.error(f"AST_BOOTSTRAP_DATABASE_IMPORT_FAILED: {type(exc).__name__}")
    finally:
        engine.dispose()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
