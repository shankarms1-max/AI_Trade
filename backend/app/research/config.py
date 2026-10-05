from dataclasses import dataclass
from datetime import time
from app.research.quotes import QuotePolicy, finite
from app.features.engine import FeatureEngineConfig


@dataclass(frozen=True)
class ReplayIntegrityConfig:
    enabled: bool = False
    quote_policy: QuotePolicy = QuotePolicy()
    feature_engine_config: FeatureEngineConfig = FeatureEngineConfig()
    expected_interval_seconds: int = 180
    interval_tolerance_seconds: int = 30
    max_fill_delay_seconds: int = 240
    max_path_gap_seconds: int = 240
    session_start: time = time(9, 18)
    session_end: time = time(15, 27)
    allow_0dte: bool = False

    def __post_init__(self):
        if (self.expected_interval_seconds <= 0 or self.interval_tolerance_seconds < 0
                or self.interval_tolerance_seconds >= self.expected_interval_seconds / 2
                or self.max_fill_delay_seconds < self.expected_interval_seconds
                or self.max_path_gap_seconds < self.expected_interval_seconds
                or self.session_start >= self.session_end):
            raise ValueError("invalid research cadence/TTL/session policy")
        if self.allow_0dte:
            raise ValueError("0DTE is excluded from the initial authoritative replay policy")


def replay_configuration(parameters, strategy, risk, regime, shadow, integrity, quantity=1):
    from app.research.manifest import json_value
    return json_value({"parameters": parameters, "strategy": strategy, "risk": risk,
                       "regime": regime, "shadow": shadow, "integrity": integrity, "requested_lots": quantity})


def validate_replay_parameters(parameters, strategy, shadow):
    numeric = (parameters.alpha_threshold, parameters.short_strike_buffer, parameters.minimum_credit_to_width,
               *parameters.spread_widths)
    optional = (parameters.expected_move_min_distance_units, parameters.minimum_carry_score,
                parameters.profit_target_credit_capture_pct, parameters.stop_loss_credit_multiple)
    if (any(not finite(value) for value in numeric)
            or any(value is not None and not finite(value) for value in optional)
            or not .5 < parameters.alpha_threshold < 1
            or not parameters.spread_widths or any(width <= 0 for width in parameters.spread_widths)
            or parameters.short_strike_buffer < 0 or parameters.minimum_credit_to_width <= 0
            or parameters.entry_start > parameters.entry_end
            or parameters.expected_move_min_distance_units is not None and parameters.expected_move_min_distance_units < 0
            or parameters.minimum_carry_score is not None and not 0 <= parameters.minimum_carry_score <= 100
            or parameters.profit_target_credit_capture_pct is not None and not 0 < parameters.profit_target_credit_capture_pct <= 100
            or parameters.stop_loss_credit_multiple is not None and parameters.stop_loss_credit_multiple <= 0
            or parameters.strategy_family_mode not in {None, "BOTH", "DIRECTIONAL_ONLY", "THETA_CARRY_ONLY"}
            or parameters.directional_min_strength not in {None, "MODERATE", "STRONG"}
            or not finite(shadow.profit_target_credit_capture_pct) or not 0 < shadow.profit_target_credit_capture_pct <= 100
            or not finite(shadow.stop_loss_credit_multiple) or shadow.stop_loss_credit_multiple <= 0):
        raise ValueError("INVALID_RESEARCH_PARAMETERS")
    boundaries = strategy.credit_spread_policy.dte_buckets
    allowed = {f"LE_{boundary:g}_DTE" for boundary in boundaries if boundary > 0} | {f"GT_{boundaries[-1]:g}_DTE"}
    if parameters.dte_bucket is not None and parameters.dte_bucket not in allowed:
        raise ValueError("INACTIVE_OR_INVALID_DTE_BUCKET")
