"""V3 cycle lifecycle over synthetic V2 signals; no live market or broker calls."""

from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from app.acts_paper import acts_v2, acts_v3
from app.paper.mirror_exit import ACTS_REASON
from tests.test_acts_paper_v2 import _decision


@pytest.fixture
def signals(monkeypatch):
    original = acts_v2._score
    current = {"score": 70}

    def fixed(ctx, direction, market, candidate, config):
        _, components, evidence = original(ctx, direction, market, candidate, config)
        return current["score"], components, evidence

    monkeypatch.setattr(acts_v2, "_score", fixed)
    base = datetime.fromisoformat(_decision()[0]["timestamp"])

    def evaluate(watch, seconds, direction="BULL", score=70, *, pullback=False, missing=False, blockers=()):
        ctx, candidate, rejected, config = _decision(1 if direction != "BEAR" else -1)
        ctx["timestamp"] = (base + timedelta(seconds=seconds)).isoformat()
        if direction is None:
            ctx["futures_returns_bps"] = {key: None for key in ctx["futures_returns_bps"]}
        if pullback:
            ctx["futures_returns_bps"]["3m"] = -3 if direction == "BULL" else 3
        current["score"] = score
        return acts_v3.decide(ctx, watch, None if missing else candidate, rejected, list(blockers), config)

    return evaluate, base


def consumed(signals, reason="SPREAD_STOP"):
    evaluate, base = signals
    watch = {}
    assert evaluate(watch, 0)["status"] == "ENTER"
    acts_v3.filled(watch, base + timedelta(seconds=15), "BULL")
    acts_v3.closed(watch, base + timedelta(seconds=30), "BULL", reason)
    return watch


@pytest.mark.parametrize("score,expected", [(59, "NO_SETUP"), (59.999, "NO_SETUP"), (60, "ENTER"), (85, "ENTER")])
def test_first_opportunity_respects_new_floor(signals, score, expected):
    evaluate, _ = signals
    result = evaluate({}, 0, score=score)
    assert result["status"] == expected
    assert ("V3_SCORE_FLOOR" in result["blockers"]) == (score < 60)
    assert result["strategy_id"] == "ACTS_V3"
    assert result["opportunity_cycle_policy_version"] == acts_v3.POLICY_VERSION


@pytest.mark.parametrize("exit_reason", ["SPREAD_STOP", "HARD_MAX_LOSS", "FORCED_CLOSE"])
def test_consumed_direction_cannot_retry_plain_continuation_after_cooldown(signals, exit_reason):
    watch = consumed(signals, exit_reason)
    result = signals[0](watch, 600, score=90)
    assert result["status"] == "NO_SETUP" and "CONSUMED_CYCLE" in result["blockers"]
    assert result["previous_exit_reason"] == exit_reason


def test_same_direction_pullback_resumption_rearms(signals):
    watch = consumed(signals)
    old_id = watch["opportunity_cycle"]["cycle_id"]
    pullback = signals[0](watch, 45, pullback=True)
    assert pullback["cycle_state"] == "RESET_OBSERVED"
    result = signals[0](watch, 60, score=60)
    assert result["status"] == "ENTER" and result["rearm_reason"] == "PULLBACK_RESUMPTION"
    assert result["cycle_id"] != old_id


def test_plain_opposite_after_mirror_is_quarantined(signals):
    watch = consumed(signals, ACTS_REASON)
    result = signals[0](watch, 45, "BEAR", 75)
    assert result["status"] == "NO_SETUP"
    assert "REVERSAL_QUARANTINE" in result["blockers"]
    assert result["previous_direction"] == "BULL"


def test_opposite_pullback_resumption_overrides_quarantine(signals):
    watch = consumed(signals, ACTS_REASON)
    signals[0](watch, 45, "BEAR", 70, pullback=True)
    result = signals[0](watch, 60, "BEAR", 60)
    assert result["status"] == "ENTER"
    assert result["rearm_reason"] == "OPPOSITE_PULLBACK_RESUMPTION"


def test_exceptional_reversal_requires_four_intervals_and_restart_persistence(signals):
    watch = consumed(signals, ACTS_REASON)
    for seconds in (45, 60, 75, 90):
        result = signals[0](watch, seconds, "BEAR", 85)
        assert result["status"] == "NO_SETUP"
        assert "REVERSAL_CONFIRMATION_PENDING" in result["blockers"]
        assert result["reversal_confirmation_seconds"] == seconds - 45
        watch = json.loads(json.dumps(watch))
    result = signals[0](watch, 105, "BEAR", 85)
    assert result["status"] == "ENTER"
    assert result["reversal_confirmation_seconds"] == 60
    assert result["rearm_reason"] == "EXCEPTIONAL_REVERSAL_CONFIRMATION"


