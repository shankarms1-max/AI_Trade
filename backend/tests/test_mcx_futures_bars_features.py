"""ACTS_V3_MCX_FUTURES bar builder, features and score tests (SYNTHETIC data only)."""

from __future__ import annotations

from datetime import timedelta
import random

import pytest

from app.mcx_futures import features, score
from app.mcx_futures.bars import MinuteBarBuilder, Observation, Resampler, bar_dict, resample
from app.mcx_futures.config import CRUDEOILM
from tests.test_mcx_futures_support import ist, obs

T0 = ist(2026, 11, 24, 10, 0)


def test_bar_builder_rejects_out_of_order_and_duplicate() -> None:
    builder = MinuteBarBuilder()
    assert builder.add(obs(T0 + timedelta(seconds=15), 5600, cum=100)).accepted
    duplicate = builder.add(obs(T0 + timedelta(seconds=15), 5601, cum=110))
    assert (duplicate.accepted, duplicate.reason) == (False, "DUPLICATE_OBSERVATION")
    late = builder.add(obs(T0, 5599, cum=90))
    assert (late.accepted, late.reason) == (False, "OUT_OF_ORDER_OBSERVATION")
    crossed = builder.add(obs(T0 + timedelta(seconds=30), 5600, bid=5602, ask=5601))
    assert crossed.reason == "CROSSED_QUOTE"
    naive = builder.add(Observation(at=(T0 + timedelta(seconds=45)).replace(tzinfo=None), ltp=1, bid=1, ask=1))
    assert naive.reason == "NAIVE_TIMESTAMP"


