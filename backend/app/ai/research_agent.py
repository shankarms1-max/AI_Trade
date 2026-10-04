from dataclasses import dataclass

from app.ai.base import ResearchModelProvider
from app.ai.guardrails import (
    ConfidenceCaps,
    agreement_status,
    apply_confidence_cap,
    estimate_cost_usd,
    validate_evidence_claims,
)
from app.ai.models import AIResearchInput, AIResearchResult


@dataclass(frozen=True)
class AIResearchConfig:
    confidence_caps: ConfidenceCaps = ConfidenceCaps()
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


def generate_research(
    research_input: AIResearchInput,
    provider: ResearchModelProvider,
    config: AIResearchConfig = AIResearchConfig(),
) -> AIResearchResult:
    provider_result = provider.generate_research(research_input)
    output = provider_result.output
    validate_evidence_claims(research_input, output)
    confidence, capped, cap_reason = apply_confidence_cap(
        output, research_input.phase4.evidence_quality, config.confidence_caps
    )
    usage = provider_result.usage
    return AIResearchResult(
        snapshot_id=research_input.snapshot_id,
        deterministic_regime=research_input.phase4.regime,
        market_view=output.market_view,
        confidence=confidence,
        original_ai_confidence=output.confidence,
        confidence_capped=capped,
        cap_reason=cap_reason,
        agreement_status=agreement_status(
            research_input.phase4.regime, output.market_view
        ),
        support_zone=output.support_zone,
        resistance_zone=output.resistance_zone,
        key_observations=output.key_observations,
        bullish_evidence=output.bullish_evidence,
        bearish_evidence=output.bearish_evidence,
        range_evidence=output.range_evidence,
        risks=output.risks,
        missing_evidence=output.missing_evidence,
        what_would_change_view=output.what_would_change_view,
        research_summary=output.research_summary,
        provider=provider_result.provider,
        model=provider_result.model,
        requested_at=provider_result.requested_at,
        responded_at=provider_result.responded_at,
        latency_ms=provider_result.latency_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=estimate_cost_usd(
            usage.input_tokens,
            usage.output_tokens,
            config.input_cost_per_million,
            config.output_cost_per_million,
        ),
    )