@pytest.mark.parametrize("break_kind", ["gap", "neutral", "direction", "score", "book"])
def test_confirmation_resets_when_continuity_is_lost(signals, break_kind):
    watch = consumed(signals, ACTS_REASON)
    signals[0](watch, 45, "BEAR", 90)
    signals[0](watch, 60, "BEAR", 90)
    if break_kind == "gap":
        result = signals[0](watch, 90, "BEAR", 90)
    else:
        signals[0](
            watch,
            75,
            None if break_kind == "neutral" else "BULL" if break_kind == "direction" else "BEAR",
            84 if break_kind == "score" else 90,
            missing=break_kind == "book",
            blockers=["ANY_EXIT_COOLDOWN_ACTIVE"],
        )
        result = signals[0](watch, 90, "BEAR", 90)
    assert result["status"] == "NO_SETUP"
    assert result["reversal_confirmation_seconds"] == 0


def test_false_reversal_return_rearms_original_direction(signals):
    watch = consumed(signals, ACTS_REASON)
    signals[0](watch, 45, "BEAR", 65)
    result = signals[0](watch, 60, "BULL", 60)
    assert result["status"] == "ENTER"
    assert result["rearm_reason"] == "RETURN_AFTER_FALSE_REVERSAL"


def test_confirmed_reversal_waiting_for_risk_loses_permission_after_gap(signals):
    watch = consumed(signals, ACTS_REASON)
    for seconds in (45, 60, 75, 90, 105):
        result = signals[0](watch, seconds, "BEAR", 85, blockers=["LOSS_COOLDOWN_ACTIVE"])
    assert result["cycle_state"] == "REARMED" and result["reversal_confirmation_seconds"] == 60
    result = signals[0](watch, 135, "BEAR", 85)
    assert result["status"] == "NO_SETUP" and result["reversal_confirmation_seconds"] == 0
    assert "REVERSAL_CONFIRMATION_PENDING" in result["blockers"]


def test_contract_identity_change_revokes_waiting_rearm_and_confirmation(signals):
    watch = consumed(signals, ACTS_REASON)
    for seconds in (45, 60, 75, 90, 105):
        signals[0](watch, seconds, "BEAR", 85, blockers=["LOSS_COOLDOWN_ACTIVE"])
    cycle = acts_v3.state(watch, signals[1] + timedelta(seconds=120), ("DIFFERENT_FUTURE", "2099-11-01", "2099-10-08"))
    assert cycle["armed_direction"] is None and cycle["reversal_confirmation_seconds"] == 0
    assert cycle["consumed_directions"] == ["BULL"]
    assert cycle["quarantined_direction"] == "BEAR"


@pytest.mark.parametrize(
    "blocker",
    [
        "MAX_TRADES_PER_DAY_REACHED",
        "MAX_OPEN_POSITIONS_REACHED",
        "HARD_DAILY_LOSS_REACHED",
        "MAX_CONSECUTIVE_LOSSES_REACHED",
        "LOSS_COOLDOWN_ACTIVE",
        "ANY_EXIT_COOLDOWN_ACTIVE",
        "KILL_SWITCH_ACTIVE",
        "MARKET_EVENT_BLOCKED",
    ],
)
def test_cycle_permission_never_bypasses_hard_risk(signals, blocker):
    result = signals[0]({}, 0, score=95, blockers=[blocker])
    assert result["status"] != "ENTER" and blocker in result["blockers"]


def test_rearm_can_wait_for_risk_permission_without_consuming_reset(signals):
    watch = consumed(signals)
    signals[0](watch, 45, pullback=True)
    blocked = signals[0](watch, 60, score=65, blockers=["LOSS_COOLDOWN_ACTIVE"])
    assert blocked["status"] != "ENTER" and blocked["cycle_state"] == "REARMED"
    result = signals[0](watch, 75, score=65)
    assert result["status"] == "ENTER" and result["rearm_reason"] == "PULLBACK_RESUMPTION"


def test_low_score_resumption_does_not_erase_real_pullback(signals):
    watch = consumed(signals)
    signals[0](watch, 45, pullback=True)
    assert signals[0](watch, 60, score=59)["status"] != "ENTER"
    result = signals[0](watch, 75, score=60)
    assert result["status"] == "ENTER" and result["rearm_reason"] == "PULLBACK_RESUMPTION"


def test_session_reset_and_replay_are_deterministic(signals):
    evaluate, _ = signals
    one, two = {}, {}
    sequence = [(0, False), (15, True), (30, False)]
    for seconds, pullback in sequence:
        assert evaluate(one, seconds, pullback=pullback) == evaluate(two, seconds, pullback=pullback)
    old = deepcopy(one)
    with pytest.raises(ValueError, match="NONCAUSAL"):
        evaluate(one, 30)
    next_day = evaluate(two, 24 * 3600, score=60)
    assert next_day["status"] == "ENTER" and next_day["previous_direction"] is None
    assert old["opportunity_cycle"]["cycle_id"] != next_day["cycle_id"]
