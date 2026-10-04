QUALITY_RANK = {"INSUFFICIENT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def evidence_at_least(actual: str, required: str) -> bool:
    return QUALITY_RANK.get(actual, -1) >= QUALITY_RANK.get(required, 99)
