from dataclasses import dataclass

from app.ai.base import ResearchModelProvider
from app.ai.input_builder import build_ai_research_input
from app.ai.models import AIResearchResult
from app.ai.repository import AIResearchRepository
from app.ai.research_agent import AIResearchConfig, generate_research


@dataclass(frozen=True)
class AIResearchBuildResult:
    result: AIResearchResult
    reused_existing: bool


def build_and_store_ai_research(
    repository: AIResearchRepository,
    snapshot_id: int,
    provider: ResearchModelProvider,
    config: AIResearchConfig,
    *,
    force: bool = False,
    candidate_set=None,
) -> AIResearchBuildResult:
    if not force:
        existing = repository.existing_success(snapshot_id)
        if existing is not None:
            return AIResearchBuildResult(existing, True)
    context = repository.load_context(snapshot_id)
    if context is None:
        raise LookupError(
            f"phase3_v1 features and phase4_v1 regime for snapshot {snapshot_id} are required"
        )
    feature_id, feature, regime_id, regime = context
    research_input = build_ai_research_input(feature, regime, repository.load_alpha(snapshot_id), candidate_set if candidate_set is not None else getattr(repository, "load_candidates", lambda _: None)(snapshot_id))
    repository.start(
        snapshot_id,
        feature_id,
        regime_id,
        provider.provider_name,
        provider.model_name,
    )
    try:
        result = generate_research(research_input, provider, config)
        result = result.model_copy(update={"ai_version": getattr(repository, "ai_version", result.ai_version), "prompt_version": getattr(repository, "prompt_version", result.prompt_version)})
        repository.complete(result)
        return AIResearchBuildResult(result, False)
    except Exception as exc:
        repository.fail(snapshot_id, exc)
        raise


def config_from_settings(settings) -> AIResearchConfig:
    from app.ai.guardrails import ConfidenceCaps

    return AIResearchConfig(
        confidence_caps=ConfidenceCaps(
            insufficient=settings.ai_confidence_cap_insufficient,
            low=settings.ai_confidence_cap_low,
            medium=settings.ai_confidence_cap_medium,
            high=settings.ai_confidence_cap_high,
        ),
        input_cost_per_million=settings.ai_input_cost_per_million,
        output_cost_per_million=settings.ai_output_cost_per_million,
    )
