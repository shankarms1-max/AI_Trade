"""Additive journal schema, ownership FKs and database-level evidence immutability."""
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.acts_paper.service import ActsPaperService
from app.db.session import build_session_factory
from app.parallel_paper.config import STRATEGIES
from app.parallel_paper.service import ParallelPaperService, verify_journals
from app.scalper.research_capture import ACTS_PAPER_MODE, ResearchSnapshotRepository
from tests.test_parallel_paper_service import capture, raw
from tests.test_scalper import settings

ROOT = Path(__file__).resolve().parents[2]


def _migration(url):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return config


def _check(url):
    config = _migration(url)
    command.upgrade(config, "0018_acts_paper")
    engine = create_engine(url)
    try:
        sessions = build_session_factory(engine)
        original = ResearchSnapshotRepository(engine, mode=ACTS_PAPER_MODE).save(raw(0))
        acts = ActsPaperService(sessions, settings(acts_paper_enabled=True))
        acts.process_snapshot(original.snapshot_id)
        with engine.connect() as connection:
            before_columns = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                              for name in inspect(connection).get_table_names()}
            before_rows = {name: connection.execute(text(f'SELECT * FROM "{name}"')).all()
                           for name in ("acts_paper_cursor", "acts_paper_trades", "acts_paper_events",
                                        "scalper_market_snapshots", "scalper_option_quotes")}
        command.upgrade(config, "0019_parallel_paper")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0019_parallel_paper"
            after_columns = {name: [column["name"] for column in inspect(connection).get_columns(name)]
                             for name in inspect(connection).get_table_names()}
            assert all(after_columns[name] == columns for name, columns in before_columns.items())
            assert set(after_columns) - set(before_columns) == {
                "parallel_paper_cursors", "parallel_paper_trades", "parallel_paper_events"}
            for name, rows in before_rows.items():
                assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == rows
            assert set(connection.scalars(text("SELECT strategy_id FROM parallel_paper_cursors"))) == set(STRATEGIES)
        paper = ParallelPaperService(sessions, settings())
        paper.process_snapshot(capture(engine, raw(0)))
        verify_journals(sessions)
        for action in ("UPDATE parallel_paper_events SET payload='{}'",
                       "DELETE FROM parallel_paper_events"):
            with pytest.raises(Exception, match="PARALLEL_PAPER_EVIDENCE_IS_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(text(action))
        command.downgrade(config, "0018_acts_paper")
        with engine.connect() as connection:
            for name, rows in before_rows.items():
                if name.startswith("acts_"):
                    assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == rows
    finally:
        engine.dispose()


def test_parallel_migration_preserves_acts_and_capture_and_enforces_immutability(tmp_path):
    _check(f"sqlite+pysqlite:///{tmp_path / 'parallel-migration.db'}")


def test_parallel_postgresql_ddl_and_triggers_compile_offline():
    output = StringIO()
    config = _migration("postgresql+psycopg://offline:dummy@localhost/offline")
    config.output_buffer = output
    command.upgrade(config, "0018_acts_paper:0019_parallel_paper", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE parallel_paper_events" in sql
    assert "PRIMARY KEY (strategy_id, sequence)" in sql
    assert "FOREIGN KEY(strategy_id, trade_id)" in sql
    assert "TRUNCATE ON parallel_paper_events" in sql
    assert "reject_parallel_paper_event_mutation" in sql
    assert "UPDATE acts_paper" not in sql and "DROP TABLE acts_paper" not in sql


def test_parallel_postgres16_additive_migration():
    value = os.getenv("PHASE15_POSTGRES_TEST_URL")
    if not value:
        pytest.skip("PHASE15_POSTGRES_TEST_URL must point to disposable PostgreSQL 16")
    base = make_url(value)
    admin = create_engine(base)
    schema = f"parallel_paper_{uuid4().hex}"
    with admin.begin() as connection:
        assert connection.scalar(text("SHOW server_version")).startswith("16.")
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = base.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        _check(url.render_as_string(hide_password=False))
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
