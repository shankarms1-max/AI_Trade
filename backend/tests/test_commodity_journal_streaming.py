"""Synthetic integrity and bounded-memory regressions; no broker or production DB."""

import gc
import json
from datetime import timedelta
import os
import tracemalloc

import pytest
from sqlalchemy import create_engine, delete, event, func, insert, select, update

from app.commodity_acts.config import INSTRUMENTS, VERSION
from app.commodity_acts.models import (
    CommodityActsCursor, CommodityActsDecision, CommodityActsEvent,
    CommodityMarketSnapshotRecord, CommodityOptionQuoteRecord,
)
from app.commodity_acts.repository import capture_key, verify_journal
from app.db.base import Base
from app.research.manifest import canonical, digest
from tests.conftest import engine as sqlite_engine  # noqa: F401 -- pytest fixture dependency
from tests.test_commodity_acts import AT, captured
from tests.test_commodity_paper_postgresql import pg_admin, pg_target  # noqa: F401 -- pytest fixture dependencies


@pytest.fixture
def engine(request):
    if not os.getenv("COMMODITY_POSTGRES_TEST_URL"):
        yield request.getfixturevalue("sqlite_engine")
        return
    # Reuse the existing loopback/name/owner/version safeguards and isolated
    # schema. create_all deliberately omits triggers so tampering can be tested.
    target = request.getfixturevalue("pg_target")
    value = create_engine(target)
    Base.metadata.create_all(value)
    try:
        yield value
    finally:
        value.dispose()


def seed_journal(sessions, count=4, instrument="CRUDEOILM", *, first=1, evidence_size=0):
    """Write in bounded batches; large per-decision evidence makes retention visible."""
    template = captured(instrument)
    template = template.model_copy(update={"options": template.options[:2]})
    with sessions.begin() as session:
        previous = session.scalar(select(CommodityActsCursor.head_hash).where(
            CommodityActsCursor.instrument_id == instrument)) or "GENESIS"
        for sequence in range(first, first + count):
            at = AT + timedelta(seconds=15 * sequence)
            snapshot = template.model_copy(update={"timestamp_ist": at, "snapshot_persisted_at": at})
            sid = sequence + (100000 if instrument == "NATGASMINI" else 0)
            latest = {"snapshot_id": sid, "instrument_id": instrument, "evidence": "x" * evidence_size}
            state = {"policy_hash": "SYNTHETIC_UNCHANGED_POLICY", "last_snapshot": sid}
            payload = {"version": VERSION, "instrument_id": instrument, "latest": latest, "state": state,
                       "snapshot_id": sid, "snapshot_hash": digest(snapshot)}
            values = {"instrument_id": instrument, "sequence": sequence, "event_key": str(sequence),
                      "event_type": "SHADOW_DECISION", "previous_hash": previous, "payload": payload}
            hashed = digest(values)
            session.execute(insert(CommodityMarketSnapshotRecord), [{
                "id": sid, "instrument_id": instrument, "captured_at": at,
                "capture_key": capture_key(snapshot), "snapshot_json": canonical(snapshot)}])
            session.execute(insert(CommodityOptionQuoteRecord), [{
                "instrument_id": instrument, "snapshot_id": sid,
                "identity_key": digest({"instrument_id": instrument, "identity": quote.identity}),
                "quote_json": canonical(quote)} for quote in snapshot.options])
            session.execute(insert(CommodityActsDecision), [{
                "instrument_id": instrument, "snapshot_id": sid, "decided_at": at,
                "document": canonical(latest)}])
            session.execute(insert(CommodityActsEvent), [
                {**values, "payload": canonical(payload), "event_hash": hashed}])
            previous = hashed
        cursor = {"sequence": sequence, "head_hash": previous, "latest_json": canonical(latest),
                  "state_json": canonical(state)}
        if first == 1:
            session.execute(insert(CommodityActsCursor), [{"instrument_id": instrument, **cursor}])
        else:
            session.execute(update(CommodityActsCursor).where(
                CommodityActsCursor.instrument_id == instrument).values(**cursor))


def rehash(sessions, instrument="CRUDEOILM"):
    """Re-seal synthetic tampering to exercise checks beyond the event hash."""
    with sessions.begin() as session:
        previous = "GENESIS"
        for row in session.execute(select(CommodityActsEvent.__table__).where(
                CommodityActsEvent.instrument_id == instrument).order_by(CommodityActsEvent.sequence)):
            payload = json.loads(row.payload)
            hashed = digest({"instrument_id": instrument, "sequence": row.sequence, "event_key": row.event_key,
                             "event_type": row.event_type, "previous_hash": previous, "payload": payload})
            session.execute(update(CommodityActsEvent).where(
                CommodityActsEvent.instrument_id == instrument, CommodityActsEvent.sequence == row.sequence
            ).values(previous_hash=previous, event_hash=hashed))
            previous = hashed
        session.execute(update(CommodityActsCursor).where(CommodityActsCursor.instrument_id == instrument).values(
            head_hash=previous, latest_json=canonical(payload["latest"]), state_json=canonical(payload["state"])))


