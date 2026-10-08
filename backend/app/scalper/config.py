"""Validated, allowlisted scalper configuration."""
from dataclasses import asdict, dataclass
from datetime import time


def parse_widths(value: str) -> tuple[int, ...]:
    try:
        widths = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise ValueError("SCALPER_ALLOWED_WIDTHS_INVALID") from exc
    if not widths or any(item <= 0 or item % 50 for item in widths):
        raise ValueError("SCALPER_ALLOWED_WIDTHS_INVALID")
    return widths


@dataclass(frozen=True)
class ScalperConfig:
    interval_seconds: int
    start_time: time
    entry_end_time: time
    forced_exit_time: time
    strike_range: int
    feature_lookback: int
    signal_min_score: float
    min_confirmations: int
    allowed_widths: tuple[int, ...]
    min_short_distance_points: float
    min_credit: float
    min_credit_to_width: float
    max_bid_ask_spread_pct: float
    max_quote_age_seconds: int
    min_open_interest: int
    min_volume: int
    max_open_positions: int
    lots: int
    max_trades_per_day: int
    max_loss_per_trade: float
    hard_daily_loss: float
    max_consecutive_losses: int
    cooldown_after_loss_seconds: int
    cooldown_after_exit_seconds: int
    kill_switch: bool
    event_configuration_json: str
    profit_capture_pct: float
    stop_credit_multiple: float
    trailing_activation_pct: float
    trailing_giveback_pct: float
    time_stop_minutes: int
    entry_ttl_seconds: int
    use_phase14_context: bool
    trend_aligned_min_score: float = 68
    mixed_min_score: float | None = None
    countertrend_min_score: float = 85

    @property
    def mixed_threshold(self) -> float:
        return self.signal_min_score if self.mixed_min_score is None else self.mixed_min_score

    def signal_policy(self) -> dict:
        return dict(min_score=self.signal_min_score,
                    trend_aligned_min_score=self.trend_aligned_min_score,
                    mixed_min_score=self.mixed_threshold,
                    countertrend_min_score=self.countertrend_min_score,
                    min_confirmations=self.min_confirmations,
                    max_confirmation_gap_seconds=self.interval_seconds * 1.5)

    @classmethod
    def from_settings(cls, settings) -> "ScalperConfig":
        config = cls(
            interval_seconds=settings.scalper_interval_seconds,
            start_time=settings.scalper_start_time,
            entry_end_time=settings.scalper_entry_end_time,
            forced_exit_time=settings.scalper_forced_exit_time,
            strike_range=settings.scalper_strike_range,
            feature_lookback=settings.scalper_feature_lookback,
            signal_min_score=settings.scalper_signal_min_score,
            min_confirmations=settings.scalper_min_confirmations,
            allowed_widths=parse_widths(settings.scalper_allowed_widths),
            min_short_distance_points=settings.scalper_min_short_distance_points,
            min_credit=settings.scalper_min_credit,
            min_credit_to_width=settings.scalper_min_credit_to_width,
            max_bid_ask_spread_pct=settings.scalper_max_bid_ask_spread_pct,
            max_quote_age_seconds=settings.scalper_max_quote_age_seconds,
            min_open_interest=settings.scalper_min_open_interest,
            min_volume=settings.scalper_min_volume,
            max_open_positions=settings.scalper_max_open_positions,
            lots=settings.scalper_lots,
            max_trades_per_day=settings.scalper_max_trades_per_day,
            max_loss_per_trade=settings.scalper_max_loss_per_trade,
            hard_daily_loss=settings.scalper_hard_daily_loss,
            max_consecutive_losses=settings.scalper_max_consecutive_losses,
            cooldown_after_loss_seconds=settings.scalper_cooldown_after_loss_seconds,
            cooldown_after_exit_seconds=settings.scalper_cooldown_after_exit_seconds,
            kill_switch=settings.scalper_kill_switch,
            event_configuration_json=settings.scalper_market_events_json,
            profit_capture_pct=settings.scalper_profit_capture_pct,
            stop_credit_multiple=settings.scalper_stop_credit_multiple,
            trailing_activation_pct=settings.scalper_trailing_activation_pct,
            trailing_giveback_pct=settings.scalper_trailing_giveback_pct,
            time_stop_minutes=settings.scalper_time_stop_minutes,
            entry_ttl_seconds=settings.scalper_entry_ttl_seconds,
            use_phase14_context=settings.scalper_use_phase14_context,
            trend_aligned_min_score=settings.scalper_trend_aligned_min_score,
            mixed_min_score=settings.scalper_mixed_min_score,
            countertrend_min_score=settings.scalper_countertrend_min_score,
        )
        if not config.start_time < config.entry_end_time < config.forced_exit_time:
            raise ValueError("SCALPER_SESSION_TIMES_INVALID")
        if not (config.trend_aligned_min_score < config.mixed_threshold
                <= config.countertrend_min_score):
            raise ValueError("SCALPER_ADAPTIVE_THRESHOLDS_INVALID")
        return config

    def payload(self) -> dict:
        return asdict(self)
