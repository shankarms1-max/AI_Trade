"""Read-only Commodity worker freshness check; never opens a broker client."""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import func, select, text  # noqa: E402
from app.alpha.clock import IST  # noqa: E402
from app.commodity_acts.config import CommoditySettings, INSTRUMENTS  # noqa: E402
from app.commodity_acts.models import CommodityActsCursor  # noqa: E402
from app.commodity_acts.paper_models import CommodityPaperCursor, CommodityPaperPosition  # noqa: E402
from app.commodity_acts.paper_policy import ACTIVE  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402


def check(sessions, config, instrument, now):
    spec = config.specification(instrument)
    policy = config.trading_policy(instrument)
    if not config.enabled(instrument) or not config.trading_enabled or policy is None:
        return {"healthy": False, "reason": "PAPER_WORKER_DISABLED_OR_POLICY_MISSING"}
    with sessions() as session, session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        active = session.scalar(
            select(func.count())
            .select_from(CommodityPaperPosition)
            .where(CommodityPaperPosition.instrument_id == instrument, CommodityPaperPosition.state.in_(ACTIVE))
        )
        research = session.get(CommodityActsCursor, instrument)
        paper = session.get(CommodityPaperCursor, instrument)
        if not spec.market_session.contains(now):
            return {"healthy": not active, "reason": "ACTIVE_EXPOSURE_OUTSIDE_SESSION" if active else "SESSION_CLOSED"}
        current_evidence = policy.evidence
        if config.auto_contract_renewal:
            from app.commodity_acts.paper_naked import NakedContractEvidence

            latest = json.loads(paper.latest_json) if paper else {}
            raw_evidence = latest.get("contract_evidence")
            if not raw_evidence:
                start = datetime.combine(now.astimezone(IST).date(), spec.market_session.start, IST)
                grace = not active and now <= start + timedelta(seconds=90)
                return {"healthy": grace, "reason": "SESSION_START_GRACE" if grace else "CONTRACT_RENEWAL_UNAVAILABLE"}
            if raw_evidence:
                current_evidence = NakedContractEvidence.model_validate(raw_evidence)
        if now > current_evidence.valid_until:
            return {"healthy": False, "reason": "CONTRACT_EVIDENCE_EXPIRED"}
        start = datetime.combine(now.astimezone(IST).date(), spec.market_session.start, IST)
        points = [
            datetime.fromisoformat(row.observed_at) if row and row.observed_at else None for row in (research, paper)
        ]
        if not active and now <= start + timedelta(seconds=90) and not all(points):
            return {"healthy": True, "reason": "SESSION_START_GRACE"}
        fresh = all(point is not None and 0 <= (now - point).total_seconds() <= 90 for point in points)
        return {
            "healthy": fresh,
            "reason": "FRESH" if fresh else "RESEARCH_OR_PAPER_OBSERVATION_STALE",
            "active_positions": active,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", required=True, choices=INSTRUMENTS)
    args = parser.parse_args(argv)
    engine = None
    try:
        settings, config = get_settings(), CommoditySettings()
        if settings.database_url is None:
            raise ValueError("database required")
        engine = build_engine(settings.database_url.get_secret_value())
        result = check(build_session_factory(engine), config, args.instrument, datetime.now(IST))
    except Exception:
        result = {"healthy": False, "reason": "HEALTH_CONFIGURATION_OR_DATABASE_UNAVAILABLE"}
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"instrument": args.instrument, **result}, sort_keys=True))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
