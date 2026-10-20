"""Additive isolated schema, immutable evidence, and original rows preserved."""
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.commodity_acts.repository import save_snapshot
from app.db.repositories import SnapshotRepository
from tests.test_commodity_acts import AT, captured


def migration_config(url):
    value = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    value.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return value


def test_additive_migration_preserves_nifty_and_raw_immutability(tmp_path, market_snapshot):
    url = f"sqlite+pysqlite:///{tmp_path / 'commodity-migration.db'}"
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
