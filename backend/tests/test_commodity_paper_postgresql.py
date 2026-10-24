"""Opt-in real PostgreSQL 16 validation. Never substitutes SQLite or DATABASE_URL.

COMMODITY_POSTGRES_TEST_URL must identify the dedicated localhost disposable DB
commodity_paper_validation, owned by commodity_validation. All market inputs are
the existing synthetic fixtures, not live MCX evidence.
"""

from datetime import datetime, timedelta
import os
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.commodity_acts.config import INSTRUMENTS
from app.commodity_acts.repository import verify_journal
from app.commodity_acts.service import CommodityResearch
from app.db.repositories import SnapshotRepository
from tests.test_commodity_acts import AT, captured, config, research_case
from tests.test_commodity_acts_migration import migration_config
from tests.test_scalper import settings

# Reuse exactly the already-passing strategy/execution assertions; only the
# database fixture changes. The schema comes from Alembic, never create_all().
from tests.test_commodity_paper import (
    case,
    test_real_actsv2_score_next_observation_atomic_fill_and_restart as test_pg_entry_restart_idempotency,
    test_bad_or_partial_entry_depth_cannot_manufacture_a_fill as test_pg_invalid_or_partial_books,
    test_exit_signal_is_not_a_close_and_later_observation_books_set_real_pnl as test_pg_causal_exits_and_pnl,
    test_missing_exit_quotes_preserve_exposure_and_recover_without_fake_close as test_pg_unresolved_exit_recovery,
    test_global_risk_reservations_serialize_simultaneous_instruments as test_pg_concurrent_global_position_limit,
    test_order_cannot_fill_from_an_observation_preceding_its_actual_creation as test_pg_no_backdated_entry,
    test_ttl_rejection_after_restart_never_backfills_old_quotes as test_pg_restart_rejects_expired_entry,
)
from tests.test_commodity_paper_controls import (
    test_simultaneous_instruments_can_each_fill_but_never_write_nifty as test_pg_concurrent_instrument_fills,
    test_failed_transaction_rolls_back_fill_order_and_cursor_then_retries as test_pg_atomic_rollback_recovery,
    test_global_control_tampering_rejected_on_restart as test_pg_global_control_integrity,
    test_disabled_entries_still_manage_frozen_open_position as test_pg_disabled_entry_exit_recovery,
    test_native_mirror_requires_full_opposite_entry_not_just_direction as test_pg_native_mirror,
    test_forced_session_exit_keeps_next_observation_causality as test_pg_forced_session_exit,
    test_trailing_uses_observed_gain_then_next_observation_exit as test_pg_trailing_exit,
    test_actual_order_creation_not_decision_start_controls_next_observation as test_pg_actual_creation_clock,
    test_mutex_wait_cannot_validate_a_book_against_the_pre_wait_clock as test_pg_mutex_freshness,
)
from tests.test_commodity_paper_read_recovery import (
    test_coordinator_recovers_v1_to_paper_crash_gap_without_same_observation_fill as test_pg_coordinator_crash_gap,
    test_initial_paper_cursor_recovers_latest_v1_not_entire_history as test_pg_initial_cursor_recovery,
    test_read_views_preserve_zero_vs_unknown_and_hide_stale_pnl as test_pg_readonly_views,
)
from tests.test_commodity_paper_naked import (
    test_marks_are_subsequent_asks_and_no_ltp_fallback as test_pg_single_option_marks,
    test_pre_expiry_exit_is_queued_then_filled_or_unresolved_after_restart as test_pg_single_pre_expiry_recovery,
    test_pre_expiry_entry_window_rejected_with_ttl_buffer as test_pg_single_pre_expiry_entry_buffer,
    test_adverse_gap_can_exceed_stop_and_entry_stress_no_clamped_pnl as test_pg_single_unbounded_adverse_gap,
    test_authorized_margin_is_refreshed_at_fill_and_never_actual_blocked_capital as test_pg_single_margin_evidence,
    test_slow_margin_lookup_cannot_authorize_a_stale_fill as test_pg_single_slow_margin,
    test_legacy_open_spread_recovers_without_rewriting_frozen_policy as test_pg_legacy_spread_recovery,
    test_stress_risk_is_repriced_and_rejected_before_fill as test_pg_single_fill_stress_revalidation,
    test_global_stress_exposure_growth_requests_real_subsequent_exit as test_pg_single_global_stress_exit,
    test_thesis_reversal_exit_does_not_require_margin_for_a_new_short as test_pg_single_mirror_without_margin,
    test_stale_future_does_not_become_verified_stress_input as test_pg_single_stress_requires_fresh_reference,
)