def test_bar_completes_only_on_later_minute_and_gaps_are_not_filled() -> None:
    builder = MinuteBarBuilder()
    for second, price, cum in ((0, 5600, 1000), (15, 5605, 1010), (30, 5598, 1030), (45, 5601, 1040)):
        result = builder.add(obs(T0 + timedelta(seconds=second), price, cum=cum))
        assert result.completed == []  # still the same minute
    nxt = builder.add(obs(T0 + timedelta(minutes=1, seconds=5), 5602, cum=1050))
    assert len(nxt.completed) == 1
    bar = nxt.completed[0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (5600, 5605, 5598, 5601)
    assert bar["volume"] == 40.0  # first observation of the session is only a baseline
    assert bar["observations"] == 4
    # 3-minute outage: no fabricated bars, gap reported.
    gap = builder.add(obs(T0 + timedelta(minutes=5, seconds=1), 5610, cum=1100))
    assert [b["start"] for b in gap.completed] == [(T0 + timedelta(minutes=1)).isoformat()]
    assert gap.gaps == [{"from": (T0 + timedelta(minutes=2)).isoformat(), "to": (T0 + timedelta(minutes=5)).isoformat(),
                         "missing_minutes": 3}]
    assert builder.flush()[0]["missing_minutes"] == 3


def test_volume_unavailable_propagates_and_vwap_is_never_substituted() -> None:
    builder = MinuteBarBuilder()
    builder.add(obs(T0, 5600, cum=None))
    bar = builder.add(obs(T0 + timedelta(minutes=1), 5600, cum=None)).completed[0]
    assert bar["volume"] is None
    value, reason = features.session_vwap([bar])
    assert value is None and reason == "VOLUME_UNAVAILABLE"


def test_resampler_emits_only_complete_buckets() -> None:
    minute_bars = [bar_dict(T0 + timedelta(minutes=i), 1, 100 + i, 101 + i, 99 + i, 100.5 + i, bid=None, ask=None,
                            volume=10.0, observations=4) for i in range(7)]
    out = resample(minute_bars, 3)
    assert [b["start"] for b in out] == [T0.isoformat(), (T0 + timedelta(minutes=3)).isoformat()]
    assert out[0]["open"] == 100 and out[0]["close"] == 102.5 and out[0]["high"] == 103 and out[0]["volume"] == 30
    sampler = Resampler(5)
    gappy = [minute_bars[0], minute_bars[1], minute_bars[6]]
    emitted = [bar for item in gappy for bar in sampler.add(item)]
    assert len(emitted) == 1 and emitted[0]["missing_minutes"] == 3


def _random_bars(n: int, seed: int = 5) -> list[dict]:
    rng = random.Random(seed)
    price = 5600.0
    out = []
    for i in range(n):
        o = price
        price += rng.gauss(0, 3)
        out.append(bar_dict(T0 + timedelta(minutes=3 * i), 3, o, max(o, price) + rng.random() * 2,
                            min(o, price) - rng.random() * 2, price, bid=price - 1, ask=price + 1, volume=50.0,
                            observations=12))
    return out


def test_feature_series_are_causal() -> None:
    bars = _random_bars(200)
    closes = [b["close"] for b in bars]
    full = (features.ema_series(closes, 21), features.atr_series(bars, 14), features.adx_series(bars, 14)[0])
    prefix = (features.ema_series(closes[:120], 21), features.atr_series(bars[:120], 14),
              features.adx_series(bars[:120], 14)[0])
    for long_series, short_series in zip(full, prefix):
        assert long_series[:120] == short_series
    snapshot = features.compute(bars[:120], CRUDEOILM)
    again = features.compute(bars[:120], CRUDEOILM)
    assert snapshot == again
    # Appending future bars never alters the features of an earlier decision point.
    assert features.compute(list(bars[:120]), CRUDEOILM) == features.compute(bars[:200][:120], CRUDEOILM)


def test_feature_warmup_is_enforced() -> None:
    need = features.warmup_bars(CRUDEOILM)
    cold = features.compute(_random_bars(need - 1), CRUDEOILM)
    assert not cold["warm"] and cold["atr"]["value"] is None and cold["atr"]["reason"].startswith("WARMUP")
    warm = features.compute(_random_bars(need), CRUDEOILM)
    assert warm["warm"] and warm["atr"]["value"] is not None and warm["adx"]["value"] is not None


def _features(**values: float | None) -> dict:
    base = {"warm": True, "bars_available": 100, "warmup_required": 54}
    for name, value in values.items():
        base[name] = {"value": value, "reason": None if value is not None else "UNAVAILABLE_IN_TEST"}
    return base


GOOD = dict(ema_fast=5620.0, ema_slow=5600.0, ema_slope_atr=0.4, atr=6.0, adx=36.0, plus_di=30.0, minus_di=12.0,
            breakout_up_atr=0.3, breakout_down_atr=-3.0, efficiency_ratio=0.7, atr_percentile=0.5,
            return_k_atr=1.2, pullback_return_atr=1.2, close_vs_vwap_atr=0.8, vwap=5590.0, spread_ticks=1.0,
            quote_age_seconds=0.0)
FACTS = {"blackout": None, "in_entry_window": True, "days_to_expiry": 20, "bar_missing_minutes": 0}


def test_score_valid_trend_reaches_full_marks() -> None:
    result = score.evaluate(_features(**GOOD), FACTS, CRUDEOILM)
    assert result["direction"] == "LONG" and result["market_state"] == "LONG_TREND"
    assert result["score"] == pytest.approx(100.0)
    assert result["vetoes"] == []
    assert {k: v["max"] for k, v in result["components"].items()} == CRUDEOILM.weights
    short = score.evaluate(_features(**{**GOOD, "ema_fast": 5580.0, "ema_slope_atr": -0.4, "return_k_atr": -1.2,
                                        "pullback_return_atr": -1.2, "close_vs_vwap_atr": -0.8,
                                        "plus_di": 12.0, "minus_di": 30.0, "breakout_down_atr": 0.3}),
                           FACTS, CRUDEOILM)
    assert short["direction"] == "SHORT" and short["score"] == pytest.approx(100.0)


def test_score_no_signal() -> None:
    flat = score.evaluate(_features(**{**GOOD, "ema_fast": 5600.0, "ema_slope_atr": 0.0}), FACTS, CRUDEOILM)
    assert flat["direction"] is None and flat["market_state"] == "NEUTRAL" and flat["score"] == 0


def test_score_false_breakout_stays_below_floor() -> None:
    # Spike above the prior high closed back below it, momentum faded, below VWAP, choppy and weak ADX.
    false_breakout = {**GOOD, "ema_slope_atr": 0.05, "breakout_up_atr": -0.2, "return_k_atr": -0.1,
                      "pullback_return_atr": -0.1, "close_vs_vwap_atr": -0.2, "adx": 17.0,
                      "efficiency_ratio": 0.2}
    result = score.evaluate(_features(**false_breakout), FACTS, CRUDEOILM)
    assert result["direction"] == "LONG" and result["market_state"] == "LONG_CHOP"
    assert result["score"] < CRUDEOILM.entry_score_floor


def test_unavailable_inputs_score_zero_without_rescaling() -> None:
    missing = {**GOOD, "close_vs_vwap_atr": None, "vwap": None}
    result = score.evaluate(_features(**missing), FACTS, CRUDEOILM)
    assert result["components"]["structure"]["points"] == pytest.approx(10.0)
    assert result["components"]["structure"]["unavailable"] == ["vwap:UNAVAILABLE_IN_TEST"]
    assert result["score"] == pytest.approx(90.0)


def test_hard_vetoes_are_separate_from_score() -> None:
    facts = {"blackout": {"event": "EIA"}, "in_entry_window": False, "days_to_expiry": 2, "bar_missing_minutes": 1}
    stale = {**GOOD, "spread_ticks": 5.0, "quote_age_seconds": 60.0}
    result = score.evaluate(_features(**stale), facts, CRUDEOILM)
    assert set(result["vetoes"]) == {"SPREAD_TOO_WIDE", "STALE_FEED", "EVENT_BLACKOUT", "OUTSIDE_ENTRY_WINDOW",
                                     "EXPIRY_WINDOW", "DATA_GAP_IN_BAR"}
    assert result["score"] == pytest.approx(90.0)  # execution component 0, score itself not vetoed


def test_pullback_state() -> None:
    result = score.evaluate(_features(**{**GOOD, "pullback_return_atr": -0.6}), FACTS, CRUDEOILM)
    assert result["market_state"] == "LONG_PULLBACK"
