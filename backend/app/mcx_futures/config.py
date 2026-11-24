"""Versioned, hashed per-instrument configuration for ACTS_V3_MCX_FUTURES.

Both configs are ``PRE_REGISTERED_UNVALIDATED``: no historical MCX data was
available, so every threshold below is a pre-registered design choice to be
tested by ``research.py``, not a calibrated value.  Nothing is inherited from
the NIFTY options strategy except the *intent* of a 100-point evidence score.

Entry score floor 60 (both): a design choice for this NEW score, not inherited
from NIFTY ACTS; research.py grids it over {55, 60, 65, 70}.

Why NATGASMINI differs from CRUDEOILM (natural gas is the noisier, gappier,
more event-driven contract, quoted to 0.10 with a 250 MMBTU lot):
- 5-minute feature bars (crude 3): fewer, less noisy decisions.
- 2 confirmation bars (crude 1) and an 88 exceptional-reversal floor (crude 85)
  with 600 s (crude 360 s) confirmation: fewer whipsaw entries/flips.
- Stop 2.0 x ATR (crude 1.5), trail activation 1.5 / trail 2.0 ATR (crude 1.0 /
  1.5), pullback 0.75 ATR (crude 0.5), gap guard 2.0 ATR for 20 min (crude 1.5
  ATR for 15 min): a wider noise envelope.
- Stricter regime filters: chop ER < 0.35 (crude 0.30), ADX 20..38 (crude
  18..35), volatility band 15th..85th percentile (crude 10th..90th).
- Risk: Rs 4,000 per trade / Rs 8,000 per day (crude Rs 2,500 / Rs 6,000),
  3 entries and 2 consecutive losses per session (crude 4 / 3), cooldowns 10 /
  20 min (crude 5 / 15), max hold 180 min (crude 120).
- Spread limit 4 ticks = Rs 0.40 (crude 3 ticks = Rs 3.00).
- Event blackout: EIA natural-gas storage report, Thursdays 10:30 ET, 10 min
  before to 20 min after (crude: EIA petroleum status report, Wednesdays 10:30
  ET, 10 before / 15 after).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
from datetime import date, datetime, time, timedelta
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from app.mcx_futures.spec import IST

STRATEGY_ID = "ACTS_V3_MCX_FUTURES"
NEW_YORK = ZoneInfo("America/New_York")
SCORE_COMPONENTS = ("trend", "momentum", "structure", "strength", "regime", "execution")


@dataclass(frozen=True)
class ScheduledEvent:
    """A recurring US release defined in New York local time (DST handled by zoneinfo).

    ``shifted`` maps an original New-York date (ISO) to a replacement New-York
    datetime (ISO) or "" to cancel it (holiday-shifted releases); ``extra`` adds
    one-off New-York datetimes.
    """

    name: str
    weekday: int
    time_et: time
    minutes_before: int
    minutes_after: int
    source: str
    shifted: tuple[tuple[str, str], ...] = ()
    extra: tuple[str, ...] = ()

    def occurrences_near(self, on: date) -> list[datetime]:
        shifted = dict(self.shifted)
        out: list[datetime] = []
        for offset in (-1, 0, 1):
            day = on + timedelta(days=offset)
            if day.weekday() != self.weekday:
                continue
            if day.isoformat() in shifted:
                replacement = shifted[day.isoformat()]
                if replacement:
                    out.append(datetime.fromisoformat(replacement).replace(tzinfo=NEW_YORK))
                continue
            out.append(datetime.combine(day, self.time_et, NEW_YORK))
        for item in self.extra:
            moment = datetime.fromisoformat(item).replace(tzinfo=NEW_YORK)
            if abs((moment.date() - on).days) <= 1:
                out.append(moment)
        return out

    def window_containing(self, at: datetime) -> tuple[datetime, datetime] | None:
        ny_date = at.astimezone(NEW_YORK).date()
        for moment in self.occurrences_near(ny_date):
            start = moment - timedelta(minutes=self.minutes_before)
            end = moment + timedelta(minutes=self.minutes_after)
            if start <= at < end:
                return start.astimezone(IST), end.astimezone(IST)
        return None


@dataclass(frozen=True)
class InstrumentConfig:
    instrument: str
    version: str
    calibration_status: str
    quotation_unit: str
    # Bars and features ------------------------------------------------------
    feature_timeframe_minutes: int
    feature_window_bars: int
    ema_fast: int
    ema_slow: int
    slope_bars: int
    atr_period: int
    adx_period: int
    breakout_lookback: int
    er_lookback: int
    atr_percentile_window: int
    atr_percentile_min_samples: int
    momentum_bars: int
    pullback_bars: int
    # Score -------------------------------------------------------------------
    weights: dict[str, float]
    slope_full_atr: float
    momentum_full_atr: float
    breakout_full_atr: float
    vwap_full_atr: float
    adx_floor: float
    adx_full: float
    chop_er_threshold: float
    trend_er_full: float
    vol_percentile_low: float
    vol_percentile_high: float
    entry_score_floor: float
    exceptional_reversal_score_floor: float
    opposite_exit_score_floor: float
    confirmation_bars: int
    # Opportunity cycle -------------------------------------------------------
    pullback_atr: float
    setup_lifetime_minutes: int
    reversal_confirmation_seconds: int
    observation_interval_seconds: int
    # Risk ----------------------------------------------------------------------
    lots: int
    stop_atr_multiple: float
    min_stop_ticks: int
    max_risk_per_trade: float
    daily_loss_limit: float
    max_entries_per_session: int
    max_consecutive_losses: int
    cooldown_after_exit_minutes: int
    cooldown_after_loss_minutes: int
    trail_activation_atr: float
    trail_atr: float
    max_holding_minutes: int
    gap_guard_atr: float
    gap_guard_minutes: int
    expiry_window_days: int
    last_entry_minutes_before_close: int
    forced_exit_minutes_before_close: int
    # Execution -----------------------------------------------------------------
    max_spread_ticks: float
    full_credit_spread_ticks: float
    stale_quote_seconds: float
    stale_exit_seconds: float
    max_entry_latency_seconds: float
    stop_slippage_ticks: int
    entry_slippage_ticks: int
    # Events ----------------------------------------------------------------------
    events: tuple[ScheduledEvent, ...] = field(default=())
    notes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if set(self.weights) != set(SCORE_COMPONENTS) or abs(sum(self.weights.values()) - 100) > 1e-9:
            raise ValueError("SCORE_WEIGHTS_MUST_COVER_COMPONENTS_AND_SUM_TO_100")
        if any(value < 0 for value in self.weights.values()):
            raise ValueError("SCORE_WEIGHTS_NEGATIVE")
        if not 0 < self.ema_fast < self.ema_slow:
            raise ValueError("EMA_PERIODS_INVALID")
        if self.feature_timeframe_minutes not in (1, 2, 3, 5, 10, 15):
            raise ValueError("FEATURE_TIMEFRAME_INVALID")
        if not 0 < self.entry_score_floor <= self.exceptional_reversal_score_floor <= 100:
            raise ValueError("SCORE_FLOORS_INVALID")
        if not 0 <= self.vol_percentile_low < self.vol_percentile_high <= 1:
            raise ValueError("VOL_BAND_INVALID")
        if self.adx_full <= self.adx_floor:
            raise ValueError("ADX_BAND_INVALID")
        if self.lots < 1 or self.stop_atr_multiple <= 0 or self.min_stop_ticks < 1:
            raise ValueError("RISK_SIZING_INVALID")
        if self.max_risk_per_trade <= 0 or self.daily_loss_limit <= 0:
            raise ValueError("RISK_LIMITS_INVALID")
        if not 0 <= self.forced_exit_minutes_before_close < self.last_entry_minutes_before_close:
            raise ValueError("SESSION_CUTOFFS_INVALID")
        if self.stale_exit_seconds < self.stale_quote_seconds:
            raise ValueError("STALE_EXIT_BEFORE_STALE_QUOTE")
        if self.full_credit_spread_ticks >= self.max_spread_ticks:
            raise ValueError("SPREAD_BAND_INVALID")
        if self.feature_window_bars < self._warmup():
            raise ValueError("FEATURE_WINDOW_SHORTER_THAN_WARMUP")

    def _warmup(self) -> int:
        from app.mcx_futures.features import warmup_bars

        return warmup_bars(self)

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = [
            {**asdict(event), "time_et": event.time_et.isoformat(), "shifted": [list(x) for x in event.shifted],
             "extra": list(event.extra)}
            for event in self.events
        ]
        data["notes"] = list(self.notes)
        return data

    @cached_property
    def config_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @cached_property
    def config_id(self) -> str:
        return f"{STRATEGY_ID}:{self.instrument}:{self.version}:{self.config_hash[:12]}"

    def with_overrides(self, **changes: Any) -> InstrumentConfig:
        return replace(self, **changes)

    @property
    def bar_seconds(self) -> int:
        return self.feature_timeframe_minutes * 60

    def blackout(self, at: datetime) -> dict[str, Any] | None:
        for event in self.events:
            window = event.window_containing(at)
            if window:
                return {"event": event.name, "start": window[0].isoformat(), "end": window[1].isoformat(),
                        "source": event.source}
        return None


EIA_WPSR = ScheduledEvent(
    name="EIA_WEEKLY_PETROLEUM_STATUS_REPORT",
    weekday=2,
    time_et=time(10, 30),
    minutes_before=10,
    minutes_after=15,
    source="EIA WPSR schedule: Wednesdays 10:30 ET (holiday weeks shift; add via `shifted`)",
)
EIA_NG_STORAGE = ScheduledEvent(
    name="EIA_WEEKLY_NATURAL_GAS_STORAGE_REPORT",
    weekday=3,
    time_et=time(10, 30),
    minutes_before=10,
    minutes_after=20,
    source="EIA WNGSR schedule: Thursdays 10:30 ET (holiday weeks shift; add via `shifted`)",
)

_COMMON_WEIGHTS = {"trend": 25.0, "momentum": 20.0, "structure": 20.0, "strength": 15.0, "regime": 10.0,
                   "execution": 10.0}

CRUDEOILM = InstrumentConfig(
    instrument="CRUDEOILM",
    version="MCXF_CRUDEOILM_V1",
    calibration_status="PRE_REGISTERED_UNVALIDATED",
    quotation_unit="BBL",
    feature_timeframe_minutes=3,
    feature_window_bars=160,
    ema_fast=9,
    ema_slow=21,
    slope_bars=3,
    atr_period=14,
    adx_period=14,
    breakout_lookback=20,
    er_lookback=10,
    atr_percentile_window=100,
    atr_percentile_min_samples=40,
    momentum_bars=3,
    pullback_bars=3,
    weights=dict(_COMMON_WEIGHTS),
    slope_full_atr=0.30,
    momentum_full_atr=1.00,
    breakout_full_atr=0.25,
    vwap_full_atr=0.50,
    adx_floor=18.0,
    adx_full=35.0,
    chop_er_threshold=0.30,
    trend_er_full=0.60,
    vol_percentile_low=0.10,
    vol_percentile_high=0.90,
    entry_score_floor=60.0,
    exceptional_reversal_score_floor=85.0,
    opposite_exit_score_floor=60.0,
    confirmation_bars=1,
    pullback_atr=0.50,
    setup_lifetime_minutes=30,
    reversal_confirmation_seconds=360,
    observation_interval_seconds=15,
    lots=1,
    stop_atr_multiple=1.5,
    min_stop_ticks=8,
    max_risk_per_trade=2500.0,
    daily_loss_limit=6000.0,
    max_entries_per_session=4,
    max_consecutive_losses=3,
    cooldown_after_exit_minutes=5,
    cooldown_after_loss_minutes=15,
    trail_activation_atr=1.0,
    trail_atr=1.5,
    max_holding_minutes=120,
    gap_guard_atr=1.5,
    gap_guard_minutes=15,
    expiry_window_days=3,
    last_entry_minutes_before_close=30,
    forced_exit_minutes_before_close=10,
    max_spread_ticks=3.0,
    full_credit_spread_ticks=1.0,
    stale_quote_seconds=45.0,
    stale_exit_seconds=120.0,
    max_entry_latency_seconds=30.0,
    stop_slippage_ticks=1,
    entry_slippage_ticks=0,
    events=(EIA_WPSR,),
    notes=("PRE_REGISTERED_UNVALIDATED: no historical MCX data; every value is a design choice under test.",),
)

NATGASMINI = InstrumentConfig(
    instrument="NATGASMINI",
    version="MCXF_NATGASMINI_V1",
    calibration_status="PRE_REGISTERED_UNVALIDATED",
    quotation_unit="MMBTU",
    feature_timeframe_minutes=5,
    feature_window_bars=160,
    ema_fast=9,
    ema_slow=21,
    slope_bars=3,
    atr_period=14,
    adx_period=14,
    breakout_lookback=20,
    er_lookback=12,
    atr_percentile_window=100,
    atr_percentile_min_samples=40,
    momentum_bars=3,
    pullback_bars=3,
    weights=dict(_COMMON_WEIGHTS),
    slope_full_atr=0.35,
    momentum_full_atr=1.25,
    breakout_full_atr=0.35,
    vwap_full_atr=0.60,
    adx_floor=20.0,
    adx_full=38.0,
    chop_er_threshold=0.35,
    trend_er_full=0.65,
    vol_percentile_low=0.15,
    vol_percentile_high=0.85,
    entry_score_floor=60.0,
    exceptional_reversal_score_floor=88.0,
    opposite_exit_score_floor=60.0,
    confirmation_bars=2,
    pullback_atr=0.75,
    setup_lifetime_minutes=45,
    reversal_confirmation_seconds=600,
    observation_interval_seconds=15,
    lots=1,
    stop_atr_multiple=2.0,
    min_stop_ticks=10,
    max_risk_per_trade=4000.0,
    daily_loss_limit=8000.0,
    max_entries_per_session=3,
    max_consecutive_losses=2,
    cooldown_after_exit_minutes=10,
    cooldown_after_loss_minutes=20,
    trail_activation_atr=1.5,
    trail_atr=2.0,
    max_holding_minutes=180,
    gap_guard_atr=2.0,
    gap_guard_minutes=20,
    expiry_window_days=3,
    last_entry_minutes_before_close=30,
    forced_exit_minutes_before_close=10,
    max_spread_ticks=4.0,
    full_credit_spread_ticks=1.0,
    stale_quote_seconds=45.0,
    stale_exit_seconds=120.0,
    max_entry_latency_seconds=30.0,
    stop_slippage_ticks=1,
    entry_slippage_ticks=0,
    events=(EIA_NG_STORAGE,),
    notes=(
        "PRE_REGISTERED_UNVALIDATED: no historical MCX data; every value is a design choice under test.",
        "Differs from CRUDEOILM because natural gas is noisier: 5-min bars (vs 3), 2 confirmation bars (vs 1), "
        "2.0 ATR stop (vs 1.5), larger pullback/trail/gap thresholds, stricter chop/ADX/vol band, "
        "Rs 4,000/trade and Rs 8,000/day risk, 3 entries and 2 consecutive losses per session, longer "
        "cooldowns, 4-tick spread limit (0.40 at tick 0.10), storage-report blackout Thursdays 10:30 ET.",
    ),
)

CONFIGS: dict[str, InstrumentConfig] = {"CRUDEOILM": CRUDEOILM, "NATGASMINI": NATGASMINI}
