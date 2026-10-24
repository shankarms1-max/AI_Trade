"""Guard the NIFTY release boundary without running workers or opening a database."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.main import app

ROOT = Path(__file__).resolve().parents[2]


def test_commodity_is_integrated_without_deployment_assets_or_session_workers():
    excluded = (
        "backend/app/commodity_acts", "backend/app/api/commodity_acts.py",
        "alembic/versions/0020_commodity_acts_research.py",
        "alembic/versions/0021_commodity_paper_trades.py",
        "scripts/deploy_commodity_automation.py",
    )
    assert all((ROOT / name).exists() for name in excluded[:-1])
    assert not (ROOT / excluded[-1]).exists()
    assert (ROOT / "worker/commodity_acts_paper_worker.py").exists()
    assert not list((ROOT / "worker").glob("commodity_session_*"))


def test_commodity_extends_nifty_with_one_additive_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0021_commodity_paper_trades"]
    assert scripts.get_revision("0020_commodity_acts_research").down_revision == "0019_parallel_paper"
    assert scripts.get_revision("0021_commodity_paper_trades").down_revision == "0020_commodity_acts_research"


def test_both_readonly_api_families_and_isolated_commodity_tables_are_registered():
    from app.db.base import Base

    paths = set(app.openapi()["paths"])
    assert "/api/phase15/summary" in paths
    assert "/api/phase15/positions" in paths
    assert "/api/commodity-acts/summary" in paths
    assert "/api/commodity-acts/paper/summary" in paths
    assert "commodity_paper_fills" in Base.metadata.tables
    assert "commodity_market_snapshots" in Base.metadata.tables
