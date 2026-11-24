"""ACTS_V3_MCX_FUTURES contract spec + session calendar tests (SYNTHETIC master fixture)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.mcx_futures.config import CRUDEOILM, NATGASMINI
from app.mcx_futures.session import CLOSED, OPEN, PRE_OPEN, DayOverride, SessionCalendar
from app.mcx_futures.spec import IST, SpecError, on_tick_grid, resolve_futures, round_to_tick
from tests.test_mcx_futures_support import ist, master_rows

TODAY = date(2026, 11, 24)


def test_resolves_lot_and_tick_from_master_rows() -> None:
    crude = resolve_futures(master_rows("CRUDEOILM"), "CRUDEOILM", TODAY, 3, master_date=TODAY)
    gas = resolve_futures(master_rows("NATGASMINI"), "NATGASMINI", TODAY, 3, master_date=TODAY)
    assert (crude.lot_size, crude.tick_size, crude.precision, crude.quotation_unit) == (10, 1.0, 2, "BBL")
    assert (gas.lot_size, gas.tick_size, gas.precision, gas.quotation_unit) == (250, 0.1, 2, "MMBTU")
    assert crude.rupees_per_point_per_lot == 10 and gas.rupees_per_point_per_lot == 250
    assert crude.segment == "mcx_fo" and crude.source == "KOTAK_SCRIP_MASTER:2026-11-24"
    # Exact pSymbolName: the CRUDEOIL (big) contract with lot 100 is never selected.
    assert crude.trading_symbol.startswith("CRUDEOILM")


def test_rollover_skips_contract_inside_expiry_window() -> None:
    # 25Nov2026 CRUDEOILM and 26Nov2026 NATGASMINI are inside the 3-day window on 2026-11-24.
    crude = resolve_futures(master_rows("CRUDEOILM"), "CRUDEOILM", TODAY, 3)
    gas = resolve_futures(master_rows("NATGASMINI"), "NATGASMINI", TODAY, 3)
    assert crude.expiry == date(2026, 12, 18) and crude.trading_symbol == "CRUDEOILM18DEC26FUT"
    assert gas.expiry == date(2026, 12, 24)
    # With no window the nearest contract is chosen.
    assert resolve_futures(master_rows("CRUDEOILM"), "CRUDEOILM", TODAY, 0).expiry == date(2026, 11, 25)
    with pytest.raises(SpecError, match="NO_FUTCOM_OUTSIDE_EXPIRY_WINDOW"):
        resolve_futures(master_rows("NATGASMINI"), "NATGASMINI", date(2026, 12, 23), 3)


@pytest.mark.parametrize("patch, reason", [
    ({"lLotSize": 0}, "lLotSize"),
    ({"lLotSize": "ten"}, "lLotSize"),
    ({"dTickSize": -100}, "dTickSize"),
    ({"dTickSize": 2.5}, "dTickSize"),
    ({"lPrecision": 2.5}, "lPrecision"),
    ({"lPrecision": None}, "lPrecision"),
    ({"pExpiryDate": "31Feb2026"}, "pExpiryDate"),
    ({"lExpiryDate": 1797465599}, "MASTER_EXPIRY_FIELDS_DISAGREE"),
    ({"pSymbol": ""}, "MASTER_IDENTITY_MISSING"),
])
def test_malformed_master_rows_are_refused(patch: dict, reason: str) -> None:
    rows = [dict(row) for row in master_rows("CRUDEOILM")]
    rows[1].update(patch)
    with pytest.raises(SpecError, match=reason):
        resolve_futures(rows, "CRUDEOILM", TODAY, 3)


def test_missing_tick_fields_and_ambiguity_are_refused() -> None:
    rows = [dict(row) for row in master_rows("NATGASMINI")]
    del rows[0]["lPrecision"]
    with pytest.raises(SpecError, match="MASTER_TICK_FIELDS_MISSING"):
        resolve_futures(rows, "NATGASMINI", TODAY, 3)
    duplicate = [dict(row) for row in master_rows("CRUDEOILM")]
    duplicate.append({**duplicate[1], "pSymbol": 999})
    with pytest.raises(SpecError, match="AMBIGUOUS_FUTCOM_EXPIRY"):
        resolve_futures(duplicate, "CRUDEOILM", TODAY, 3)
    with pytest.raises(SpecError, match="UNSUPPORTED_INSTRUMENT"):
        resolve_futures(duplicate, "GOLDM", TODAY, 3)


def test_round_to_tick() -> None:
    assert round_to_tick(5601.4, 1.0, "DOWN") == 5601.0
    assert round_to_tick(5601.4, 1.0, "UP") == 5602.0
    assert round_to_tick(308.44, 0.1, "UP") == pytest.approx(308.5)
    assert round_to_tick(308.46, 0.1, "DOWN") == pytest.approx(308.4)
    assert round_to_tick(308.45, 0.1, "NEAREST") == pytest.approx(308.5)
    assert on_tick_grid(308.5, 0.1) and not on_tick_grid(308.55, 0.1)
    with pytest.raises(ValueError):
        round_to_tick(1.0, 0.1, "SIDEWAYS")


def test_winter_and_summer_session_windows_and_cutoffs() -> None:
    cal = SessionCalendar()
    winter = cal.day(date(2026, 11, 24))
    summer = cal.day(date(2026, 10, 30))
    assert winter is not None and summer is not None
    assert winter.close_at.time() == time(23, 55) and "MCX/TRD/550/2026" in winter.source
    assert summer.close_at.time() == time(23, 30)
    assert cal.day(date(2026, 11, 2)).close_at.time() == time(23, 55)  # type: ignore[union-attr]
    assert cal.day(date(2027, 3, 12)).close_at.time() == time(23, 55)  # type: ignore[union-attr]
    assert cal.day(date(2027, 3, 15)).close_at.time() == time(23, 30)  # type: ignore[union-attr]
    for cfg in (CRUDEOILM, NATGASMINI):
        last, forced = winter.cutoffs(cfg.last_entry_minutes_before_close, cfg.forced_exit_minutes_before_close)
        assert (last.time(), forced.time()) == (time(23, 25), time(23, 45))
        last, forced = summer.cutoffs(cfg.last_entry_minutes_before_close, cfg.forced_exit_minutes_before_close)
        assert (last.time(), forced.time()) == (time(23, 0), time(23, 20))


def test_session_state_for_any_aware_datetime() -> None:
    cal = SessionCalendar()
    assert cal.state(ist(2026, 11, 24, 9, 0)).status == OPEN
    assert cal.state(ist(2026, 11, 24, 23, 54, 59)).status == OPEN
    closed = cal.state(ist(2026, 11, 24, 23, 55))
    assert closed.status == CLOSED and closed.next_open == ist(2026, 11, 25, 9, 0)
    pre = cal.state(ist(2026, 11, 24, 8, 59))
    assert pre.status == PRE_OPEN and pre.next_open == ist(2026, 11, 24, 9, 0)
    # UTC input: 2026-11-24 18:00 UTC = 23:30 IST -> still open in winter.
    assert cal.state(datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)).status == OPEN
    # Summer: 23:30 IST is closed.
    assert cal.state(datetime(2026, 10, 30, 23, 30, tzinfo=IST)).status == CLOSED
    weekend = cal.state(ist(2026, 11, 28, 12, 0))
    assert weekend.status == CLOSED and weekend.next_open == ist(2026, 11, 30, 9, 0)
    with pytest.raises(ValueError):
        cal.state(datetime(2026, 11, 24, 12, 0))


def test_holiday_and_evening_only_override() -> None:
    cal = SessionCalendar(
        holidays=frozenset({date(2026, 12, 25)}),
        overrides={date(2026, 11, 26): DayOverride(time(17, 0), None, "EVENING_ONLY", "SYNTHETIC_TEST_CIRCULAR")},
    )
    assert cal.day(date(2026, 12, 25)) is None
    assert cal.state(ist(2026, 12, 25, 12, 0)).next_open == ist(2026, 12, 28, 9, 0)
    evening = cal.day(date(2026, 11, 26))
    assert evening is not None and evening.open_at == ist(2026, 11, 26, 17, 0)
    assert evening.close_at == ist(2026, 11, 26, 23, 55) and evening.kind == "OVERRIDE"
    state = cal.state(ist(2026, 11, 26, 10, 0))
    assert state.status == PRE_OPEN and state.next_open == ist(2026, 11, 26, 17, 0)
    assert cal.state(ist(2026, 11, 26, 17, 0)).status == OPEN
    full_close = SessionCalendar(overrides={date(2026, 11, 27): DayOverride(None, None, "CLOSED", "TEST")})
    assert full_close.day(date(2026, 11, 27)) is None


def test_event_blackouts_are_new_york_time_converted_to_ist() -> None:
    # Winter (EST, UTC-5): Wed 10:30 ET = 21:00 IST; blackout 20:50-21:15 IST.
    assert CRUDEOILM.blackout(ist(2026, 11, 25, 20, 50)) is not None
    assert CRUDEOILM.blackout(ist(2026, 11, 25, 21, 14, 59)) is not None
    assert CRUDEOILM.blackout(ist(2026, 11, 25, 21, 15)) is None
    assert CRUDEOILM.blackout(ist(2026, 11, 25, 20, 49)) is None
    # Summer (EDT, UTC-4) before 2026-11-01: Wed 2026-10-28 10:30 ET = 20:00 IST.
    assert CRUDEOILM.blackout(ist(2026, 10, 28, 20, 0)) is not None
    assert CRUDEOILM.blackout(ist(2026, 10, 28, 21, 0)) is None
    # Natural gas storage: Thursdays, not Wednesdays; crude is not blacked out on Thursday.
    assert NATGASMINI.blackout(ist(2026, 11, 26, 21, 10)) is not None
    assert NATGASMINI.blackout(ist(2026, 11, 25, 21, 0)) is None
    assert CRUDEOILM.blackout(ist(2026, 11, 26, 21, 0)) is None


def test_holiday_shifted_event_override() -> None:
    from dataclasses import replace

    from app.mcx_futures.config import EIA_NG_STORAGE

    shifted = replace(EIA_NG_STORAGE, shifted=(("2026-11-26", "2026-11-25T12:00:00"),))
    cfg = NATGASMINI.with_overrides(events=(shifted,))
    assert cfg.blackout(ist(2026, 11, 26, 21, 0)) is None
    # 12:00 EST = 22:30 IST on Wednesday.
    assert cfg.blackout(ist(2026, 11, 25, 22, 30)) is not None
    assert cfg.config_hash != NATGASMINI.config_hash


def test_configs_are_independent_versioned_and_unvalidated() -> None:
    assert CRUDEOILM.calibration_status == NATGASMINI.calibration_status == "PRE_REGISTERED_UNVALIDATED"
    assert CRUDEOILM.config_hash != NATGASMINI.config_hash
    assert (CRUDEOILM.stop_atr_multiple, NATGASMINI.stop_atr_multiple) == (1.5, 2.0)
    assert (CRUDEOILM.max_risk_per_trade, NATGASMINI.max_risk_per_trade) == (2500, 4000)
    assert (CRUDEOILM.daily_loss_limit, NATGASMINI.daily_loss_limit) == (6000, 8000)
    assert (CRUDEOILM.max_entries_per_session, NATGASMINI.max_entries_per_session) == (4, 3)
    assert NATGASMINI.confirmation_bars > CRUDEOILM.confirmation_bars
    assert CRUDEOILM.entry_score_floor == NATGASMINI.entry_score_floor == 60
    assert sum(CRUDEOILM.weights.values()) == 100
    with pytest.raises(ValueError):
        CRUDEOILM.with_overrides(weights={**CRUDEOILM.weights, "trend": 30.0})
