"""Synthetic execution/risk evidence, not validation of live MCX contracts."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, func, select, text

from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_execution import candidates
from app.commodity_acts.paper_journal import verify
from app.commodity_acts.paper_models import CommodityPaperCursor, CommodityPaperEvent, CommodityPaperFill
from app.commodity_acts.paper_policy import ContractEvidence
from tests.test_commodity_paper import AT, case, policy, positions, raw, research_case, step


def latest(case):
    with case[0]() as session:
        return json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)


def test_trailing_uses_observed_gain_then_next_observation_exit(case):
    step(case, 0)
    step(case, 15)
    credit = positions(case)[0][1]["entry_credit"]
    width = positions(case)[0][1]["candidate"]["spread_width"]
    # Choose an observed synthetic premium curve with a 35% profit, below the
    # existing configured 50% target, then a 20%-of-credit giveback.
    gain_scale = (credit*.65-2)/(width*.15)
    bounce_scale = (credit*.85-2)/(width*.15)
    def tick_aligned(rows):
        for row in rows:
            if row["exchange_token"].endswith("_F"): continue
            for side in ("buy", "sell"):
                row["depth"][side][0]["price"] = str(round(float(row["depth"][side][0]["price"])*4)/4)
        return rows
    step(case, 30, scale=gain_scale, corrupt=tick_aligned)
    assert positions(case)[0][0] == "OPEN"
    step(case, 45, scale=bounce_scale, corrupt=tick_aligned)
    assert positions(case)[0][1]["exit_reason"] == "TRAILING_EXIT"
    assert positions(case)[0][0] == "PENDING_EXIT"
    step(case, 60, scale=bounce_scale, corrupt=tick_aligned)
    assert positions(case)[0][0] == "CLOSED"


@pytest.mark.parametrize("missing_opposite", [False, True])
def test_native_mirror_requires_full_opposite_entry_not_just_direction(case, missing_opposite):
    step(case, 0)
    step(case, 15)
    case[3].direction = -1
    def corrupt(rows):
        return [{**row, "depth": {}} if row["exchange_token"].endswith("_CE") else row for row in rows]
    step(case, 30, corrupt=corrupt if missing_opposite else None)
    state, doc = positions(case)[0]
    if missing_opposite:
        assert state == "OPEN" and doc["mirror_signal"]["status"] != "ENTER"
        assert "exit_order_id" not in doc
    else:
        assert state == "PENDING_EXIT" and doc["exit_reason"] == "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"
        assert doc["mirror_signal"]["status"] == "ENTER" and doc["mirror_signal"]["direction"] == "BEAR"
        step(case, 45)
        assert positions(case)[0][0] == "CLOSED"


def test_forced_session_exit_keeps_next_observation_causality(case):
    case[2]["CRUDEOILM"].policy = policy(max_holding_minutes=1000)
    step(case, 0)
    step(case, 15)
    forced = (case[1]["CRUDEOILM"].spec.market_session.forced_exit.hour-AT.hour)*3600 + 55*60
    step(case, forced)
    assert positions(case)[0][0] == "PENDING_EXIT"
    assert positions(case)[0][1]["exit_reason"] == "FORCED_SESSION_EXIT"
    step(case, forced+15)
    assert positions(case)[0][0] == "CLOSED"
    assert "SESSION_ENTRY_CLOSED" in latest(case)["risk_blocks"]


def test_disabled_entries_still_manage_frozen_open_position(case):
    step(case, 0)
    step(case, 15)
    service = case[1]["CRUDEOILM"]
    service.config = service.config.model_copy(update={"trading_enabled": False, "paper_policies_json": "{}"})
    case[2]["CRUDEOILM"] = CommodityPaperEngine(service)
    step(case, 1830)
    assert positions(case)[0][0] == "PENDING_EXIT"
    step(case, 1845)
    assert positions(case)[0][0] == "CLOSED"
    assert "PAPER_ACTIVATION_DISABLED" in latest(case)["risk_blocks"]


@pytest.mark.parametrize("setting,reason", [
    ("max_loss_per_trade", "NO_EXECUTABLE_CANDIDATE"),
    ("daily_loss_limit", "INSTRUMENT_DAILY_LOSS_RESERVATION_LIMIT"),
    ("global_max_defined_risk", "GLOBAL_DEFINED_RISK_LIMIT"),
    ("global_daily_loss_limit", "GLOBAL_DAILY_LOSS_RESERVATION_LIMIT"),
    ("max_broker_margin", "NO_EXECUTABLE_CANDIDATE"),
    ("kill_switch", "KILL_SWITCH"),
])
def test_risk_and_unverified_margin_caps_fail_closed(case, setting, reason):
    engine = case[2]["CRUDEOILM"]
    if setting.startswith("global_"):
        engine.config = engine.config.model_copy(update={setting: 1})
    else:
        engine.policy = policy(**{setting: True if setting == "kill_switch" else 1})
    step(case, 0)
    assert not positions(case)
    assert reason in latest(case)["decision"]["blockers"]
    if setting == "max_broker_margin":
        assert latest(case)["decision"]["candidate_rejections"]["MARGIN_UNVERIFIED_OR_LIMIT_EXCEEDED"] > 0


def test_session_trade_limit_counts_completed_trades_not_only_open_positions(case):
    step(case, 0)
    step(case, 15)
    step(case, 1830)
    step(case, 1845)
    assert "SESSION_TRADE_LIMIT" in latest(case)["risk_blocks"]
    assert len(positions(case)) == 1


def test_global_policy_drift_cannot_fill_pending_order(case):
    step(case, 0)
    engine = case[2]["CRUDEOILM"]
    engine.config = engine.config.model_copy(update={"global_max_defined_risk": 300000})
    step(case, 15)
    assert positions(case)[0][0] == "ENTRY_REJECTED"
    assert positions(case)[0][1]["rejection_reason"] == "GLOBAL_RISK_POLICY_CHANGED"
    verify(case[0], "CRUDEOILM")


@pytest.mark.parametrize("change", ["policy_json='{}'", "lock_version=0"])
def test_global_control_tampering_rejected_on_restart(case, change):
    step(case, 0)
    with case[0].begin() as session:
        session.execute(text(f"UPDATE commodity_paper_controls SET {change}"))
    with pytest.raises(RuntimeError, match="GLOBAL_POLICY_INVALID"):
        CommodityPaperEngine(case[1]["CRUDEOILM"])


def test_failed_transaction_rolls_back_fill_order_and_cursor_then_retries(case, monkeypatch):
    step(case, 0)
    engine = case[2]["CRUDEOILM"]
    original = engine._emit
    def crash(session, cursor, kind, *args, **kwargs):
        if kind == "PAPER_OBSERVATION": raise RuntimeError("SYNTHETIC_CRASH_AFTER_FILL")
        return original(session, cursor, kind, *args, **kwargs)
    monkeypatch.setattr(engine, "_emit", crash)
    with pytest.raises(RuntimeError, match="SYNTHETIC_CRASH"):
        step(case, 15)
    assert positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    with case[0]() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 0
    verify(case[0], "CRUDEOILM")
    monkeypatch.setattr(engine, "_emit", original)
    step(case, 15)
    assert positions(case)[0][0] == "OPEN"
    verify(case[0], "CRUDEOILM")


@pytest.mark.parametrize("missing", ["depth_unit", "tick_size", "option_future_link_verified", "rupees_per_point_per_lot"])
def test_unconfirmed_contract_metadata_is_not_inferred(missing):
    evidence = policy().evidence.model_dump()
    evidence.pop(missing)
    with pytest.raises(ValueError):
        ContractEvidence.model_validate(evidence)


@pytest.mark.parametrize("field", ["expiry", "reference_timestamp", "future_token"])
def test_expiry_and_reference_essentials_block_entries(case, field):
    snapshot = raw()
    if field == "expiry":
        engine = case[2]["CRUDEOILM"]
        engine.policy = policy(evidence={**policy().evidence.model_dump(), "option_expiry": AT.date()})
        snapshot = snapshot.model_copy(update={"expiry": AT.date(), "options": [q.model_copy(update={"expiry": AT.date()}) for q in snapshot.options]})
    elif field == "reference_timestamp":
        snapshot = snapshot.model_copy(update={"source_market_timestamp": None})
    else:
        snapshot = snapshot.model_copy(update={"future_token": "DIFFERENT_SYNTHETIC_FUTURE"})
    sid, _ = case[1]["CRUDEOILM"].observe(snapshot)
    case[2]["CRUDEOILM"].process_snapshot(sid)
    assert not positions(case)
    assert latest(case)["decision"]["status"] != "ENTER"
    if field == "expiry": assert "EXPIRY_ENTRY_FORBIDDEN" in latest(case)["risk_blocks"]


def test_exact_contract_roll_never_silently_substitutes_positions(case):
    step(case, 0)
    step(case, 15)
    snapshot = raw(seconds=1830)
    snapshot = snapshot.model_copy(update={"options": [q.model_copy(update={"instrument_token": q.instrument_token+"_ROLLED"}) for q in snapshot.options]})
    case[3].at = snapshot.timestamp_ist+timedelta(seconds=1)
    sid, _ = case[1]["CRUDEOILM"].observe(snapshot)
    case[2]["CRUDEOILM"].process_snapshot(sid)
    state, doc = positions(case)[0]
    assert state == "UNRESOLVED_EXPOSURE" and doc["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    assert doc.get("exit_timestamp") is None
    verify(case[0], "CRUDEOILM")


def test_journal_records_no_fill_capital_when_margin_unverified(case):
    step(case, 0)
    step(case, 15)
    with case[0]() as session:
        events = [json.loads(row.payload) for row in session.scalars(select(CommodityPaperEvent))]
    assert any(row["kind"] == "ENTRY_FILLED" for row in events)
    from app.research.manifest import digest
    assert all(digest(row["paper_policy"]) == row["paper_policy_hash"] for row in events if row["kind"] == "PAPER_OBSERVATION")
    assert all("broker_order_id" not in json.dumps(row) for row in events)
    assert positions(case)[0][1]["margin_evidence"] is None


def test_finite_limits_and_default_off():
    from app.commodity_acts.config import CommoditySettings
    assert not CommoditySettings(_env_file=None).trading_enabled
    with pytest.raises(ValueError):
        CommoditySettings(_env_file=None, global_daily_loss_limit=float("inf"))
    with pytest.raises(ValueError, match="POLICY_MAP_REQUIRED"):
        CommoditySettings(_env_file=None, paper_policies_json="[]").trading_policy("CRUDEOILM")


def test_policy_defaults_have_stable_hash_after_durable_json_roundtrip():
    from app.commodity_acts.paper_policy import PaperPolicy
    from app.research.manifest import digest
    original = policy()
    assert digest(original) == digest(PaperPolicy.model_validate_json(original.model_dump_json()))


def test_actual_order_creation_not_decision_start_controls_next_observation(case, monkeypatch):
    from app.commodity_acts.paper_models import CommodityPaperOrder
    engine = case[2]["CRUDEOILM"]
    ticks = iter([AT+timedelta(seconds=1), AT+timedelta(seconds=1), AT+timedelta(seconds=20)])
    monkeypatch.setattr(engine, "clock", lambda: next(ticks))
    step(case, 0)
    with case[0]() as session:
        order = json.loads(session.scalar(select(CommodityPaperOrder)).document)
    assert order["created_at"] == (AT+timedelta(seconds=20)).isoformat()
    assert positions(case)[0][1]["processed_at"] == (AT+timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(engine, "clock", lambda: case[3].at)
    step(case, 15)
    assert positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    step(case, 30)
    assert positions(case)[0][0] == "OPEN"


def test_mutex_wait_cannot_validate_a_book_against_the_pre_wait_clock(case, engine):
    def contention(_conn, _cursor, statement, *_):
        if statement.lstrip().upper().startswith("UPDATE COMMODITY_PAPER_CONTROLS"):
            case[3].at = AT+timedelta(seconds=61)
    event.listen(engine, "before_cursor_execute", contention)
    try:
        step(case, 0)
    finally:
        event.remove(engine, "before_cursor_execute", contention)
    assert not positions(case)
    row = latest(case)
    assert row["processed_at"] == (AT+timedelta(seconds=61)).isoformat()
    assert row["decision"]["candidate_rejections"] == {"REFERENCE_QUOTE_TIMESTAMP_MISSING_OR_STALE": 1}


@pytest.mark.parametrize("global_limit", [False, True])
def test_observed_daily_loss_forces_exit_without_inventing_a_fill(case, global_limit):
    engine = case[2]["CRUDEOILM"]
    if global_limit:
        engine.config = engine.config.model_copy(update={"global_daily_loss_limit": 2000})
        engine.policy = policy(daily_loss_limit=1000000)
    else:
        engine.policy = policy(daily_loss_limit=2000)
    step(case, 0)
    step(case, 15)
    step(case, 30, scale=100)
    state, doc = positions(case)[0]
    assert state == "PENDING_EXIT"
    assert doc["exit_reason"] == ("GLOBAL_DAILY_LOSS" if global_limit else "INSTRUMENT_DAILY_LOSS")
    assert doc.get("exit_timestamp") is None
    step(case, 45, scale=100)
    assert positions(case)[0][0] == "CLOSED"
    # Observed wide exit debit is NOT clamped to theoretical expiry width.
    doc = positions(case)[0][1]
    assert doc["exit_debit"] > doc["candidate"]["spread_width"]
    assert doc["gross_realized_pnl"] < -doc["defined_risk"]


def test_simultaneous_instruments_can_each_fill_but_never_write_nifty(case):
    from concurrent.futures import ThreadPoolExecutor
    from app.acts_paper.models import ActsPaperTrade
    from app.parallel_paper.models import ParallelPaperTrade
    for instrument in case[2]: step(case, 0, instrument=instrument)
    case[3].at = AT+timedelta(seconds=16)
    ids = {name: service.observe(raw(name, seconds=15))[0] for name, service in case[1].items()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda name: case[2][name].process_snapshot(ids[name]), list(ids)))
    assert all(positions(case, name)[0][0] == "OPEN" for name in ids)
    with case[0]() as session:
        assert session.scalar(select(func.count()).select_from(CommodityPaperFill)) == 2
        assert session.scalar(select(func.count()).select_from(ActsPaperTrade)) == 0
        assert session.scalar(select(func.count()).select_from(ParallelPaperTrade)) == 0
        sequences = [json.loads(row.payload)["global_sequence"] for row in session.scalars(select(
            CommodityPaperEvent).where(CommodityPaperEvent.event_type == "PAPER_OBSERVATION"))]
        assert len(sequences) == 4 and len(set(sequences)) == 4
    for name in ids: verify(case[0], name)
