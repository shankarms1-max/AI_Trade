from dataclasses import dataclass

from app.ai.models import (
    AgreementStatus, AIResearchInput, AIResearchModelOutput, MarketView,
    AlphaAssessmentDirection, AlphaDerivativesAlignment,
)


class AIResearchSafetyError(ValueError):
    pass


@dataclass(frozen=True)
class ConfidenceCaps:
    insufficient: float = 40
    low: float = 55
    medium: float = 75
    high: float = 90

    def for_quality(self, quality: str) -> float:
        return {
            "INSUFFICIENT": self.insufficient,
            "LOW": self.low,
            "MEDIUM": self.medium,
            "HIGH": self.high,
        }[quality]


def agreement_status(deterministic: str, ai_view: MarketView) -> AgreementStatus:
    if deterministic == ai_view.value:
        return AgreementStatus.AGREE
    if deterministic == "NO_TRADE" and ai_view == MarketView.UNCERTAIN:
        return AgreementStatus.AGREE
    if ai_view == MarketView.UNCERTAIN and deterministic in {
        "BULLISH", "BEARISH", "RANGE"
    }:
        return AgreementStatus.PARTIAL
    if deterministic == "NO_TRADE":
        return AgreementStatus.NOT_COMPARABLE
    return AgreementStatus.DISAGREE


def validate_evidence_claims(
    research_input: AIResearchInput, output: AIResearchModelOutput
) -> None:
    alpha = research_input.phase14_alpha
    if not alpha.available and (
        output.alpha_direction != AlphaAssessmentDirection.INSUFFICIENT
        or output.key_alpha_evidence
    ):
        raise AIResearchSafetyError("AI output invented unavailable statistical alpha evidence")
    if alpha.joint_alpha_direction == "CONFLICT" and (
        output.alpha_vs_derivatives_alignment == AlphaDerivativesAlignment.AGREE
    ):
        raise AIResearchSafetyError("AI output concealed statistical alpha conflict")
    if "RANK_SIGN_CONFLICT" in alpha.warnings and output.alpha_direction in {
        AlphaAssessmentDirection.BULLISH, AlphaAssessmentDirection.BEARISH
    }:
        raise AIResearchSafetyError("AI output inferred direction from conflicting rank and signed return")
    if research_input.data_quality.intraday_oi_usable:
        return
    text = " ".join(
        output.key_observations
        + output.bullish_evidence
        + output.bearish_evidence
        + output.range_evidence
        + [output.research_summary]
    ).lower()
    forbidden = (
        "strong fresh put writing",
        "strong fresh call writing",
        "confirmed fresh put writing",
        "confirmed fresh call writing",
        "strong fresh unwinding",
        "confirmed fresh unwinding",
    )
    if any(phrase in text for phrase in forbidden):
        raise AIResearchSafetyError(
            "AI output asserted unavailable dynamic OI evidence"
        )


def apply_confidence_cap(
    output: AIResearchModelOutput,
    evidence_quality: str,
    caps: ConfidenceCaps,
) -> tuple[float, bool, str | None]:
    cap = caps.for_quality(evidence_quality)
    if output.confidence <= cap:
        return output.confidence, False, None
    return cap, True, f"{evidence_quality}_EVIDENCE_CONFIDENCE_CAP"


def estimate_cost_usd(
    input_tokens: int | None,
    output_tokens: int | None,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> float | None:
    if (
        input_tokens is None
        or output_tokens is None
        or input_cost_per_million is None
        or output_cost_per_million is None
    ):
        return None
    return round(
        input_tokens / 1_000_000 * input_cost_per_million
        + output_tokens / 1_000_000 * output_cost_per_million,
        8,
    )
