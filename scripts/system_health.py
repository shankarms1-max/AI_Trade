"""Print a compact, read-only system health report."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402
from app.observability.service import ObservabilityService  # noqa: E402


def exit_code(status: str) -> int:
    if status in {"HEALTHY", "IDLE"}:
        return 0
    return 2 if status == "UNHEALTHY" else 1


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print("SYSTEM HEALTH\ndatabase=UNHEALTHY\noverall=UNHEALTHY")
        return 2
    sessions = build_session_factory(build_engine(settings.database_url.get_secret_value()))
    result = ObservabilityService(sessions, settings).compact_health()
    components = result["components"]
    print("SYSTEM HEALTH")
    print(f"database={components['database']}")
    print(f"collector={components['collector_heartbeat']}")
    for name in ("data_freshness", "market_data", "pipeline", "shadow", "ai"):
        print(f"{name}={components[name]}")
    print(f"telegram={components.get('notifications', 'DISABLED')}")
    print(f"overall={result['status']}")
    return exit_code(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
