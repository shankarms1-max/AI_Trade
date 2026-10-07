from datetime import date, datetime
from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import DatabaseError

ROOT = Path(__file__).resolve().parents[2]


def test_additive_migration_preserves_capture_and_enforces_sql_immutability(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    url = f"sqlite:///{tmp_path / 'paper_migration.db'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0014_phase14_2_1_replay_integrity")
    engine = create_engine(url)
    with engine.begin() as connection:
        raw = Table("market_snapshots", MetaData(), autoload_with=connection)
        connection.execute(raw.insert().values(id=1, timestamp_ist=datetime(2026, 10, 7, 10),
            collection_bucket_ist=datetime(2026, 10, 7, 10), nifty_spot=25000,
            atm_strike=25000, expiry=date(2026, 10, 13), source="OFFLINE_FIXTURE"))
        before = dict(connection.execute(select(raw)).mappings().one())
    command.upgrade(config, "0015_forward_paper")
    with engine.begin() as connection:
        assert dict(connection.execute(select(raw)).mappings().one()) == before
        cursor = connection.execute(text("SELECT sequence, head_hash FROM paper_cursor")).one()
        assert tuple(cursor) == (0, "GENESIS")
        connection.execute(text("INSERT INTO paper_events (sequence,event_key,event_type,snapshot_id,previous_hash,event_hash,payload) VALUES (1,'fixture','OBSERVATION',1,'GENESIS','fixture','{}')"))
    for statement in ("UPDATE paper_events SET payload='altered'", "DELETE FROM paper_events"):
        with pytest.raises(DatabaseError, match="PAPER_EVIDENCE_IS_IMMUTABLE"):
            with engine.begin() as connection:
                connection.execute(text(statement))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT payload FROM paper_events")).scalar() == "{}"
    command.downgrade(config, "0014_phase14_2_1_replay_integrity")
    assert "paper_trades" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert dict(connection.execute(select(raw)).mappings().one()) == before
    engine.dispose()


def test_postgresql_additive_ddl_and_trigger_compile_offline():
    buffer = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline:dummy@localhost/offline")
    command.upgrade(config, "0014_phase14_2_1_replay_integrity:0015_forward_paper", sql=True)
    ddl = buffer.getvalue()
    assert "CREATE TABLE paper_trades" in ddl
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE ON paper_events" in ddl
    assert "UNIQUE (decision_snapshot_id)" in ddl and "UNIQUE (event_key)" in ddl
    assert "ALTER TABLE market_snapshots" not in ddl
    assert "0015_forward_paper" in ddl and len("0015_forward_paper") <= 32
