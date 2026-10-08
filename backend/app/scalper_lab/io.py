"""Explicit local, read-only inputs and new immutable research artifacts."""
from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from app.scalper.models import ScalperMarketSnapshotRecord
from app.scalper.repository import ScalperRepository
from app.scalper_lab.context import ResearchObservation
from app.scalper_lab.engine import LabResult

IST = ZoneInfo("Asia/Kolkata")
FUTURES_COLUMNS = {"timestamp", "source_timestamp", "symbol", "expiry",
                   "instrument_token", "ltp", "cumulative_volume", "vwap",
                   "basis", "source_fields"}


def load_futures_csv(path: Path | None) -> dict[str, dict[str, Any]]:
    """Companion broker export, keyed by decision capture time, never inferred."""
    if path is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not FUTURES_COLUMNS <= set(reader.fieldnames or []):
            raise ValueError("LAB_FUTURES_CSV_COLUMNS_MISSING")
        for row in reader:
            at = datetime.fromisoformat(row["timestamp"])
            if at.tzinfo is None:
                raise ValueError("LAB_FUTURES_TIMESTAMP_NOT_AWARE")
            key = at.astimezone(IST).isoformat()
            if key in result:
                raise ValueError("LAB_DUPLICATE_FUTURES_TIMESTAMP")
            try:
                volume = int(row["cumulative_volume"]) if row["cumulative_volume"] else None
                vwap = float(row["vwap"]) if row["vwap"] else None
                ltp = float(row["ltp"]) if row["ltp"] else None
                sources = json.loads(row["source_fields"])
            except (ValueError, TypeError) as exc:
                raise ValueError("LAB_FUTURES_CSV_VALUE_INVALID") from exc
            if not isinstance(sources, list) or any(not isinstance(x, str) for x in sources):
                raise ValueError("LAB_FUTURES_SOURCE_FIELDS_INVALID")
            result[key] = {"source_timestamp": row["source_timestamp"],
                           "symbol": row["symbol"], "expiry": row["expiry"],
                           "instrument_token": row["instrument_token"],
                           "ltp": ltp, "cumulative_volume": volume,
                           "vwap": vwap, "basis": row["basis"],
                           "source_fields": sources}
    return result


def load_local_sqlite(path: Path, start: date, end: date,
                      futures: dict[str, dict[str, Any]] | None = None) -> list[ResearchObservation]:
    """SQLite mode=ro ensures even an accidental INSERT cannot mutate evidence."""
    if end < start:
        raise ValueError("LAB_DATE_RANGE_INVALID")
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    lower = datetime.combine(start, time.min, tzinfo=IST)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST)
    try:
        with Session(engine) as session:
            records = session.scalars(select(ScalperMarketSnapshotRecord).options(
                selectinload(ScalperMarketSnapshotRecord.quotes)).where(
                    ScalperMarketSnapshotRecord.captured_at >= lower,
                    ScalperMarketSnapshotRecord.captured_at < upper).order_by(
                        ScalperMarketSnapshotRecord.captured_at,
                        ScalperMarketSnapshotRecord.id)).all()
            output = []
            for record in records:
                snapshot = ScalperRepository._row_to_snapshot(record)
                output.append(ResearchObservation(
                    record.id, snapshot,
                    (futures or {}).get(snapshot.captured_at.isoformat())))
            return output
    finally:
        engine.dispose()


def _jsonline(path: Path, rows: tuple[dict, ...]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")


def write_result(result: LabResult, output: Path) -> dict[str, Path]:
    """Write each context once; decisions link to that snapshot's full OI evidence."""
    output.mkdir(parents=True, exist_ok=False)
    paths = {name: output / name for name in (
        "manifest.json", "contexts.jsonl", "decisions.jsonl", "trades.jsonl",
        "summary.json", "comparison.csv", "comparison.md")}
    paths["manifest.json"].write_text(json.dumps(result.manifest, indent=2, sort_keys=True) + "\n")
    paths["summary.json"].write_text(
        json.dumps(result.summaries, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _jsonline(paths["contexts.jsonl"], result.contexts)
    _jsonline(paths["decisions.jsonl"], result.decisions)
    _jsonline(paths["trades.jsonl"], result.trades)
    fields = ("strategy_id", "observations", "setups", "qualified_entries",
              "executed_trades", "wins", "losses", "win_rate", "gross_pnl",
              "expectancy_per_trade", "profit_factor", "max_drawdown",
              "unresolved_trades", "accounting_status")
    with paths["comparison.csv"].open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.comparison)
    lines = ["| " + " | ".join(fields) + " |",
             "| " + " | ".join("---" for _ in fields) + " |"]
    for row in result.comparison:
        lines.append("| " + " | ".join(str(row.get(field, "")) for field in fields) + " |")
    paths["comparison.md"].write_text("\n".join(lines) + "\n")
    return paths
