"""Additive isolated schema, immutable evidence, and original rows preserved."""
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.commodity_acts.repository import save_snapshot
from app.db.repositories import SnapshotRepository
from tests.test_commodity_acts import AT, captured


def migration_config(url):
    value = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    value.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return value


def _check_additive_migration(url, market_snapshot):
    configuration = migration_config(url)
    command.upgrade(configuration, "0019_parallel_paper")
    engine = create_engine(url)
    sessions = sessionmaker(bind=engine)
    SnapshotRepository(sessions).save_market_snapshot(market_snapshot, market_snapshot.timestamp_ist)
    with engine.connect() as connection:
        names = set(inspect(connection).get_table_names()) - {"alembic_version"}
        columns = {name: [column["name"] for column in inspect(connection).get_columns(name)] for name in names}
        before = {name: connection.execute(text(f'SELECT * FROM "{name}"')).all() for name in names}
    command.upgrade(configuration, "0020_commodity_acts_research")
    try:
        with engine.connect() as connection:
            after = set(inspect(connection).get_table_names()) - {"alembic_version"}
            assert after - names == {"commodity_market_snapshots", "commodity_option_quotes", "commodity_acts_cursors", "commodity_acts_events", "commodity_acts_decisions"}
            for name in names:
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == before[name]
                assert [column["name"] for column in inspect(connection).get_columns(name)] == columns[name]
            assert set(connection.scalars(text("SELECT instrument_id FROM commodity_acts_cursors"))) == {"CRUDEOILM", "NATGASMINI"}
        save_snapshot(sessions, captured(), AT)
        for table in ("commodity_market_snapshots", "commodity_option_quotes"):
            for action in (f"DELETE FROM {table}", f"UPDATE {table} SET instrument_id='NATGASMINI'"):
                with pytest.raises(Exception, match="COMMODITY_RESEARCH_EVIDENCE_IS_IMMUTABLE"):
                    with engine.begin() as connection:
                        connection.execute(text(action))
        command.downgrade(configuration, "0019_parallel_paper")
        with engine.connect() as connection:
            for name in names:
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == before[name]
    finally:
        engine.dispose()


def test_additive_migration_preserves_nifty_and_raw_immutability(tmp_path, market_snapshot):
    _check_additive_migration(f"sqlite+pysqlite:///{tmp_path / 'commodity-migration.db'}", market_snapshot)


def test_commodity_migration_is_the_single_head_with_unchanged_parent():
    scripts = ScriptDirectory.from_config(migration_config("sqlite://"))
    assert scripts.get_heads() == ["0021_commodity_paper_trades"]
    assert scripts.get_revision("0021_commodity_paper_trades").down_revision == "0020_commodity_acts_research"
    assert scripts.get_revision("0020_commodity_acts_research").down_revision == "0019_parallel_paper"


def test_commodity_postgresql_additive_migration(market_snapshot):
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    schema = f"commodity_research_{uuid4().hex}"
    try:
        with admin.begin() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        target = base.update_query_dict({"options": f"-csearch_path={schema}"})
        try:
            _check_additive_migration(target.render_as_string(hide_password=False), market_snapshot)
        finally:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin.dispose()


def test_postgresql_migration_compiles_without_touching_existing_tables():
    output = StringIO()
    configuration = migration_config("postgresql+psycopg://offline:dummy@localhost/offline")
    configuration.output_buffer = output
    command.upgrade(configuration, "0019_parallel_paper:0020_commodity_acts_research", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE commodity_acts_events" in sql and "PRIMARY KEY (instrument_id, sequence)" in sql
    assert "TRUNCATE ON commodity_market_snapshots" in sql and "TRUNCATE ON commodity_acts_events" in sql
    for existing in ("acts_paper", "parallel_paper", "scalper_", "market_snapshots"):
        assert f"UPDATE {existing}" not in sql and f"ALTER TABLE {existing}" not in sql
