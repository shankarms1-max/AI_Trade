"""ACTS paper schema is additive to existing Phase 15 evidence."""
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url


def test_acts_paper_migration_is_additive(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'acts-migration.db'}"
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0017_contract_continuity")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            before = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                      for name in inspect(connection).get_table_names()}
        command.upgrade(config, "0018_acts_paper")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0018_acts_paper")
            after = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                     for name in inspect(connection).get_table_names()}
            assert all(after[name] == columns for name, columns in before.items())
            assert set(after) - set(before) == {
                "acts_paper_cursor", "acts_paper_trades", "acts_paper_events"}
            assert connection.scalar(text("SELECT head_hash FROM acts_paper_cursor")) == (
                "GENESIS")
            triggers = connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'acts_paper_events'"))
            assert {row[0] for row in triggers} == {
                "acts_paper_events_no_update", "acts_paper_events_no_delete"}
    finally:
        engine.dispose()


def test_acts_paper_postgres16_migration_is_additive():
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL is required for PostgreSQL integration")
    schema = f"acts_paper_{uuid4().hex}"
    base = make_url(value)
    admin = create_engine(base)
    with admin.begin() as connection:
        assert connection.scalar(text("SHOW server_version")).startswith("16.")
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = base.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_engine(url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(
        hide_password=False).replace("%", "%%"))
    try:
        command.upgrade(config, "0017_contract_continuity")
        with engine.connect() as connection:
            before = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                      for name in inspect(connection).get_table_names()}
        command.upgrade(config, "0018_acts_paper")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0018_acts_paper")
            after = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                     for name in inspect(connection).get_table_names()}
            assert all(after[name] == columns for name, columns in before.items())
            assert set(after) - set(before) == {
                "acts_paper_cursor", "acts_paper_trades", "acts_paper_events"}
            assert connection.scalar(text("SELECT head_hash FROM acts_paper_cursor")) == (
                "GENESIS")
            assert connection.scalar(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = "
                "'acts_paper_events_immutable'")) == 1
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
