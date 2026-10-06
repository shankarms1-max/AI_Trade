from functools import lru_cache

from datetime import date, time
from math import isfinite

from pydantic import Field, SecretStr, field_validator, model_validator
from app.strategy.policy import CreditSpreadPolicy
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings; secret values stay masked in repr and logs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    app_env: str = "development"
    enable_api_docs: bool = True
    cors_allowed_origins: str = ""
    log_level: str = "INFO"
    timezone: str = Field(default="Asia/Kolkata", validation_alias="TZ")

    kotak_consumer_key: SecretStr
    kotak_mobile_number: SecretStr | None = None
    kotak_ucc: SecretStr | None = None
    kotak_totp_secret: SecretStr | None = None
    kotak_mpin: SecretStr | None = None

    kotak_strike_range: int = Field(default=10, ge=0, le=50)
    kotak_nifty_strike_step: int = Field(default=50, gt=0)
    kotak_option_chain_diagnostics: bool = False

    database_url: SecretStr | None = None
    collector_interval_minutes: int = Field(default=3, ge=1, le=60)
    collector_start_time: time = time(9, 18)
    collector_end_time: time = time(15, 27)
    collector_holidays: str = ""

    feature_top_n: int = Field(default=5, ge=1, le=20)
    feature_min_data_coverage: float = Field(default=0.6, ge=0, le=1)
    feature_cluster_max_fraction: float = Field(default=0.6, gt=0, le=1)
    feature_opening_range_start: time = time(9, 15)
    feature_opening_range_end: time = time(9, 30)
    feature_opening_range_min_samples: int = Field(default=4, ge=2)
    feature_vix_low: float = 12.0
    feature_vix_elevated: float = 16.0
    feature_vix_high: float = 22.0

    regime_min_confidence: float = Field(default=60, ge=0, le=100)
    regime_min_directional_margin: float = Field(default=2.0, ge=0)
    regime_min_contracts: int = Field(default=10, ge=1)
    regime_small_move_pct: float = Field(default=0.05, ge=0)
    regime_pcr_low: float = Field(default=0.8, gt=0)
    regime_pcr_high: float = Field(default=1.2, gt=0)
    regime_price_weight: float = Field(default=3.0, ge=0)
    regime_dynamic_oi_weight: float = Field(default=4.0, ge=0)
    regime_positioning_weight: float = Field(default=4.0, ge=0)
    regime_futures_weight: float = Field(default=2.0, ge=0)
    regime_static_oi_weight: float = Field(default=1.5, ge=0)
    regime_pcr_weight: float = Field(default=1.0, ge=0)
    regime_oi_dependency_cap: float = Field(default=6.0, ge=0)
    regime_low_quality_confidence_cap: float = Field(default=55, ge=0, le=100)
    regime_insufficient_confidence_cap: float = Field(default=30, ge=0, le=100)

    openai_api_key: SecretStr | None = None
    ai_research_model: str | None = None
    ai_max_retries: int = Field(default=1, ge=0, le=3)
    ai_input_cost_per_million: float | None = Field(default=None, ge=0)
    ai_output_cost_per_million: float | None = Field(default=None, ge=0)
    ai_confidence_cap_insufficient: float = Field(default=40, ge=0, le=100)
    ai_confidence_cap_low: float = Field(default=55, ge=0, le=100)
    ai_confidence_cap_medium: float = Field(default=75, ge=0, le=100)
    ai_confidence_cap_high: float = Field(default=90, ge=0, le=100)

    strategy_min_regime_confidence: float = Field(default=60, ge=0, le=100)
    strategy_min_evidence_quality: str = "MEDIUM"
    strategy_require_structure_reference: bool = True
    strategy_min_short_distance_points: float = Field(default=100, ge=0)
    strategy_min_short_distance_pct: float = Field(default=0.25, ge=0)
    strategy_allowed_spread_widths: str = "50,100,150,200"
    strategy_min_short_premium: float = Field(default=1, ge=0)
    strategy_min_net_credit: float = Field(default=1, ge=0)
    strategy_min_credit_to_width_ratio: float = Field(default=0.03, ge=0)
    strategy_min_short_open_interest: int = Field(default=100, ge=0)
    strategy_min_long_open_interest: int = Field(default=100, ge=0)
    strategy_min_short_volume: int = Field(default=1, ge=0)
    strategy_min_long_volume: int = Field(default=1, ge=0)
    strategy_require_usable_volume: bool = False
    strategy_max_bid_ask_spread_pct: float = Field(default=30, ge=0)
    strategy_short_delta_min_abs: float | None = Field(default=None, ge=0, le=1)
    strategy_short_delta_max_abs: float | None = Field(default=None, ge=0, le=1)
    strategy_max_candidates: int = Field(default=5, ge=1, le=50)
    strategy_max_snapshot_age_seconds: int = Field(default=600, ge=1)

    # Phase 14.2 research policy. Legacy selection remains the default.
    phase14_2_strategy_logic_enabled: bool = False
    theta_carry_enabled: bool = False
    strategy_family_mode: str = "BOTH"
    strategy_allowed_widths_points: str = "100,200,300,400"
    directional_alpha_min_strength: str = 'MODERATE'
    theta_carry_max_opposing_alpha_strength: str = 'MODERATE'
    strong_directional_score: float = Field(default=70, ge=0)
    moderate_directional_score: float = Field(default=40, ge=0)
    weak_directional_score: float = Field(default=15, ge=0)
    directional_evidence_scale: float = Field(default=5, gt=0)
    conflict_min_score: float = Field(default=3, ge=0)
    conflict_max_directional_score: float = Field(default=25, ge=0, le=100)
    strong_min_survival: float = Field(default=45, ge=0)
    moderate_min_survival: float = Field(default=60, ge=0)
    theta_min_survival: float = Field(default=70, ge=0, le=100, validation_alias="THETA_CARRY_MIN_SURVIVAL_SCORE")
    strong_min_carry: float = Field(default=20, ge=0)
    moderate_min_carry: float = Field(default=30, ge=0)
    theta_min_carry: float = Field(default=40, ge=0, le=100, validation_alias="THETA_CARRY_MIN_CARRY_SCORE")
    strong_buffer_multiplier: float = Field(default=1, ge=0)
    moderate_buffer_multiplier: float = Field(default=1.5, ge=0)
    theta_buffer_multiplier: float = Field(default=2, ge=0)
    high_vol_buffer_multiplier: float = Field(default=1.5, ge=0)
    elevated_vol_buffer_multiplier: float = Field(default=1.25, ge=0)
    low_dte_buffer_multiplier: float = Field(default=1.5, ge=0)
    expected_move_source: str = 'AUTO'
    expected_move_min_distance_units: float | None = None
    dte_buckets: tuple[float, ...] = Field(default=(0, 1, 3, 7), validation_alias="GAMMA_RISK_DTE_BUCKETS")
    gamma_extreme_dte: float = Field(default=0.25, ge=0)
    gamma_high_dte: float = Field(default=1, ge=0)
    gamma_moderate_dte: float = Field(default=3, ge=0)
    gamma_extreme_distance_units: float = Field(default=0.5, ge=0)
    gamma_high_distance_units: float = Field(default=1, ge=0)
    side_safety_min_score: float = Field(default=55, ge=0)
    side_safety_min_margin: float = Field(default=10, ge=0)
    movement_stress_points: float = Field(default=100, ge=0)
    survival_weights: dict[str, float] = Field(validation_alias="SURVIVAL_SCORE_WEIGHTS", default_factory=lambda: CreditSpreadPolicy().survival_weights)
    carry_weights: dict[str, float] = Field(validation_alias="CARRY_SCORE_WEIGHTS", default_factory=lambda: CreditSpreadPolicy().carry_weights)
    ranking_weights: dict[str, float] = Field(default_factory=lambda: CreditSpreadPolicy().ranking_weights)
    gamma_penalty: float = Field(default=20, ge=0)
    volatility_penalty: float = Field(default=10, ge=0)
    max_loss_penalty: float = Field(default=10, ge=0)
    cost_penalty: float = Field(default=10, ge=0)
    expected_move_penalty: float = Field(default=10, ge=0)
    max_loss_scale_per_lot: float = Field(default=20000, ge=0)
    carry_credit_width_target: float = Field(default=0.15, ge=0)
    carry_credit_loss_target: float = Field(default=0.2, ge=0)
    carry_credit_per_day_target: float = Field(default=20, ge=0)
    carry_premium_retention_target: float = Field(default=0.5, ge=0)
    brokerage_per_order: float = Field(default=0, ge=0)
    exchange_rate: float = Field(default=0, ge=0)
    stt_rate: float = Field(default=0, ge=0)
    gst_rate: float = Field(default=0, ge=0)
    stamp_rate: float = Field(default=0, ge=0)
    slippage_points_per_leg: float = Field(default=0, ge=0)
    costs_complete: bool = False

    @model_validator(mode="after")
    def validate_credit_spread_policy(self):
        from app.strategy.policy import STRENGTH_RANK
        if self.strategy_family_mode not in {"BOTH", "DIRECTIONAL_ONLY", "THETA_CARRY_ONLY"}:
            raise ValueError("invalid STRATEGY_FAMILY_MODE")
        for value in (self.directional_alpha_min_strength, self.theta_carry_max_opposing_alpha_strength):
            if value not in STRENGTH_RANK:
                raise ValueError("invalid alpha strength policy")
        if self.expected_move_source not in {"AUTO", "VIX", "ATM_STRADDLE"}:
            raise ValueError("invalid EXPECTED_MOVE_SOURCE")
        for name in ("survival_weights", "carry_weights", "ranking_weights"):
            values = getattr(self, name)
            from app.strategy.policy import CreditSpreadPolicy
            if set(values) != set(getattr(CreditSpreadPolicy(), name)) or any(not isfinite(v) or v < 0 for v in values.values()) or sum(values.values()) <= 0:
                raise ValueError(f"invalid {name}")
        if not 0 <= self.weak_directional_score <= self.moderate_directional_score <= self.strong_directional_score <= 100:
            raise ValueError("directional score thresholds must be ordered within 0..100")
        if not self.dte_buckets or any(not isfinite(v) for v in self.dte_buckets) or tuple(sorted(set(self.dte_buckets))) != self.dte_buckets or self.dte_buckets[0] < 0:
            raise ValueError("DTE buckets must be nonnegative and strictly increasing")
        if not 0 <= self.gamma_extreme_dte <= self.gamma_high_dte <= self.gamma_moderate_dte:
            raise ValueError("gamma DTE thresholds must be ordered")
        if self.expected_move_min_distance_units is not None and self.expected_move_min_distance_units < 0:
            raise ValueError("expected move minimum must be nonnegative")
        for name in ("max_loss_scale_per_lot", "carry_credit_width_target", "carry_credit_loss_target", "carry_credit_per_day_target", "carry_premium_retention_target", "movement_stress_points"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("strong_buffer_multiplier", "moderate_buffer_multiplier", "theta_buffer_multiplier", "high_vol_buffer_multiplier", "elevated_vol_buffer_multiplier", "low_dte_buffer_multiplier"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.strong_buffer_multiplier <= self.moderate_buffer_multiplier <= self.theta_buffer_multiplier:
            raise ValueError("strike distance multipliers must increase with safety requirements")
        return self

    @property
    def active_regime_version(self):
        return "phase14_2_v1" if self.phase14_2_strategy_logic_enabled else "phase4_v1"

    @property
    def active_strategy_version(self):
        return "phase14_2_v1" if self.phase14_2_strategy_logic_enabled else "phase6_v1"

    risk_max_loss_per_trade: float | None = Field(default=None, gt=0)
    risk_max_capital_per_trade: float | None = Field(default=None, gt=0)
    risk_max_trades_per_day: int = Field(default=1, ge=0)
    risk_max_daily_loss: float | None = Field(default=None, gt=0)
    risk_max_spread_width: float = Field(default=200, gt=0)
    risk_min_net_credit: float = Field(default=1, ge=0)
    risk_min_credit_to_width: float = Field(default=0.03, ge=0)
    risk_min_short_oi: int = Field(default=100, ge=0)
    risk_min_long_oi: int = Field(default=100, ge=0)
    risk_min_short_volume: int = Field(default=1, ge=0)
    risk_min_long_volume: int = Field(default=1, ge=0)
    risk_require_volume: bool = True
    risk_max_bid_ask_spread_pct: float = Field(default=30, ge=0)
    risk_entry_start_time: time = time(9, 35)
    risk_entry_end_time: time = time(13, 30)
    risk_min_regime_confidence: float = Field(default=60, ge=0, le=100)
    risk_min_evidence_quality: str = "MEDIUM"
    risk_require_bid_ask: bool = False
    risk_allow_ltp_estimate: bool = True
    risk_max_snapshot_age_seconds: int = Field(default=600, ge=1)
    risk_allow_expiry_day: bool = False
    risk_require_intraday_oi: bool = True
    risk_required_consecutive_directional_snapshots: int = Field(default=2, ge=1, le=20)
    risk_require_candidate_stability: bool = False
    risk_candidate_stability_snapshots: int = Field(default=2, ge=2, le=20)
    risk_market_events_json: str = "[]"

    shadow_profit_target_credit_capture_pct: float = Field(default=50, gt=0, le=100)
    shadow_stop_loss_credit_multiple: float = Field(default=1.5, gt=0)
    shadow_force_exit_time: time = time(15, 20)
    shadow_hard_exit_cutoff: time = time(15, 29)
    shadow_exit_on_opposite_regime: bool = True
    shadow_exit_on_structural_breach: bool = True
    shadow_max_new_trades_per_day: int = Field(default=1, ge=1)
    shadow_allow_multiple_open_trades: bool = False
    shadow_risk_capital_base: float | None = Field(default=None, gt=0)
    shadow_risk_max_loss_per_trade: float | None = Field(default=None, gt=0)
    shadow_risk_max_capital_per_trade: float | None = Field(default=None, gt=0)
    shadow_risk_max_daily_loss: float | None = Field(default=None, gt=0)
    shadow_risk_max_trades_per_day: int = Field(default=1, ge=0)

    pipeline_after_snapshot: bool = False
    pipeline_run_ai_research: bool = False
    # Explicit forward research rehearsal: persist decisions, never create/mark fills.
    # This does not enable the separate authoritative offline replay engine.
    pipeline_decision_only: bool = False

    # Phase 14 is deliberately disabled until live interval-volume validation.
    alpha_engine_enabled: bool = False
    regime_use_statistical_alpha: bool = False
    alpha_price_source: str = "FUTURE"
    alpha_price_horizon_seconds: int = Field(default=300, ge=60, le=3600)
    alpha_horizon_tolerance_seconds: int = Field(default=120, ge=0, le=1800)
    alpha_min_horizon_seconds: int = Field(default=240, ge=1, le=3600)
    alpha_max_horizon_seconds: int = Field(default=420, ge=1, le=3600)
    alpha_lookback_clock_mode: str = "TRADING_MINUTES"
    alpha_hypothesis_type: str = "CONTINUATION"
    alpha1_lookback_minutes: int = Field(default=800, ge=30)
    alpha2_lookback_minutes: int = Field(default=300, ge=30)
    alpha_volume_lookback_minutes: int = Field(default=300, ge=30)
    alpha_volatility_lookback_minutes: int = Field(default=300, ge=30)
    alpha_min_rank_observations: int = Field(default=50, ge=2)
    alpha_min_volume_observations: int = Field(default=5, ge=1)
    alpha_min_volatility_returns: int = Field(default=5, ge=2)
    alpha_max_sequence_gap_seconds: int = Field(default=600, ge=60)
    volume_max_interval_seconds: int = Field(default=420, ge=60)
    volatility_min_valid_level: float = Field(default=0.00001, gt=0)
    alpha_activity_baseline_method: str = "MEDIAN"
    alpha_activity_min_baseline: float = Field(default=1.0, gt=0)
    alpha_activity_ratio_cap: float = Field(default=10.0, gt=0)
    alpha_atm_max_distance_percent: float = Field(default=0.5, gt=0)
    alpha_volatility_epsilon: float = Field(default=1e-8, gt=0)
    alpha_strong_upper: float = Field(default=0.80, ge=0, le=1)
    alpha_strong_lower: float = Field(default=0.20, ge=0, le=1)
    alpha_moderate_upper: float = Field(default=0.70, ge=0, le=1)
    alpha_moderate_lower: float = Field(default=0.30, ge=0, le=1)
    alpha_required_consecutive_confirmations: int = Field(default=2, ge=1, le=20)
    alpha_stale_after_seconds: int = Field(default=600, ge=60)

    regime_alpha1_weight: float = Field(default=4.0, ge=0)
    regime_alpha2_weight: float = Field(default=4.0, ge=0)
    regime_statistical_alpha_cap: float = Field(default=5.0, ge=0)
    regime_price_movement_cap: float = Field(default=5.0, ge=0)
    regime_basis_weight: float = Field(default=1.0, ge=0)
    regime_require_alpha_for_directional: bool = True
    regime_alpha_contradiction_confidence_penalty: float = Field(default=25.0, ge=0, le=100)

    strategy_volatility_buffer_enabled: bool = False
    strategy_vix_low_distance_multiplier: float = Field(default=1.0, ge=1)
    strategy_vix_normal_distance_multiplier: float = Field(default=1.0, ge=1)
    strategy_vix_elevated_distance_multiplier: float = Field(default=1.25, ge=1)
    strategy_vix_high_distance_multiplier: float = Field(default=1.5, ge=1)
    strategy_no_candidate_on_high_vix: bool = False

    research_min_trades_for_evaluation: int = Field(default=30, ge=1)
    research_max_experiment_combinations: int = Field(default=100, ge=1, le=1000)
    phase14_2_1_replay_integrity_enabled: bool = False
    replay_allow_unknown_depth: bool = False
    replay_allow_0dte: bool = False
    replay_expected_interval_seconds: int = Field(default=180, gt=0)
    replay_interval_tolerance_seconds: int = Field(default=30, ge=0)
    replay_max_fill_delay_seconds: int = Field(default=240, gt=0)
    replay_max_path_gap_seconds: int = Field(default=240, gt=0)

    obs_collector_worker_id: str = "collector-main"
    obs_collector_heartbeat_seconds: int = Field(default=60, ge=10, le=3600)
    obs_expected_snapshot_interval_seconds: int = Field(default=180, ge=30, le=3600)
    obs_data_fresh_healthy_seconds: int = Field(default=300, ge=1)
    obs_data_fresh_degraded_seconds: int = Field(default=600, ge=1)
    obs_pipeline_degraded_fraction: float = Field(default=0.5, gt=0, le=1)
    log_dir: str = "logs"
    log_max_bytes: int = Field(default=10_485_760, ge=1024)
    log_backup_count: int = Field(default=7, ge=1, le=100)

    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    telegram_timeout_seconds: float = Field(default=10, gt=0, le=60)
    telegram_max_retries: int = Field(default=2, ge=0, le=5)
    telegram_notify_operational: bool = True
    telegram_notify_regime: bool = True
    telegram_notify_strategy: bool = True
    telegram_notify_risk: bool = True
    telegram_notify_shadow: bool = True
    telegram_notify_daily_summary: bool = True
    telegram_notify_range: bool = False
    telegram_send_startup_message: bool = False
    telegram_daily_summary_time: time = time(15, 40)

    @field_validator("kotak_consumer_key")
    @classmethod
    def consumer_key_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("KOTAK_CONSUMER_KEY is required")
        return value

    @field_validator("database_url")
    @classmethod
    def database_url_must_not_be_blank(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("openai_api_key")
    @classmethod
    def openai_api_key_must_not_be_blank(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("telegram_bot_token", "telegram_chat_id")
    @classmethod
    def telegram_secret_must_not_be_blank(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("ai_research_model")
    @classmethod
    def ai_research_model_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("strategy_min_evidence_quality", "risk_min_evidence_quality")
    @classmethod
    def strategy_evidence_quality_is_valid(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"INSUFFICIENT", "LOW", "MEDIUM", "HIGH"}:
            raise ValueError("STRATEGY_MIN_EVIDENCE_QUALITY is invalid")
        return normalized

    @field_validator("alpha_price_source")
    @classmethod
    def alpha_price_source_is_valid(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"SPOT", "FUTURE"}:
            raise ValueError("ALPHA_PRICE_SOURCE must be SPOT or FUTURE")
        return normalized

    @field_validator("alpha_lookback_clock_mode")
    @classmethod
    def alpha_clock_is_valid(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"WALL_CLOCK", "TRADING_MINUTES", "SESSION_ONLY"}:
            raise ValueError("ALPHA_LOOKBACK_CLOCK_MODE is invalid")
        return normalized

    @field_validator("alpha_hypothesis_type")
    @classmethod
    def alpha_hypothesis_is_valid(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CONTINUATION", "REVERSAL"}:
            raise ValueError("ALPHA_HYPOTHESIS_TYPE is invalid")
        return normalized

    @field_validator("alpha_activity_baseline_method")
    @classmethod
    def alpha_activity_method_is_valid(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"MEDIAN", "TRIMMED_MEAN"}:
            raise ValueError("ALPHA_ACTIVITY_BASELINE_METHOD is invalid")
        return normalized

    @property
    def configured_strategy_widths(self) -> tuple[float, ...]:
        source = self.strategy_allowed_widths_points if self.phase14_2_strategy_logic_enabled else self.strategy_allowed_spread_widths
        widths = tuple(float(part.strip()) for part in source.split(",") if part.strip())
        if not widths or any(not isfinite(width) or width <= 0 for width in widths):
            raise ValueError("STRATEGY_ALLOWED_SPREAD_WIDTHS must contain positive numbers")
        return tuple(sorted(set(widths)))

    @property
    def configured_holidays(self) -> frozenset[date]:
        values = (part.strip() for part in self.collector_holidays.split(","))
        return frozenset(date.fromisoformat(value) for value in values if value)

    @property
    def configured_cors_origins(self) -> tuple[str, ...]:
        local = ("http://localhost:3000", "http://127.0.0.1:3000")
        extra = tuple(
            value.strip().rstrip("/")
            for value in self.cors_allowed_origins.split(",")
            if value.strip()
        )
        if "*" in extra:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain a wildcard")
        return tuple(dict.fromkeys((*local, *extra)))


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