@pytest.mark.parametrize("instrument", INSTRUMENTS)
def test_valid_journal_is_readonly_streamed_and_does_not_hydrate_orm(session_factory, instrument):
    seed_journal(session_factory, instrument=instrument)
    seen_stream = []

    def no_orm(*_):
        raise AssertionError("Verifier must use column projections, not ORM histories")

    def inspect_statement(conn, statement, multiparams, params, execution_options):
        assert statement.is_select
        if "commodity_acts_events.sequence" in str(statement):
            assert execution_options.get("yield_per") == 1
            seen_stream.append(True)

    cls = session_factory.class_
    event.listen(cls, "loaded_as_persistent", no_orm)
    event.listen(cls, "before_flush", no_orm)
    engine = session_factory.kw["bind"]
    event.listen(engine, "before_execute", inspect_statement)
    try:
        verify_journal(session_factory, instrument)
        assert seen_stream
    finally:
        event.remove(cls, "loaded_as_persistent", no_orm)
        event.remove(cls, "before_flush", no_orm)
        event.remove(engine, "before_execute", inspect_statement)


@pytest.mark.parametrize("column,value", [
    ("event_hash", "broken"), ("previous_hash", "broken"), ("sequence", 99),
])
def test_sequence_and_hash_corruption_fails(session_factory, column, value):
    seed_journal(session_factory)
    with session_factory.begin() as session:
        session.execute(update(CommodityActsEvent).where(CommodityActsEvent.sequence == 2).values({column: value}))
    with pytest.raises(RuntimeError, match="COMMODITY_JOURNAL_HASH_INVALID"):
        verify_journal(session_factory, "CRUDEOILM")


@pytest.mark.parametrize("field,value,reason", [
    ("instrument_id", "NATGASMINI", "JOURNAL_HASH"),
    ("snapshot_id", 999, "JOURNAL_SNAPSHOT"),
    ("snapshot_id", 100001, "JOURNAL_SNAPSHOT"),
    ("snapshot_hash", "broken", "CAPTURE_HASH"),
])
def test_payload_ownership_and_evidence_fail_even_after_rehash(session_factory, field, value, reason):
    seed_journal(session_factory)
    seed_journal(session_factory, count=1, instrument="NATGASMINI")
    with session_factory.begin() as session:
        payload = json.loads(session.scalar(select(CommodityActsEvent.payload).where(
            CommodityActsEvent.instrument_id == "CRUDEOILM", CommodityActsEvent.sequence == 2)))
        payload[field] = value
        session.execute(update(CommodityActsEvent).where(
            CommodityActsEvent.instrument_id == "CRUDEOILM", CommodityActsEvent.sequence == 2
        ).values(payload=canonical(payload)))
    rehash(session_factory)
    with pytest.raises(RuntimeError, match=f"COMMODITY_{reason}_INVALID"):
        verify_journal(session_factory, "CRUDEOILM")


@pytest.mark.parametrize("corruption,reason", [
    ("capture_key", "CAPTURE_HASH"), ("snapshot_json", "CAPTURE_HASH"),
    ("quote_json", "CAPTURE_QUOTES"), ("identity_key", "CAPTURE_QUOTE_IDENTITY"),
    ("missing_quote", "CAPTURE_QUOTES"), ("extra_quote", "CAPTURE_QUOTES"),
])
def test_snapshot_and_quote_corruption_fails(session_factory, corruption, reason):
    seed_journal(session_factory)
    with session_factory.begin() as session:
        if corruption == "capture_key":
            session.execute(update(CommodityMarketSnapshotRecord).where(
                CommodityMarketSnapshotRecord.id == 2).values(capture_key="broken"))
        elif corruption == "snapshot_json":
            data = json.loads(session.scalar(select(CommodityMarketSnapshotRecord.snapshot_json).where(
                CommodityMarketSnapshotRecord.id == 2)))
            data["future_price"] = data["reference_price"] = 6000
            session.execute(update(CommodityMarketSnapshotRecord).where(
                CommodityMarketSnapshotRecord.id == 2).values(snapshot_json=canonical(data)))
        elif corruption == "extra_quote":
            session.execute(insert(CommodityOptionQuoteRecord), [{"instrument_id": "CRUDEOILM", "snapshot_id": 2,
                            "identity_key": "extra", "quote_json": "{}"}])
        else:
            qid = session.scalar(select(CommodityOptionQuoteRecord.id).where(
                CommodityOptionQuoteRecord.snapshot_id == 2))
            where = CommodityOptionQuoteRecord.id == qid
            if corruption == "missing_quote":
                session.execute(delete(CommodityOptionQuoteRecord).where(where))
            else:
                session.execute(update(CommodityOptionQuoteRecord).where(where).values({corruption: "{}"}))
    with pytest.raises(RuntimeError, match=f"COMMODITY_{reason}_INVALID"):
        verify_journal(session_factory, "CRUDEOILM")


