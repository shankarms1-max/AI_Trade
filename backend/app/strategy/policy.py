"""Explicit research defaults, never production tuning or calibrated probabilities."""

from dataclasses import dataclass, field

LOGIC_VERSION = "phase14_2_v1"
STRENGTH_RANK = {"NONE": 0, "WEAK": 1, "MODERATE": 2, "STRONG": 3}


@dataclass(frozen=True)
class CreditSpreadPolicy:
    enabled: bool = False
    theta_carry_enabled: bool = False
    family_mode: str = "BOTH"
    directional_alpha_min_strength: str = "MODERATE"
    theta_carry_max_opposing_alpha_strength: str = "MODERATE"
    strong_directional_score: float = 70
    moderate_directional_score: float = 40
    weak_directional_score: float = 15
    directional_evidence_scale: float = 5
    conflict_min_score: float = 3
    conflict_max_directional_score: float = 25
    strong_min_survival: float = 45
    moderate_min_survival: float = 60
    theta_min_survival: float = 70
    strong_min_carry: float = 20
    moderate_min_carry: float = 30
    theta_min_carry: float = 40
    strong_buffer_multiplier: float = 1
    moderate_buffer_multiplier: float = 1.5
    theta_buffer_multiplier: float = 2
    high_vol_buffer_multiplier: float = 1.5
    elevated_vol_buffer_multiplier: float = 1.25
    low_dte_buffer_multiplier: float = 1.5
    expected_move_source: str = "AUTO"
    expected_move_min_distance_units: float | None = None
    dte_buckets: tuple[float, ...] = (0, 1, 3, 7)
    gamma_extreme_dte: float = 0.25
    gamma_high_dte: float = 1
    gamma_moderate_dte: float = 3
    gamma_extreme_distance_units: float = 0.5
    gamma_high_distance_units: float = 1
    side_safety_min_score: float = 55
    side_safety_min_margin: float = 10
    movement_stress_points: float = 100
    survival_weights: dict[str, float] = field(
        default_factory=lambda: {
            "distance": 0.15,
            "opposing_structure": 0.05,
            "structure": 0.20,
            "expected_move": 0.20,
            "volatility": 0.15,
            "oi_structure": 0.10,
            "liquidity": 0.10,
            "dte": 0.05,
        }
    )
    carry_weights: dict[str, float] = field(
        default_factory=lambda: {
            "credit_width": 0.30,
            "credit_loss": 0.25,
            "credit_dte": 0.15,
            "premium_retention": 0.15,
            "survival_distance": 0.15,
        }
    )
    ranking_weights: dict[str, float] = field(
        default_factory=lambda: {
            "survival": 0.30,
            "carry": 0.25,
            "directional": 0.10,
            "structure": 0.10,
            "liquidity": 0.15,
            "credit": 0.10,
        }
    )
    gamma_penalty: float = 20
    volatility_penalty: float = 10
    max_loss_penalty: float = 10
    cost_penalty: float = 10
    expected_move_penalty: float = 10
    max_loss_scale_per_lot: float = 20000
    carry_credit_width_target: float = 0.15
    carry_credit_loss_target: float = 0.20
    carry_credit_per_day_target: float = 20
    carry_premium_retention_target: float = 0.50
    brokerage_per_order: float = 0
    exchange_rate: float = 0
    stt_rate: float = 0
    gst_rate: float = 0
    stamp_rate: float = 0
    slippage_points_per_leg: float = 0
    costs_complete: bool = False


def policy_from_settings(settings) -> CreditSpreadPolicy:
    return CreditSpreadPolicy(
        **{
            name: getattr(settings, name)
            for name in CreditSpreadPolicy.__dataclass_fields__
            if name not in {"enabled", "theta_carry_enabled", "family_mode"}
        },
        enabled=settings.phase14_2_strategy_logic_enabled,
        theta_carry_enabled=settings.theta_carry_enabled,
        family_mode=settings.strategy_family_mode
    )
