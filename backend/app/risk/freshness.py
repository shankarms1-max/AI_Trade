from datetime import datetime

from app.risk.models import EvaluationContext


def snapshot_age_seconds(
    observed_at: datetime, evaluated_at: datetime, context: EvaluationContext
) -> float | None:
    if context == EvaluationContext.HISTORICAL:
        return None
    return (evaluated_at - observed_at).total_seconds()