@pytest.mark.parametrize("corruption", ["mismatch", "missing", "extra", "duplicate_reference_extra"])
def test_decision_projection_integrity(session_factory, corruption):
    seed_journal(session_factory)
    with session_factory.begin() as session:
        where = CommodityActsDecision.snapshot_id == 2
        if corruption == "mismatch":
            session.execute(update(CommodityActsDecision).where(where).values(document="{}"))
        elif corruption == "missing":
            session.execute(delete(CommodityActsDecision).where(where))
        else:
            # An unprocessed capture may exist, but an extra decision projection may not.
            session.execute(delete(CommodityActsEvent).where(CommodityActsEvent.sequence == 4))
            session.execute(update(CommodityActsCursor).values(sequence=3))
            if corruption == "duplicate_reference_extra":
                payload = json.loads(session.scalar(select(CommodityActsEvent.payload).where(
                    CommodityActsEvent.sequence == 2)))
                session.execute(update(CommodityActsEvent).where(CommodityActsEvent.sequence == 3).values(
                    payload=canonical(payload)))
    if corruption in {"extra", "duplicate_reference_extra"}:
        rehash(session_factory)
    with pytest.raises(RuntimeError, match="COMMODITY_JOURNAL_PROJECTION_INVALID"):
        verify_journal(session_factory, "CRUDEOILM")


@pytest.mark.parametrize("column,value", [
    ("sequence", 99), ("head_hash", "broken"), ("latest_json", "{}"), ("state_json", "{}"),
])
def test_cursor_projection_mismatch_fails(session_factory, column, value):
    seed_journal(session_factory)
    with session_factory.begin() as session:
        session.execute(update(CommodityActsCursor).values({column: value}))
    with pytest.raises(RuntimeError, match="COMMODITY_JOURNAL_PROJECTION_INVALID"):
        verify_journal(session_factory, "CRUDEOILM")


def test_empty_and_failure_only_journals(session_factory):
    from app.commodity_acts.repository import append_event

    with pytest.raises(RuntimeError, match="COMMODITY_MIGRATION_REQUIRED"):
        verify_journal(session_factory, "CRUDEOILM")
    with session_factory.begin() as session:
        session.add(CommodityActsCursor(instrument_id="CRUDEOILM"))
    verify_journal(session_factory, "CRUDEOILM")
    with session_factory.begin() as session:
        cursor = session.get(CommodityActsCursor, "CRUDEOILM")
        append_event(session, cursor, "failure", "CAPTURE_FAILED", {"error": "SYNTHETIC_FAILURE"}, {})
    verify_journal(session_factory, "CRUDEOILM")


def test_large_journal_memory_does_not_grow_with_decision_history(session_factory):
    def peak():
        queries = 0

        def collect_transients(*_):
            nonlocal queries
            queries += 1
            # Measure reachable history, not version-dependent cyclic-GC delays
            # in DB-driver result contexts. A retained decisions dict survives GC.
            if queries % 48 == 0:
                gc.collect()

        gc.collect()
        engine = session_factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", collect_transients)
        tracemalloc.start()
        try:
            verify_journal(session_factory, "CRUDEOILM")
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            event.remove(engine, "before_cursor_execute", collect_transients)

    seed_journal(session_factory, count=64, evidence_size=32768)
    # Warm statement caches before comparing retained history at 64 vs 1,024 decisions.
    verify_journal(session_factory, "CRUDEOILM")
    small = peak()
    seed_journal(session_factory, count=960, first=65, evidence_size=32768)
    large = peak()
    assert large < 4 * 1024 * 1024, {"small_peak": small, "large_peak": large}
    assert large < small + 1024 * 1024, {"small_peak": small, "large_peak": large}
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CommodityActsDecision)) == 1024
    print(f"COMMODITY_VERIFY_MEMORY small_decisions=64 large_decisions=1024 "
          f"small_peak_bytes={small} large_peak_bytes={large}")
