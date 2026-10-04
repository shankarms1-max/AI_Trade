from datetime import date


def operational_key(event_id: int, event_code: str) -> str:
    return f"OPERATIONAL:{event_id}:{event_code}"


def regime_key(snapshot_id: int, regime: str) -> str:
    return f"REGIME_CONFIRMED:{snapshot_id}:{regime}"


def candidate_key(snapshot_id: int, fingerprint: str) -> str:
    return f"STRATEGY_CANDIDATE:{snapshot_id}:{fingerprint}"


def risk_key(risk_decision_id: int) -> str:
    return f"RISK_DECISION:{risk_decision_id}"


def shadow_entry_key(shadow_trade_id: int) -> str:
    return f"SHADOW_ENTRY:{shadow_trade_id}"


def shadow_exit_key(shadow_trade_id: int) -> str:
    return f"SHADOW_EXIT:{shadow_trade_id}"


def daily_key(day: date) -> str:
    return f"DAILY_SUMMARY:{day.isoformat()}"