from tests.test_commodity_runtime_integration import (
    test_legacy_closed_documents_and_journals_read_and_restart_without_requalification as test_pg_pre_eb6_documents,
    test_renewed_policy_and_native_floors_both_control_subsequent_fill as test_pg_renewal_native_floors,
)

PAPER_TABLES = {
    "commodity_paper_cursors",
    "commodity_paper_controls",
    "commodity_paper_positions",
    "commodity_paper_orders",
    "commodity_paper_fills",
    "commodity_paper_events",
}


@pytest.fixture(scope="module")
def pg_admin():
    value = os.getenv("COMMODITY_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("COMMODITY_POSTGRES_TEST_URL requires a dedicated disposable local PostgreSQL 16 database")
    url = make_url(value)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or not url.port
        or url.database != "commodity_paper_validation"
        or url.username != "commodity_validation"
        or url.query
    ):
        pytest.fail("Only the dedicated loopback commodity PostgreSQL validation database is allowed")
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            assert connection.scalar(text("SELECT current_database()")) == "commodity_paper_validation"
        yield admin
    finally:
        admin.dispose()


@pytest.fixture
def pg_target(pg_admin):
    schema = f"commodity_pg_{uuid4().hex}"
    with pg_admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = pg_admin.url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield url
    finally:
        # Exact generated schema in the guarded disposable DB only.
        with pg_admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def engine(pg_target):
    command.upgrade(migration_config(pg_target.render_as_string(hide_password=False)), "head")
    engine = create_engine(pg_target)
    try:
        with engine.begin() as connection:
            # Existing fixtures insert identical genesis cursors. Remove only
            # these mutable seed rows, retaining every migrated table/trigger/FK.
            for table in ("commodity_acts_cursors", "commodity_paper_cursors", "commodity_paper_controls"):
                connection.execute(text(f"DELETE FROM {table}"))
        yield engine
    finally:
        engine.dispose()


