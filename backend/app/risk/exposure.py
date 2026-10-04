from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class RiskState:
    trades_today: int
    realized_pnl_today: float
    open_strategy_keys: frozenset[str]
    authoritative_for_live: bool
    authoritative_for_shadow: bool = False
    provider_kind: str = "CUSTOM"
    monetary_pnl_complete: bool = True


class RiskStateProvider(Protocol):
    def get_state(self, trading_date: date) -> RiskState: ...


class ResearchRiskStateProvider:
    """Explicit zero state for historical research; never claims live authority."""

    def get_state(self, trading_date: date) -> RiskState:
        return RiskState(0, 0.0, frozenset(), False, False, "RESEARCH", True)
