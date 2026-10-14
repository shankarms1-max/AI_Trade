"""Pure causal ORB, EMA and Supertrend/RSI/option-volume experiments."""
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app.parallel_paper.bars import Bar, MarketPoint, build_bars, contiguous_bars, opening_range
from app.parallel_paper.config import ExperimentConfig
from app.parallel_paper.decisions import decide, entry_committed
from app.parallel_paper.indicators import ema, rsi, supertrend, wilder
from app.scalper.repository import IST

START = datetime(2099, 10, 1, 9, 15, tzinfo=IST)
FUTURE_EXPIRY = date(2099, 10, 29)


def points(minutes=60, direction=1):
    rows = [MarketPoint(START + timedelta(seconds=index * 15),
                        25000 + (10 if index % 2 else -10), "NIFTY_FUT", FUTURE_EXPIRY,
                        1000 + index * 100, None) for index in range(minutes * 4)]
    at = START + timedelta(minutes=minutes)
    rows.append(MarketPoint(at, 25000 + direction * 30, "NIFTY_FUT", FUTURE_EXPIRY,
                            1000 + len(rows) * 100, None))
    return rows


def bars(direction=1, count=25, spike=150):
    result = []
    for index in range(count):
        price = 25000 + direction * index * 10
        result.append(Bar(START + timedelta(minutes=index * 3),
                          START + timedelta(minutes=(index + 1) * 3),
                          price - direction, price + 3, price - 3, price + direction * 2,
                          spike if index == count - 1 else 100))
    return result


@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize("direction,label", [(1, "BULL"), (-1, "BEAR")])
def test_orb_exact_range_bull_bear_and_short_causal_confirmation(minutes, direction, label):
    strategy, config, state = f"ORB_{minutes}", ExperimentConfig(), {}
    observations = points(minutes, direction)
    early = decide(strategy, observations[:-1], [], observations[-2].at, state, config)
    assert early["status"] == "NO_SETUP"
    assert "OPENING_RANGE_NOT_COMPLETE" in early["blockers"]
    first = decide(strategy, observations, [], observations[-1].at, state, config)
    assert first["status"] == "WATCH"
    assert first["features"]["opening_high"] == 25010
    assert first["features"]["opening_low"] == 24990
    assert first["features"]["range_size"] == 20
    at = observations[-1].at + timedelta(seconds=30)
    observations.append(replace(observations[-1], at=at))
    confirmed = decide(strategy, observations, [], at, state, config)
    assert confirmed["status"] == "ENTER"
    assert confirmed["direction"] == label
    assert confirmed["features"]["breakout_distance_bps"] > 0
    assert confirmed["features"]["confirmation_seconds"] == 30


def test_orb_equal_boundary_false_breakout_and_configured_reentry():
    observations, state, config = points(15), {}, ExperimentConfig()
    first = decide("ORB_15", observations, [], observations[-1].at, state, config)
    assert first["status"] == "WATCH"
    equal = replace(observations[-1], at=observations[-1].at + timedelta(seconds=15), price=25010)
    observations.append(equal)
    false = decide("ORB_15", observations, [], equal.at, state, config)
    assert false["features"]["false_breakout"]["direction"] == "BULL"
    assert false["status"] == "NO_SETUP"
    assert "breakout" not in state
    for offset in (15, 45):
        observations.append(replace(equal, at=equal.at + timedelta(seconds=offset), price=25030))
        decision = decide("ORB_15", observations, [], observations[-1].at, state, config)
    assert decision["status"] == "ENTER"
    entry_committed("ORB_15", decision, state)
    observations.append(replace(observations[-1], at=observations[-1].at + timedelta(seconds=15)))
    assert decide("ORB_15", observations, [], observations[-1].at, state, config)["status"] == "NO_SETUP"
    observations.append(replace(observations[-1], at=observations[-1].at + timedelta(seconds=15), price=25000))
    decide("ORB_15", observations, [], observations[-1].at, state, config)
    configured = config.model_copy(update={"orb_allow_reentry": True})
    for offset in (15, 45):
        observations.append(replace(observations[-1], at=observations[-1].at + timedelta(seconds=offset), price=25030))
        decision = decide("ORB_15", observations, [], observations[-1].at, state, configured)
    assert decision["status"] == "ENTER"


def test_orb_missing_opening_capture_and_confirmation_gap_fail_closed():
    observations, config = points(15), ExperimentConfig()
    incomplete = opening_range(observations[5:], observations[-1].at, 15, config)
    assert not incomplete["complete"]
    assert incomplete["reason"] == "PRICE_SEQUENCE_GAP"
    state = {}
    decide("ORB_15", observations, [], observations[-1].at, state, config)
    observations.append(replace(observations[-1], at=observations[-1].at + timedelta(seconds=60)))
    decision = decide("ORB_15", observations, [], observations[-1].at, state, config)
    assert decision["status"] == "WATCH"
    assert decision["features"]["confirmation_seconds"] == 0


def test_one_completed_bar_series_no_endpoint_price_or_future_leakage():
    observations = points(3)
    config = ExperimentConfig()
    before = build_bars(observations[:-1], START + timedelta(minutes=3, seconds=-1), config)
    assert before == []
    complete = build_bars(observations, observations[-1].at, config)
    assert len(complete) == 1 and complete[0].valid
    assert complete[0].high == 25010  # 09:18 tick belongs to the next price bar.
    assert complete[0].volume == 1200  # Actual same-basket cumulative delta at the boundary.
    changed = [*observations[:-1], replace(observations[-1], price=30000)]
    assert build_bars(changed, changed[-1].at, config) == complete
    with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
        build_bars(observations, observations[-2].at, config)
    with pytest.raises(ValueError, match="FUTURE_OBSERVATION"):
        decide("EMA_PULLBACK", [], complete, START, {}, config)


