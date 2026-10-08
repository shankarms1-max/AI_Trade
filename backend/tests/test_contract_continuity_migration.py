"""Additive migration preserves original capture evidence on SQLite and real PG16."""
from datetime import date, datetime
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("postgres", [False, True])
def test_continuity_migration_preserves_existing_snapshot(tmp_path, postgres):
    schema = f"continuity_{uuid4().hex}"
    admin = None
    if postgres:
        value = os.getenv("PHASE15_POSTGRES_TEST_URL")
        if not value:
            pytest.skip("PHASE15_POSTGRES_TEST_URL is required for PostgreSQL integration")
        base = make_url(value)
        admin = create_engine(base)
        with admin.begin() as connection:
            assert connection.scalar(text("SHOW server_version")).startswith("16.")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = base.update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = make_url(f"sqlite+pysqlite:///{tmp_path / 'continuity.db'}")
    engine = create_engine(url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(
        hide_password=False).replace("%", "%%"))
    try:
        command.upgrade(config, "0016_scalper")
        with engine.begin() as connection:
            raw = Table("market_snapshots", MetaData(), autoload_with=connection)
            connection.execute(raw.insert().values(
                id=1, timestamp_ist=datetime(2026, 10, 8, 10),
                collection_bucket_ist=datetime(2026, 10, 8, 10),
                nifty_spot=25000, atm_strike=25000, expiry=date(2026, 10, 13),
                source="OFFLINE_MIGRATION_FIXTURE"))
            before = dict(connection.execute(select(raw)).mappings().one())
            tables = inspect(connection).get_table_names()
            columns = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                       for name in tables}
        command.upgrade(config, "head")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0017_contract_continuity")
            assert dict(connection.execute(select(raw)).mappings().one()) == before
            assert set(inspect(connection).get_table_names()) == set(tables)
            for name in tables:
                expected = columns[name] + (
                    ["required_contracts_json"] if name == "market_snapshots" else [])
                assert [col["name"] for col in inspect(connection).get_columns(name)] == expected
            added = Table("market_snapshots", MetaData(), autoload_with=connection)
            assert connection.scalar(select(added.c.required_contracts_json)) is None
            evidence = {"requested": [{"instrument_token": "fixture-only"}], "quotes": []}
            connection.execute(added.update().where(added.c.id == 1).values(
                required_contracts_json=evidence))
            assert connection.scalar(select(added.c.required_contracts_json)) == evidence
        command.downgrade(config, "0016_scalper")
        with engine.connect() as connection:
            assert dict(connection.execute(select(raw)).mappings().one()) == before
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
