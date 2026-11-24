"""ACTS_V3_MCX_FUTURES opportunity cycle + risk tests (SYNTHETIC evaluations only)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.mcx_futures import cycle, risk
from app.mcx_futures.config import CRUDEOILM, NATGASMINI
from tests.test_mcx_futures_support import ist, spec_for

CFG = CRUDEOILM  # confirmation_bars = 1, 3-minute bars
BAR = timedelta(seconds=CFG.bar_seconds)
T0 = ist(2026, 11, 24, 12, 0)


def ev(direction: str | None, score: float = 70.0, state: str | None = None) -> dict:
    market = state or (f"{direction}_TREND" if direction else "NEUTRAL")
    return {"direction": direction, "market_state": market, "score": score, "warm": True}


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def tick(self, bars: int = 1):
        self.at = self.at + bars * BAR
        return self.at


def test_first_opportunity_reserves_and_consumes_only_on_fill() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    first = cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    assert first["enter"] and first["entry_reason"] == "FIRST_OPPORTUNITY"
    assert state["cycle_state"] == "RESERVED" and state["consumed_directions"] == []
    audit = cycle.filled(state, clock.at + timedelta(seconds=15), "LONG")
    assert audit["cycle_state"] == "ENTERED" and audit["consumed_directions"] == ["LONG"]
    cycle.closed(state, clock.tick(), "LONG", "STOP")
    again = cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    assert not again["enter"] and "CONSUMED_CYCLE" in again["blockers"]


def test_unfilled_entry_releases_without_consuming() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    assert cycle.decide(state, clock.tick(), ev("SHORT"), [], CFG)["enter"]
    released = cycle.released(state, clock.at + timedelta(seconds=15))
    assert released["cycle_state"] == "ARMED" and released["armed_direction"] == "SHORT"
    assert released["consumed_directions"] == []
    retry = cycle.decide(state, clock.tick(), ev("SHORT"), [], CFG)
    assert retry["enter"] and retry["cycle"]["cycle_id"] == released["cycle_id"]


def test_risk_blockers_keep_cycle_armed_but_block_entry() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    blocked = cycle.decide(state, clock.tick(), ev("LONG"), ["EVENT_BLACKOUT"], CFG)
    assert not blocked["enter"] and state["cycle_state"] == "ARMED" and state["armed_direction"] == "LONG"
    assert cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)["enter"]


def test_pullback_resumption_rearms_consumed_direction() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    cycle.filled(state, clock.at, "LONG")
    cycle.closed(state, clock.tick(), "LONG", "TRAILING_STOP")
    assert "CONSUMED_CYCLE" in cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)["blockers"]
    pull = cycle.decide(state, clock.tick(), ev("LONG", 40, "LONG_PULLBACK"), [], CFG)
    assert not pull["enter"] and state["pullback"]["direction"] == "LONG"
    resumed = cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    assert resumed["enter"] and resumed["entry_reason"] == "PULLBACK_RESUMPTION"
    assert state["cycle_state"] == "RESERVED"


def test_pullback_expires_after_setup_lifetime() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    cycle.filled(state, clock.at, "LONG")
    cycle.closed(state, clock.tick(), "LONG", "STOP")
    cycle.decide(state, clock.tick(), ev("LONG", 40, "LONG_PULLBACK"), [], CFG)
    clock.tick(CFG.setup_lifetime_minutes // 3 + 1)
    late = cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    assert not late["enter"] and "CONSUMED_CYCLE" in late["blockers"]


def test_return_after_false_reversal_and_quarantine() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    cycle.filled(state, clock.at, "LONG")
    audit = cycle.closed(state, clock.tick(), "LONG", cycle.OPPOSITE_SIGNAL)
    assert audit["quarantined_direction"] == "SHORT"
    flip = cycle.decide(state, clock.tick(), ev("SHORT", 75), [], CFG)
    assert not flip["enter"] and "REVERSAL_QUARANTINE" in flip["blockers"]
    back = cycle.decide(state, clock.tick(), ev("LONG", 70), [], CFG)
    assert back["enter"] and back["entry_reason"] == "RETURN_AFTER_FALSE_REVERSAL"


def test_exceptional_reversal_needs_sustained_confirmation_with_gap_tolerance() -> None:
    state, clock = cycle.new_state("2026-11-24"), Clock()
    cycle.decide(state, clock.tick(), ev("LONG"), [], CFG)
    cycle.filled(state, clock.at, "LONG")
    cycle.closed(state, clock.tick(), "LONG", cycle.OPPOSITE_SIGNAL)
    needed = CFG.reversal_confirmation_seconds // CFG.bar_seconds  # 2 bars after the first sighting
    results = [cycle.decide(state, clock.tick(), ev("SHORT", 90), [], CFG) for _ in range(needed)]
    assert all(not r["enter"] for r in results)
    assert "REVERSAL_CONFIRMATION_PENDING" in results[0]["blockers"]
    final = cycle.decide(state, clock.tick(), ev("SHORT", 90), [], CFG)
    assert final["enter"] and final["entry_reason"] == "EXCEPTIONAL_REVERSAL_CONFIRMATION"
    # A gap longer than one bar + 1.5 observation intervals restarts the confirmation clock.
    assert cycle.max_confirmation_gap_seconds(CFG) == CFG.bar_seconds + 1.5 * CFG.observation_interval_seconds
    gap_state, clock2 = cycle.new_state("2026-11-24"), Clock()
    cycle.decide(gap_state, clock2.tick(), ev("LONG"), [], CFG)
    cycle.filled(gap_state, clock2.at, "LONG")
    cycle.closed(gap_state, clock2.tick(), "LONG", cycle.OPPOSITE_SIGNAL)
    cycle.decide(gap_state, clock2.tick(), ev("SHORT", 90), [], CFG)
    cycle.decide(gap_state, clock2.tick(), ev("SHORT", 90), [], CFG)
    assert gap_state["reversal_confirmation_seconds"] == CFG.bar_seconds
    late = clock2.at + timedelta(seconds=cycle.max_confirmation_gap_seconds(CFG) + 1)
    restarted = cycle.decide(gap_state, late, ev("SHORT", 90), [], CFG)
    assert gap_state["reversal_confirmation_seconds"] == 0.0 and not restarted["enter"]


def test_confirmation_bars_for_natgas_and_session_reset() -> None:
    state = cycle.new_state("2026-11-24")
    at = T0
    step = timedelta(seconds=NATGASMINI.bar_seconds)
    first = cycle.decide(state, at + step, ev("LONG"), [], NATGASMINI)
    assert not first["enter"] and "AWAITING_CONFIRMATION" in first["blockers"]
    assert cycle.decide(state, at + 2 * step, ev("LONG"), [], NATGASMINI)["enter"]
    cycle.filled(state, at + 2 * step, "LONG")
    fresh = cycle.ensure_session(state, "2026-11-25")
    assert fresh["consumed_directions"] == [] and fresh["cycle_state"] == "ARMED"
    with pytest.raises(ValueError, match="NONCAUSAL"):
        cycle.decide(state, at, ev("LONG"), [], NATGASMINI)


# -- risk ------------------------------------------------------------------------------------------------------
SESSION = {"session_id": "2026-11-24", "open_at": ist(2026, 11, 24, 9, 0).isoformat(),
           "last_entry_at": ist(2026, 11, 24, 23, 25).isoformat(),
           "forced_exit_at": ist(2026, 11, 24, 23, 45).isoformat()}


def blockers(state: dict, at=None, atr: float | None = 6.0, cfg=CRUDEOILM, spec=None, age: float | None = 0.0):
    return risk.entry_blockers(state, at or ist(2026, 11, 24, 12, 0), atr=atr, cfg=cfg,
                               spec=spec or spec_for(cfg.instrument), session=SESSION, position_open=False,
                               quote_age=age)


def test_stop_distance_rounding_and_too_large_stop_refused() -> None:
    crude, gas = spec_for("CRUDEOILM"), spec_for("NATGASMINI")
    assert risk.stop_distance(6.1, CRUDEOILM, crude) == 10.0  # 9.15 -> ceil to tick 1.00
    assert risk.stop_distance(2.0, CRUDEOILM, crude) == 8.0   # min ticks
    assert risk.stop_distance(0.83, NATGASMINI, gas) == pytest.approx(1.7)  # 1.66 -> 1.7
    state = risk.new_state("2026-11-24")
    assert blockers(state, atr=6.0) == []
    # crude: 1.5 x 170 = 255 points x 10 = Rs 2,550 > Rs 2,500
    assert "STOP_RISK_EXCEEDS_MAX_PER_TRADE" not in blockers(state, atr=166.0)
    assert "STOP_RISK_EXCEEDS_MAX_RISK_PER_TRADE" in blockers(state, atr=170.0)
    # natgas: 2.0 x 8.1 = 16.2 points x 250 = Rs 4,050 > Rs 4,000
    assert "STOP_RISK_EXCEEDS_MAX_RISK_PER_TRADE" in blockers(risk.new_state("2026-11-24"), atr=8.1,
                                                              cfg=NATGASMINI)


def test_daily_loss_consecutive_losses_cooldowns() -> None:
    state = risk.new_state("2026-11-24")
    at = ist(2026, 11, 24, 12, 0)
    risk.record_exit(state, at, -5950.0)
    out = blockers(state, at + timedelta(minutes=30))
    assert "DAILY_LOSS_HEADROOM" in out and "DAILY_LOSS_LIMIT" not in out
    risk.record_exit(state, at, -100.0)
    assert "DAILY_LOSS_LIMIT" in blockers(state, at + timedelta(minutes=30))
    streak = risk.new_state("2026-11-24")
    for _ in range(3):
        risk.record_exit(streak, at, -10.0)
    assert "MAX_CONSECUTIVE_LOSSES" in blockers(streak, at + timedelta(minutes=30))
    cool = risk.new_state("2026-11-24")
    risk.record_exit(cool, at, 50.0)
    assert "COOLDOWN_AFTER_EXIT" in blockers(cool, at + timedelta(minutes=4))
    assert blockers(cool, at + timedelta(minutes=5)) == []
    risk.record_exit(cool, at, -50.0)
    assert "COOLDOWN_AFTER_LOSS" in blockers(cool, at + timedelta(minutes=14))
    many = risk.new_state("2026-11-24")
    many["entries"] = CRUDEOILM.max_entries_per_session
    assert "MAX_ENTRIES_PER_SESSION" in blockers(many)


def test_stale_feed_gap_guard_expiry_and_last_entry() -> None:
    state = risk.new_state("2026-11-24")
    assert "STALE_FEED" in blockers(state, age=46.0)
    assert "STALE_FEED" in blockers(state, age=None)
    at = ist(2026, 11, 24, 12, 0)
    assert risk.check_gap(state, at, 5610.0, 5600.0, 8.0, CRUDEOILM) is None  # 1.25 ATR <= 1.5
    evidence = risk.check_gap(state, at, 5615.0, 5600.0, 8.0, CRUDEOILM)
    assert evidence and evidence["gap_atr"] == pytest.approx(1.875)
    assert "GAP_GUARD" in blockers(state, at + timedelta(minutes=14))
    assert "GAP_GUARD" not in blockers(state, at + timedelta(minutes=15))
    near = spec_for("CRUDEOILM")
    from dataclasses import replace
    expiring = replace(near, expiry=ist(2026, 11, 26, 0, 0).date())
    assert "EXPIRY_WINDOW" in blockers(risk.new_state("2026-11-24"), spec=expiring)
    assert "AFTER_LAST_ENTRY_TIME" in blockers(risk.new_state("2026-11-24"), at=ist(2026, 11, 24, 23, 25))
    assert "AFTER_LAST_ENTRY_TIME" not in blockers(risk.new_state("2026-11-24"), at=ist(2026, 11, 24, 23, 24, 59))