def test_bar_gaps_identity_volume_resets_and_missing_volume_are_not_fabricated():
    observations, config = points(6), ExperimentConfig()
    observations[5] = replace(observations[5], future_id="OTHER_FUTURE")
    malformed = build_bars(observations, observations[-1].at, config)
    assert not malformed[0].valid
    assert malformed[0].reason == "FUTURES_IDENTITY_OR_PRICE_UNAVAILABLE"
    assert len(contiguous_bars(malformed)) == 1
    observations = points(6)
    observations[6] = replace(observations[6], volume=1)
    malformed = build_bars(observations, observations[-1].at, config)
    assert malformed[0].volume is None
    assert malformed[0].volume_reason == "OPTION_BASKET_COUNTER_RESET"
    observations[6] = replace(observations[6], volume=None, volume_reason="EXACT_QUOTE_MISSING")
    assert build_bars(observations, observations[-1].at, config)[0].volume_reason == "EXACT_QUOTE_MISSING"
    assert not build_bars(points(3)[5:], START + timedelta(minutes=3), config)[0].valid
    # One leg's counter reset must not be hidden by a larger increase on the other leg.
    observations = [replace(point, volume_counters=(point.volume // 2, point.volume // 2))
                    for point in points(3)]
    observations[6] = replace(observations[6], volume=100000, volume_counters=(1, 99999))
    assert build_bars(observations, observations[-1].at, config)[0].volume is None


@pytest.mark.parametrize("direction,label", [(1, "BULL"), (-1, "BEAR")])
def test_ema_pullback_and_resumption_are_completed_bar_causal(direction, label):
    config, state = ExperimentConfig(), {}
    trend = bars(direction, count=23)
    closes = [bar.close for bar in trend]
    fast = ema(closes, config.ema_fast)[-1]
    assert fast is not None
    start = trend[-1].end
    pullback = Bar(start, start + timedelta(minutes=3), closes[-1],
                   max(closes[-1], fast) + 3, min(closes[-1], fast) - 3, fast, 100)
    trend.append(pullback)
    watch = decide("EMA_PULLBACK", [], trend, pullback.end, state, config)
    assert watch["status"] == "WATCH"
    assert watch["direction"] == label
    resume_price = closes[-1] + direction * 10
    resume = Bar(pullback.end, pullback.end + timedelta(minutes=3), fast,
                 max(resume_price, fast) + 3, min(resume_price, fast) - 3, resume_price, 150)
    trend.append(resume)
    decision = decide("EMA_PULLBACK", [], trend, resume.end, state, config)
    assert decision["status"] == "ENTER"
    assert decision["direction"] == label
    assert decision["features"]["resumption_strength_bps"] >= config.ema_resumption_bps
    assert decision["features"]["pullback_duration_seconds"] == 180
    assert decide("EMA_PULLBACK", [], trend, resume.end, state, config)["status"] == "NO_SETUP"


@pytest.mark.parametrize("direction,label", [(1, "BULL"), (-1, "BEAR")])
def test_supertrend_rsi_and_compatible_volume_confirm_direction(direction, label):
    observations = bars(direction)
    decision = decide("SUPERTREND_RSI_VOLUME", [], observations, observations[-1].end, {}, ExperimentConfig())
    assert decision["status"] == "ENTER"
    assert decision["direction"] == label
    assert decision["features"]["atr"] > 0
    assert decision["features"]["volume_ratio"] == 1.5
    assert decision["features"]["rsi"] == (100 if direction > 0 else 0)


def test_neutral_rsi_low_or_missing_volume_rejects_technical_only():
    config = ExperimentConfig()
    observations = bars(spike=100)
    decision = decide("SUPERTREND_RSI_VOLUME", [], observations, observations[-1].end, {}, config)
    assert "VOLUME_CONFIRMATION_REQUIRED" in decision["blockers"]
    missing = [*observations[:-1], replace(observations[-1], volume=None)]
    decision = decide("SUPERTREND_RSI_VOLUME", [], missing, missing[-1].end, {}, config)
    assert "VALID_OPTION_VOLUME_SERIES_REQUIRED" in decision["blockers"]
    flat = [replace(bar, open=25000, high=25001, low=24999, close=25000)
            for bar in bars()]
    neutral = decide("SUPERTREND_RSI_VOLUME", [], flat, flat[-1].end, {}, config)
    assert "RSI_NEUTRAL" in neutral["blockers"]
    assert neutral["features"]["rsi"] == 50


def test_indicator_values_and_deterministic_strategy_evidence():
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert wilder([3, 3, 3, 6], 3) == [None, None, 3, 4]
    assert rsi([1, 1, 1, 1], 2) == 50
    assert rsi([4, 3, 2, 1], 2) == 0
    observations = bars()
    assert supertrend(observations, 10, 3) == supertrend(observations, 10, 3)
    first = decide("SUPERTREND_RSI_VOLUME", [], observations, observations[-1].end, {}, ExperimentConfig())
    second = decide("SUPERTREND_RSI_VOLUME", [], observations, observations[-1].end, {}, ExperimentConfig())
    assert first == second
