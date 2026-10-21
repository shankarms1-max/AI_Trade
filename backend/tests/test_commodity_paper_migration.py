"""Local disposable schemas only; no production migration is executed."""
from io import StringIO
import os
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.commodity_acts.repository import save_snapshot
from app.db.repositories import SnapshotRepository
from tests.test_commodity_acts import AT, captured
from tests.test_commodity_acts_migration import migration_config


def additive_check(url, market_snapshot):
    config = migration_config(url)
    command.upgrade(config, "0020_commodity_acts_research")
    engine = create_engine(url)
    sessions = sessionmaker(bind=engine)
    SnapshotRepository(sessions).save_market_snapshot(market_snapshot, market_snapshot.timestamp_ist)
    save_snapshot(sessions, captured(), AT)
    try:
        with engine.connect() as connection:
            existing = set(inspect(connection).get_table_names()) - {"alembic_version"}
            columns = {name: [column["name"] for column in inspect(connection).get_columns(name)] for name in existing}
            before = {name: connection.execute(text(f'SELECT * FROM "{name}"')).all() for name in existing}
        command.upgrade(config, "0021_commodity_paper_trades")
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) - existing - {"alembic_version"} == {
                "commodity_paper_cursors", "commodity_paper_controls", "commodity_paper_positions",
                "commodity_paper_orders", "commodity_paper_fills", "commodity_paper_events"}
            for name in existing:
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == before[name]
                assert [column["name"] for column in inspect(connection).get_columns(name)] == columns[name]
            assert connection.scalar(text("SELECT COUNT(*) FROM commodity_paper_cursors")) == 2
            assert connection.scalar(text("SELECT policy_json FROM commodity_paper_controls WHERE id=1")) == "{}"
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO commodity_paper_positions VALUES ('p','CRUDEOILM','OPEN','{}','2099-10-01')"))
            connection.execute(text("INSERT INTO commodity_paper_orders VALUES ('o','CRUDEOILM','p','FILLED','{}','2099-10-01')"))
            connection.execute(text("INSERT INTO commodity_paper_fills VALUES ('f','CRUDEOILM','o','{}','2099-10-01')"))
            connection.execute(text("INSERT INTO commodity_paper_events VALUES ('CRUDEOILM',1,'e','TEST','GENESIS','h','{}')"))
        for table in ("commodity_paper_fills", "commodity_paper_events"):
            for action in (f"DELETE FROM {table}", f"UPDATE {table} SET instrument_id='NATGASMINI'"):
                with pytest.raises(Exception, match="COMMODITY_PAPER_EVIDENCE_IS_IMMUTABLE"):
                    with engine.begin() as connection:
                        connection.execute(text(action))
            if engine.dialect.name == "postgresql":
                with pytest.raises(Exception, match="COMMODITY_PAPER_EVIDENCE_IS_IMMUTABLE"):
                    with engine.begin() as connection:
                        connection.execute(text(f"TRUNCATE {table}"))
        command.downgrade(config, "0020_commodity_acts_research")
        with engine.connect() as connection:
            for name in existing:
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == before[name]
    finally:
        engine.dispose()


def test_additive_paper_migration_preserves_nifty_and_v1_and_enforces_immutable_fills(tmp_path, market_snapshot):
    additive_check(f"sqlite+pysqlite:///{tmp_path / 'paper-migration.db'}", market_snapshot)


def test_postgresql_paper_migration_offline_sql_is_additive():
    output = StringIO()
    config = migration_config("postgresql+psycopg://offline:dummy@localhost/offline")
    config.output_buffer = output
    command.upgrade(config, "0020_commodity_acts_research:0021_commodity_paper_trades", sql=True)
    sql = output.getvalue()
    assert "TRUNCATE ON commodity_paper_fills" in sql and "TRUNCATE ON commodity_paper_events" in sql
    assert "PRIMARY KEY (instrument_id, sequence)" in sql
    for table in ("market_snapshots", "acts_paper", "parallel_paper", "commodity_market_snapshots", "commodity_acts_"):
        assert f"UPDATE {table}" not in sql and f"ALTER TABLE {table}" not in sql


def test_postgresql_paper_migration_on_disposable_schema(market_snapshot):
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    schema = f"commodity_paper_{uuid4().hex}"
    try:
        with admin.begin() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        target = base.update_query_dict({"options": f"-csearch_path={schema}"})
        try:
            additive_check(target.render_as_string(hide_password=False), market_snapshot)
        finally:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin.dispose()
