"""Generate on-demand, research-only AI interpretation of Phase 3/4 evidence."""

import argparse
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.ai.openai_provider import OpenAIResearchProvider  # noqa: E402
from app.ai.repository import AIResearchRepository  # noqa: E402
from app.ai.service import build_and_store_ai_research, config_from_settings  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db.session import build_engine, build_session_factory  # noqa: E402


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    group = value.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--snapshot-id", type=int)
    value.add_argument("--force", action="store_true")
    return value


def result_lines(built) -> list[str]:
    result = built.result
    return [
        "AI_RESEARCH_EXISTING" if built.reused_existing else "AI_RESEARCH_CREATED",
        f"snapshot_id={result.snapshot_id}",
        f"deterministic_regime={result.deterministic_regime}",
        f"ai_view={result.market_view.value}",
        f"confidence={result.confidence}",
        f"agreement={result.agreement_status.value}",
        f"model={result.model}",
        f"tokens={result.total_tokens}",
        f"estimated_cost_usd={result.estimated_cost_usd}",
    ]


def main() -> int:
    args = parser().parse_args()
    settings = get_settings()
    missing = []
    if not settings.database_url:
        missing.append("DATABASE_URL")
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.ai_research_model:
        missing.append("AI_RESEARCH_MODEL")
    if missing:
        print(f"Configuration error: {', '.join(missing)} required", file=sys.stderr)
        return 2
    repository = AIResearchRepository(build_session_factory(
        build_engine(settings.database_url.get_secret_value())
    ), regime_version=settings.active_regime_version, strategy_version=settings.active_strategy_version)
    snapshot_id = args.snapshot_id
    if args.latest:
        snapshot_id = repository.latest_context_snapshot_id()
        if snapshot_id is None:
            print("AI research failed: no Phase 4 result found", file=sys.stderr)
            return 1
    provider = OpenAIResearchProvider(
        settings.openai_api_key.get_secret_value(),
        settings.ai_research_model,
        settings.ai_max_retries,
    )
    try:
        built = build_and_store_ai_research(
            repository,
            snapshot_id,
            provider,
            config_from_settings(settings),
            force=args.force,
        )
    except Exception as exc:
        print(f"AI research failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    for line in result_lines(built):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