def existing_state(engine):
    """Exact rows + reflected DDL of every pre-existing table, including NIFTY."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    result = {}
    with engine.connect() as connection:
        for table in sorted(tables):
            result[table] = {
                "rows": sorted(connection.execute(text(f'SELECT * FROM "{table}"')).all(), key=repr),
                "columns": [{**column, "type": str(column["type"])} for column in inspector.get_columns(table)],
                "primary_key": inspector.get_pk_constraint(table),
                "foreign_keys": inspector.get_foreign_keys(table),
                "unique_constraints": inspector.get_unique_constraints(table),
                "checks": inspector.get_check_constraints(table),
                "indexes": inspector.get_indexes(table),
                "triggers": connection.execute(
                    text(
                        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t "
                        "WHERE t.tgrelid=to_regclass(:name) AND NOT t.tgisinternal ORDER BY t.tgname"
                    ),
                    {"name": table},
                )
                .scalars()
                .all(),
            }
    return result


def existing_sequences(engine):
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT sequencename, last_value FROM pg_sequences " "WHERE schemaname=current_schema()")
            ).all()
        )


def test_pg_0020_to_0021_preserves_research_and_every_existing_nifty_table(pg_target, market_snapshot, monkeypatch):
    migration = migration_config(pg_target.render_as_string(hide_password=False))
    command.upgrade(migration, "0020_commodity_acts_research")
    engine = create_engine(pg_target)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    clock = SimpleNamespace(at=AT + timedelta(seconds=1))
    import app.alpha.engine as alpha_engine

    class TestDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.at if tz is None else clock.at.astimezone(tz)

    monkeypatch.setattr(alpha_engine, "datetime", TestDateTime)
    try:
        SnapshotRepository(sessions).save_market_snapshot(market_snapshot, market_snapshot.timestamp_ist)
        services = {
            name: CommodityResearch(
                sessions, settings(), config(), config().specification(name), clock=lambda: clock.at
            )
            for name in INSTRUMENTS
        }
        for seconds in (0, 15):
            clock.at = AT + timedelta(seconds=seconds + 1)
            for name, service in services.items():
                service.observe(captured(name, at=AT + timedelta(seconds=seconds)))
        before = existing_state(engine)
        sequences_before = existing_sequences(engine)
        assert len(before["commodity_market_snapshots"]["rows"]) == 4
        assert len(before["commodity_option_quotes"]["rows"]) == 168
        assert len(before["commodity_acts_decisions"]["rows"]) == 4
        assert len(before["commodity_acts_events"]["rows"]) == 4
        assert len(before["market_snapshots"]["rows"]) == 1
        command.upgrade(migration, "0021_commodity_paper_trades")
        after = existing_state(engine)
        assert set(after) - set(before) == PAPER_TABLES
        assert {table: after[table] for table in before} == before
        sequences_after = existing_sequences(engine)
        assert {name: sequences_after[name] for name in sequences_before} == sequences_before
        for name in INSTRUMENTS:
            verify_journal(sessions, name)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM commodity_paper_cursors")) == 2
            assert connection.scalar(text("SELECT count(*) FROM commodity_paper_controls")) == 1
    finally:
        engine.dispose()


def test_pg_fresh_database_upgrades_to_latest_revision(pg_admin):
    database = f"commodity_fresh_{uuid4().hex}"
    with pg_admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = pg_admin.url.set(database=database)
    engine = create_engine(url)
    try:
        assert inspect(engine).get_table_names() == []
        command.upgrade(migration_config(url.render_as_string(hide_password=False)), "head")
        assert PAPER_TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021_commodity_paper_trades"
            assert connection.scalar(text("SELECT count(*) FROM commodity_acts_cursors")) == 2
            assert connection.scalar(text("SELECT count(*) FROM commodity_paper_cursors")) == 2
            assert connection.scalar(text("SELECT policy_json FROM commodity_paper_controls")) == "{}"
    finally:
        engine.dispose()
        with pg_admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE "{database}"'))


@pytest.mark.parametrize("table", ["commodity_paper_fills", "commodity_paper_events"])
@pytest.mark.parametrize("action", ["UPDATE", "DELETE", "TRUNCATE"])
def test_pg_immutable_evidence_blocks_database_mutations(case, table, action):
    from tests.test_commodity_paper import step

    step(case, 0)
    step(case, 15)
    statement = (
        f"UPDATE {table} SET instrument_id='NATGASMINI'"
        if action == "UPDATE"
        else f"{action} FROM {table}" if action == "DELETE" else f"TRUNCATE {table}"
    )
    with pytest.raises(Exception, match="COMMODITY_PAPER_EVIDENCE_IS_IMMUTABLE"):
        with case[0].begin() as session:
            session.execute(text(statement))


def test_pg_concurrent_duplicate_observation_has_one_atomic_fill(case):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import func, select
    from app.commodity_acts.paper_journal import verify
    from app.commodity_acts.paper_models import CommodityPaperFill
    from tests.test_commodity_paper import positions, raw, step

    step(case, 0)
    case[3].at = AT + timedelta(seconds=16)
    snapshot_id, _ = case[1]["CRUDEOILM"].observe(raw(seconds=15))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: case[2]["CRUDEOILM"].process_snapshot(snapshot_id), range(2)))
    assert sorted(results) == ["DUPLICATE", "PAPER_OBSERVATION"]
    assert positions(case)[0][0] == "OPEN"
    with case[0]() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 1
    verify(case[0], "CRUDEOILM")


@pytest.mark.parametrize("restart_state", ["PENDING_PAPER_ENTRY", "OPEN", "PENDING_EXIT", "UNRESOLVED_EXPOSURE"])
def test_pg_restart_reopens_connections_and_recovers_durable_state(case, restart_state):
    from sqlalchemy import func, select
    from app.commodity_acts.paper_coordinator import CommodityPaperCoordinator
    from app.commodity_acts.paper_journal import verify
    from app.commodity_acts.paper_models import CommodityPaperFill
    from tests.test_commodity_paper import positions, raw, step

    step(case, 0)
    if restart_state != "PENDING_PAPER_ENTRY":
        step(case, 15)
    if restart_state in ("PENDING_EXIT", "UNRESOLVED_EXPOSURE"):

        def missing(rows):
            return [{**row, "depth": {}} if not row["exchange_token"].endswith("_F") else row for row in rows]

        step(case, 1830, corrupt=missing if restart_state == "UNRESOLVED_EXPOSURE" else None)
    assert positions(case)[0][0] == restart_state
    old = case[0].kw["bind"]
    url = old.url
    old.dispose()
    fresh = create_engine(url)
    fresh_sessions = sessionmaker(bind=fresh, expire_on_commit=False)
    try:
        original = case[1]["CRUDEOILM"]
        recovered = CommodityPaperCoordinator(
            fresh_sessions, settings(), original.config, original.spec, clock=lambda: case[3].at
        )
        fresh_case = (fresh_sessions, {}, {}, case[3])
        assert positions(fresh_case)[0][0] == restart_state
        seconds = 15 if restart_state == "PENDING_PAPER_ENTRY" else 1830 if restart_state == "OPEN" else 1845
        case[3].at = AT + timedelta(seconds=seconds + 1)
        recovered.observe(raw(seconds=seconds))
        if restart_state == "PENDING_PAPER_ENTRY":
            assert positions(fresh_case)[0][0] == "OPEN"
            seconds = 1830
            case[3].at = AT + timedelta(seconds=seconds + 1)
            recovered.observe(raw(seconds=seconds))
        if positions(fresh_case)[0][0] == "PENDING_EXIT":
            seconds += 15
            case[3].at = AT + timedelta(seconds=seconds + 1)
            recovered.observe(raw(seconds=seconds))
        assert positions(fresh_case)[0][0] == "CLOSED"
        assert recovered.observe(raw(seconds=seconds))[1] == "DUPLICATE"
        with fresh_sessions() as session:
            assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 2
        verify(fresh_sessions, "CRUDEOILM")
    finally:
        fresh.dispose()


@pytest.mark.parametrize("kind", ["order", "fill"])
def test_pg_foreign_keys_reject_cross_instrument_ownership(case, kind):
    import json
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.commodity_acts.paper_journal import verify
    from app.commodity_acts.paper_models import CommodityPaperPosition
    from tests.test_commodity_paper import step

    step(case, 0)
    with case[0]() as session:
        position = session.scalar(select(CommodityPaperPosition))
        owner_id = position.id if kind == "order" else json.loads(position.document)["entry_order_id"]
    table = "commodity_paper_orders" if kind == "order" else "commodity_paper_fills"
    owner_column = "position_id" if kind == "order" else "order_id"
    columns = f"id,instrument_id,{owner_column},observed_at,document" + (",state" if kind == "order" else "")
    values = ":id,'NATGASMINI',:owner,:at,'{}'" + (",'PENDING'" if kind == "order" else "")
    with pytest.raises(IntegrityError) as error:
        with case[0].begin() as session:
            session.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES ({values})"),
                {"id": uuid4().hex, "owner": owner_id, "at": AT.isoformat()},
            )
    assert error.value.orig.sqlstate == "23503"
    verify(case[0], "CRUDEOILM")


# Re-export test fixtures and helpers for the shared PostgreSQL validation suite.
__all__ = [
    "test_pg_pre_eb6_documents", "test_pg_renewal_native_floors",
    "datetime",
    "timedelta",
    "os",
    "SimpleNamespace",
    "uuid4",
    "command",
    "pytest",
    "create_engine",
    "inspect",
    "text",
    "make_url",
    "sessionmaker",
    "INSTRUMENTS",
    "verify_journal",
    "CommodityResearch",
    "SnapshotRepository",
    "AT",
    "captured",
    "config",
    "research_case",
    "migration_config",
    "settings",
    "case",
    "test_pg_entry_restart_idempotency",
    "test_pg_invalid_or_partial_books",
    "test_pg_causal_exits_and_pnl",
    "test_pg_unresolved_exit_recovery",
    "test_pg_concurrent_global_position_limit",
    "test_pg_no_backdated_entry",
    "test_pg_restart_rejects_expired_entry",
    "test_pg_concurrent_instrument_fills",
    "test_pg_atomic_rollback_recovery",
    "test_pg_global_control_integrity",
    "test_pg_disabled_entry_exit_recovery",
    "test_pg_native_mirror",
    "test_pg_forced_session_exit",
    "test_pg_trailing_exit",
    "test_pg_actual_creation_clock",
    "test_pg_mutex_freshness",
    "test_pg_coordinator_crash_gap",
    "test_pg_initial_cursor_recovery",
    "test_pg_readonly_views",
    "test_pg_single_option_marks",
    "test_pg_single_pre_expiry_recovery",
    "test_pg_single_pre_expiry_entry_buffer",
    "test_pg_single_unbounded_adverse_gap",
    "test_pg_single_margin_evidence",
    "test_pg_single_slow_margin",
    "test_pg_legacy_spread_recovery",
    "test_pg_single_fill_stress_revalidation",
    "test_pg_single_global_stress_exit",
    "test_pg_single_mirror_without_margin",
    "test_pg_single_stress_requires_fresh_reference",
]
