"""AST additive migration, SQLite/PostgreSQL immutability and evidence preservation."""

from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


def migration(url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return cfg


def check(url):
    cfg = migration(url)
    command.upgrade(cfg, "0021_commodity_paper_trades")
    engine = create_engine(url)
    from app.scalper.research_capture import ResearchSnapshotRepository
    from tests.test_scalper import snapshot

    captured = ResearchSnapshotRepository(engine).save(snapshot(0))
    try:
        with engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            before = {
                name: connection.execute(text(f'SELECT * FROM "{name}"')).all()
                for name in tables
                if name != "alembic_version"
            }
            columns = {name: inspect(connection).get_columns(name) for name in before}
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0022_adaptive_supertrend"
            assert set(inspect(connection).get_table_names()) - set(tables) == {
                "ast_optimizer_runs",
                "ast_session_parameters",
                "ast_paper_cursor",
                "ast_paper_trades",
                "ast_paper_events",
            }
            for name, rows in before.items():
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == rows
                assert [c["name"] for c in inspect(connection).get_columns(name)] == [c["name"] for c in columns[name]]
            assert connection.execute(text("SELECT sequence,head_hash FROM ast_paper_cursor")).one() == (0, "GENESIS")
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO ast_optimizer_runs (id,document) VALUES ('test','{}')"))
            connection.execute(
                text(
                    "INSERT INTO ast_session_parameters (trading_date,optimizer_run_id,document) "
                    "VALUES ('2026-11-20','test','{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO ast_paper_events (sequence,event_key,event_type,snapshot_id,"
                    "previous_hash,event_hash,payload) VALUES (1,'fixture','OBSERVATION',:snapshot,"
                    "'GENESIS','fixture','{}')"
                ),
                {"snapshot": captured.snapshot_id},
            )
        for table in ("ast_optimizer_runs", "ast_session_parameters", "ast_paper_events"):
            for verb in ("UPDATE", "DELETE"):
                stmt = (
                    f"UPDATE {table} SET {'document' if table != 'ast_paper_events' else 'payload'}='{{}}'"
                    if verb == "UPDATE"
                    else f"DELETE FROM {table}"
                )
                with pytest.raises(Exception, match="AST_EVIDENCE_IS_IMMUTABLE"):
                    with engine.begin() as connection:
                        connection.execute(text(stmt))
            if engine.dialect.name == "postgresql":
                with pytest.raises(Exception, match="AST_EVIDENCE_IS_IMMUTABLE"):
                    with engine.begin() as connection:
                        connection.execute(text(f"TRUNCATE {table} CASCADE"))
        command.downgrade(cfg, "0021_commodity_paper_trades")
        with engine.connect() as connection:
            for name, rows in before.items():
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == rows
    finally:
        engine.dispose()


def test_additive_sqlite_migration(tmp_path):
    check(f'sqlite+pysqlite:///{tmp_path / "ast.db"}')


def test_postgresql_ddl():
    cfg = migration("postgresql+psycopg://offline:dummy@localhost/offline")
    cfg.output_buffer = StringIO()
    command.upgrade(cfg, "0021_commodity_paper_trades:head", sql=True)
    sql = cfg.output_buffer.getvalue()
    assert "TRUNCATE ON ast_paper_events" in sql
    assert "CREATE TABLE ast_session_parameters" in sql
    assert "ALTER TABLE acts_" not in sql and "UPDATE acts_" not in sql


def test_real_postgresql_16_additive_and_fresh_upgrade():
    url = os.getenv("AST_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("AST_POSTGRES_TEST_URL must name disposable PostgreSQL 16")
    base = make_url(url)
    admin = create_engine(base)
    schema = "ast_test_" + uuid4().hex
    with admin.begin() as connection:
        assert connection.scalar(text("SHOW server_version")).startswith("16.")
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = base.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        check(isolated.render_as_string(hide_password=False))
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
