"""Production defect 25 Nov: the daily scrip-master refresh (and any config / cost upgrade) discarded every warm-up bar.
SYNTHETIC data only."""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.mcx_futures import runtime
from app.mcx_futures.config import CRUDEOILM
from app.mcx_futures.costs import DEFAULT_SCHEDULE
from app.mcx_futures.engine import McxFuturesEngine
from app.mcx_futures.session import SessionCalendar
from app.mcx_futures.features import warmup_bars
from tests.test_mcx_futures_support import observations_from_bars, spec_for, trend_bars

SPEC = spec_for("CRUDEOILM")
CAL = SessionCalendar()


def warmed_state(minutes: int = 240) -> dict:
    engine = McxFuturesEngine(CRUDEOILM, SPEC, DEFAULT_SCHEDULE, calendar=CAL)
    stream = observations_from_bars(trend_bars(date(2026, 11, 24), start_price=5600, slope=0.4, tick=1.0, minutes=minutes,
                                               wiggle=1.5), 1.0)
    for o in stream:
        engine.on_observation(o, allow_entries=False)      # flat: we only want market state
    snap = engine.snapshot()
    assert len(snap["bars"]) >= warmup_bars(CRUDEOILM) and snap["position"] is None and snap["pending_entry"] is None
    return snap


def test_next_days_scrip_master_no_longer_discards_warm_up_bars():
    snap = warmed_state()
    next_day_spec = replace(SPEC, source="KOTAK_SCRIP_MASTER:2026-11-25")    # same contract, new master file date
    engine = runtime.open_engine(CRUDEOILM, next_day_spec, DEFAULT_SCHEDULE, {"state": snap}, CAL)
    assert engine.s["bars"] == snap["bars"] and len(engine.s["bars"]) >= warmup_bars(CRUDEOILM)
    assert engine.s["spec"]["source"] == "KOTAK_SCRIP_MASTER:2026-11-25"           # identity re-stamped
    assert engine.s["carried_market_state"]["previous_spec_source"] == SPEC.source
    assert engine.s["risk"] == snap["risk"] and engine.s["cycle"] == snap["cycle"]  # same-session budgets kept


def test_config_or_cost_upgrade_keeps_bars_but_never_carries_a_position_or_partial_bar():
    snap = warmed_state()
    upgraded = replace(CRUDEOILM, version="MCXF_CRUDEOILM_TEST_UPGRADE")
    assert upgraded.config_hash != CRUDEOILM.config_hash
    engine = runtime.open_engine(upgraded, SPEC, DEFAULT_SCHEDULE.with_brokerage(10), {"state": snap}, CAL)
    assert engine.s["bars"] == snap["bars"] and engine.s["config_hash"] == upgraded.config_hash
    assert engine.s["position"] is None and engine.s["pending_entry"] is None and engine.s["session"] is None
    assert engine.s["builder"]["current"] is None                                   # no partial bar carried


def test_a_different_contract_or_timeframe_starts_from_zero():
    snap = warmed_state()
    rolled = replace(SPEC, token="999999", trading_symbol="CRUDEOILM19JAN27FUT", expiry=date(2027, 1, 19))
    assert runtime.open_engine(CRUDEOILM, rolled, DEFAULT_SCHEDULE, {"state": snap}, CAL).s["bars"] == []
    slower = replace(CRUDEOILM, feature_timeframe_minutes=5)
    assert runtime.open_engine(slower, SPEC, DEFAULT_SCHEDULE, {"state": snap}, CAL).s["bars"] == []


def test_identity_change_with_exposure_is_still_refused():
    snap = warmed_state()
    snap["pending_entry"] = {"session_id": "2026-11-24"}
    with pytest.raises(RuntimeError, match="IDENTITY_CHANGED_WITH_EXPOSURE_REFUSED"):
        runtime.open_engine(CRUDEOILM, replace(SPEC, source="KOTAK_SCRIP_MASTER:2026-11-25"), DEFAULT_SCHEDULE,
                            {"state": snap}, CAL)


def test_carried_bars_drive_the_next_decision_immediately():
    snap = warmed_state()
    engine = runtime.open_engine(CRUDEOILM, replace(SPEC, source="KOTAK_SCRIP_MASTER:2026-11-25"), DEFAULT_SCHEDULE,
                                 {"state": snap}, CAL)
    nxt = observations_from_bars(trend_bars(date(2026, 11, 25), start_price=5700, slope=0.4, tick=1.0, minutes=12), 1.0)
    decisions = [e for o in nxt for e in engine.on_observation(o) if e["type"] == "DECISION" and e["payload"].get("status") != "SKIPPED"]
    assert decisions and all("WARMUP_INCOMPLETE" not in (d["payload"].get("vetoes") or []) for d in decisions)
