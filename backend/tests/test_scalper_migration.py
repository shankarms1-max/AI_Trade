from datetime import date, datetime
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DatabaseError

from app.db.alembic_bootstrap import ensure_postgresql_alembic_version_capacity

ROOT = Path(__file__).resolve().parents[2]


def test_scalper_migration_is_additive_and_journal_is_immutable(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    url = f"sqlite:///{tmp_path / 'scalper.db'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0015_forward_paper")
    engine = create_engine(url)
    with engine.begin() as connection:
        raw = Table("market_snapshots", MetaData(), autoload_with=connection)
        connection.execute(raw.insert().values(
            id=1, timestamp_ist=datetime(2026, 10, 7, 10),
            collection_bucket_ist=datetime(2026, 10, 7, 10), nifty_spot=25000,
            atm_strike=25000, expiry=date(2026, 10, 8), source="OFFLINE_FIXTURE"))
        before = dict(connection.execute(select(raw)).mappings().one())
    command.upgrade(config, "0016_scalper")
    with engine.begin() as connection:
        assert dict(connection.execute(select(raw)).mappings().one()) == before
        assert connection.execute(text(
            "SELECT sequence, head_hash FROM scalper_cursor")).one() == (0, "GENESIS")
        connection.execute(text(
            "INSERT INTO scalper_market_snapshots "
            "(id,capture_key,captured_at,request_started_at,response_received_at,"
            "nifty_spot,lot_size,atm_strike,expiry,source,feature_json,signal_json) "
            "VALUES (1,'fixture','2026-10-07 10:00','2026-10-07 10:00',"
            "'2026-10-07 10:00',25000,50,25000,'2026-10-08','FIXTURE','{}','{}')"
        ))
        connection.execute(text(
            "INSERT INTO scalper_events "
            "(sequence,event_key,event_type,snapshot_id,previous_hash,event_hash,payload) "
            "VALUES (1,'fixture','SCALPER_OBSERVATION',1,'GENESIS','fixture','{}')"
        ))
    for statement in ("UPDATE scalper_events SET payload='altered'",
                      "DELETE FROM scalper_events"):
        with pytest.raises(DatabaseError, match="SCALPER_EVIDENCE_IS_IMMUTABLE"):
            with engine.begin() as connection:
                connection.execute(text(statement))
    command.downgrade(config, "0015_forward_paper")
    assert "scalper_trades" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert dict(connection.execute(select(raw)).mappings().one()) == before
    engine.dispose()


def test_postgresql_scalper_ddl_compiles_offline():
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("sqlalchemy.url",
                           "postgresql+psycopg://offline:dummy@localhost/offline")
    command.upgrade(config, "0015_forward_paper:0016_scalper", sql=True)
    ddl = output.getvalue()
    assert "CREATE TABLE scalper_market_snapshots" in ddl
    assert "CREATE TABLE scalper_option_quotes" in ddl
    assert "CREATE TABLE scalper_trades" in ddl
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE ON scalper_events" in ddl
    assert "ALTER TABLE market_snapshots" not in ddl
    assert len("0016_scalper") <= 32


@pytest.mark.skipif(
    not os.getenv("PHASE15_POSTGRES_TEST_URL"),
    reason="PHASE15_POSTGRES_TEST_URL is required for PostgreSQL integration",
)
def test_postgresql_empty_upgrade_and_phase15_transition():
    base_url = make_url(os.environ["PHASE15_POSTGRES_TEST_URL"])
    schema = f"phase15_migration_{uuid4().hex}"
    admin_engine = create_engine(base_url)
    scoped_url = base_url.update_query_dict({"options": f"-csearch_path={schema}"})
    scoped_engine = create_engine(scoped_url)

    def reset_schema() -> None:
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    def migration_config() -> Config:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option(
            "sqlalchemy.url",
            scoped_url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        return config

    try:
        reset_schema()
        with scoped_engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE bootstrap_sentinel "
                "(id INTEGER PRIMARY KEY, evidence TEXT NOT NULL)"
            ))
            connection.execute(text(
                "INSERT INTO bootstrap_sentinel VALUES (1, 'unchanged')"
            ))
            connection.execute(text(
                "CREATE TABLE alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            ))
            ensure_postgresql_alembic_version_capacity(connection)
            tables = set(inspect(connection).get_table_names())
            assert tables == {"alembic_version", "bootstrap_sentinel"}
            assert connection.execute(text(
                "SELECT evidence FROM bootstrap_sentinel WHERE id = 1"
            )).scalar_one() == "unchanged"
            version_column = next(
                column for column in inspect(connection).get_columns("alembic_version")
                if column["name"] == "version_num"
            )
            assert version_column["type"].length >= 64

        reset_schema()
        config = migration_config()
        command.upgrade(config, "head")
        with scoped_engine.connect() as connection:
            assert connection.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == "0017_contract_continuity"
            version_column = next(
                column for column in inspect(connection).get_columns("alembic_version")
                if column["name"] == "version_num"
            )
            assert version_column["type"].length >= 64

        reset_schema()
        config = migration_config()
        command.upgrade(config, "0015_forward_paper")
        with scoped_engine.begin() as connection:
            snapshots = Table(
                "market_snapshots", MetaData(), autoload_with=connection
            )
            connection.execute(snapshots.insert().values(
                id=1,
                timestamp_ist=datetime(2026, 10, 7, 10),
                collection_bucket_ist=datetime(2026, 10, 7, 10),
                nifty_spot=25000,
                atm_strike=25000,
                expiry=date(2026, 10, 8),
                source="POSTGRES_FIXTURE",
            ))
            before = dict(connection.execute(select(snapshots)).mappings().one())
        command.upgrade(config, "0016_scalper")
        with scoped_engine.connect() as connection:
            assert connection.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == "0016_scalper"
            assert dict(connection.execute(
                select(snapshots)
            ).mappings().one()) == before
    finally:
        scoped_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
