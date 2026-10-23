"""Guard the NIFTY release boundary without running workers or opening a database."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_no_commodity_backend_models_workers_or_deployment_assets():
    excluded = (
        "backend/app/commodity_acts", "backend/app/api/commodity_acts.py",
        "alembic/versions/0020_commodity_acts_research.py",
        "alembic/versions/0021_commodity_paper_trades.py",
        "scripts/deploy_commodity_automation.py",
    )
    assert not [name for name in excluded if (ROOT / name).exists()]
    assert not list((ROOT / "worker").glob("commodity_acts_*"))
    assert not list((ROOT / "worker").glob("commodity_session_*"))


def test_nifty_migration_head_has_no_commodity_revisions():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0019_parallel_paper"]
    assert not any("commodity" in revision.path.lower() for revision in scripts.walk_revisions())


def test_nifty_runtime_has_no_commodity_api_or_tables():
    from app.db.base import Base

    paths = set(app.openapi()["paths"])
    assert "/api/phase15/summary" in paths
    assert "/api/phase15/positions" in paths
    assert not any("commodity" in path.lower() for path in paths)
    assert not any("commodity" in name.lower() for name in Base.metadata.tables)
