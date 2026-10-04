from app.strategy.models import CreditSpreadCandidate


def validate_candidate(candidate: CreditSpreadCandidate) -> CreditSpreadCandidate:
    """Revalidate strict defined-risk invariants at a persistence boundary."""
    return CreditSpreadCandidate.model_validate(candidate.model_dump())
