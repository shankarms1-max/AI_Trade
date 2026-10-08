from dataclasses import dataclass
from app.strategy.policy import CreditSpreadPolicy
from datetime import time


@dataclass(frozen=True)
class RiskConfig:
    credit_spread_policy: CreditSpreadPolicy = CreditSpreadPolicy()
    capital_base: float | None = None
    max_loss_per_trade: float | None = None
    max_capital_per_trade: float | None = None
    max_trades_per_day: int = 1
    max_daily_loss: float | None = None
    max_spread_width: float | None = None
    min_net_credit: float = 1
    min_credit_to_width: float = 0.03
    min_short_oi: int = 100
    min_long_oi: int = 100
    min_short_volume: int = 1
    min_long_volume: int = 1
    require_volume: bool = True
    max_bid_ask_spread_pct: float = 30
    entry_start_time: time = time(9, 35)
    entry_end_time: time = time(13, 30)
    min_regime_confidence: float = 60
    min_evidence_quality: str = "MEDIUM"
    require_bid_ask: bool = False
    allow_ltp_estimate: bool = True
    max_snapshot_age_seconds: int = 600
    allow_expiry_day: bool = False
    require_intraday_oi: bool = True
    required_consecutive_directional_snapshots: int = 2
    require_candidate_stability: bool = False
    candidate_stability_snapshots: int = 2
