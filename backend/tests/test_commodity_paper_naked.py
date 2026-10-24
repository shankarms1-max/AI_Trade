"""Synthetic regression evidence only, never a claim about live MCX execution."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_journal import verify
from app.commodity_acts.paper_margin import KotakSingleMarginReader, authorized_margin_reader
from app.commodity_acts.paper_models import CommodityPaperControl, CommodityPaperCursor, CommodityPaperPosition
from app.commodity_acts.paper_naked import NakedPaperPolicy, candidates, execute, load_policy, SingleMarginEvidence
from app.commodity_acts.paper_policy import margin_for
from app.research.manifest import digest, canonical, json_value
from tests.test_commodity_paper import AT, case, policy, positions, raw, research_case, step


@pytest.mark.parametrize("instrument,lot", [("CRUDEOILM", 10), ("NATGASMINI", 250)])
@pytest.mark.parametrize("quantity_offset", [-1, 0, 1])
def test_underlying_units_not_lots_and_no_partial_fill(instrument, lot, quantity_offset):
    p = policy(instrument)
    snapshot = raw(instrument, seconds=15)
    quote = next(q for q in snapshot.options if q.option_type.value == "PE" and q.strike < snapshot.future_price)
    quote = quote.model_copy(update={"bid_quantity": lot + quantity_offset, "ask_quantity": lot + quantity_offset})
    snapshot = snapshot.model_copy(update={"options": [quote]})
    candidate = {"short_leg": quote.model_dump(mode="json")}
    for entry in (True, False):
        fill, failure = execute(snapshot, candidate, p, AT + timedelta(seconds=16), entry=entry, after=AT)
        if quantity_offset < 0:
            assert fill is None and failure == "INSUFFICIENT_DEPTH"
        else:
            assert failure is None and fill["contract_quantity"] == lot
            assert fill["value"] == (quote.bid if entry else quote.ask)
            assert fill["action"] == ("SELL" if entry else "BUY") and "long" not in fill


@pytest.mark.parametrize(
    "change", [{"depth_unit": "LOTS"}, {"depth_unit": "UNKNOWN"}, {"lot_size": 100}, {"rupees_per_point_per_lot": 1}]
)
def test_naked_policy_rejects_wrong_confirmed_contract_units(change):
    with pytest.raises(ValueError):
        policy(evidence={**policy().evidence.model_dump(), **change})


def test_rank_balances_quality_and_distance_not_maximum_premium():
    snapshot = raw()
    puts = [q for q in snapshot.options if q.option_type.value == "PE" and q.strike < snapshot.future_price]
    expensive = max(puts, key=lambda q: q.strike).model_copy(
        update={"bid": 200, "ask": 225, "open_interest": 0, "volume": 0}
    )
    snapshot = snapshot.model_copy(
        update={"options": [expensive if q.identity == expensive.identity else q for q in snapshot.options]}
    )
    chosen, _ = candidates(snapshot, "BULL", {}, policy(), AT + timedelta(seconds=1))
    reversed_choice, _ = candidates(
        snapshot.model_copy(update={"options": list(reversed(snapshot.options))}),
        "BULL",
        {},
        policy(),
        AT + timedelta(seconds=1),
    )
    assert chosen and chosen.short_leg.instrument_token != expensive.instrument_token
    assert chosen.candidate_id == reversed_choice.candidate_id
    assert chosen.ranking_components["short_quality"] > 0 and chosen.loss_is_bounded is False
    assert chosen.maximum_loss is None and "spread_width" not in chosen.model_dump()


def test_marks_are_subsequent_asks_and_no_ltp_fallback(case):
    step(case, 0)
    step(case, 15)
    doc = positions(case)[0][1]
    assert doc["mark_at"] is None and doc["unrealized_gross"] is None
    step(case, 30)
    doc = positions(case)[0][1]
    assert doc["mark_execution"]["action"] == "BUY"
    assert doc["current_debit"] == doc["mark_execution"]["short"]["quote"]["ask"]
    assert doc["unrealized_gross"] == pytest.approx(
        (doc["entry_credit"] - doc["current_debit"]) * doc["contract_quantity"]
    )

    def absent(rows):
        for row in rows:
            if not row["exchange_token"].endswith("_F"):
                row["depth"] = {}
                row["ltp"] = "99999"
        return rows

    step(case, 45, corrupt=absent)
    state, doc = positions(case)[0]
    assert state == "UNRESOLVED_EXPOSURE" and doc["unrealized_gross"] is None
    assert doc.get("exit_timestamp") is None
    verify(case[0], "CRUDEOILM")


@pytest.mark.parametrize("missing", [False, True])
def test_pre_expiry_exit_is_queued_then_filled_or_unresolved_after_restart(case, missing):
    engine = case[2]["CRUDEOILM"]
    engine.policy = policy(pre_expiry_exit_days=14, pre_expiry_exit_time="10:02:00")
    step(case, 0)
    step(case, 15)

    def no_depth(rows):
        return [{**row, "depth": {}} if not row["exchange_token"].endswith("_F") else row for row in rows]

    step(case, 120, corrupt=no_depth if missing else None)
    state, doc = positions(case)[0]
    assert state == ("UNRESOLVED_EXPOSURE" if missing else "PENDING_EXIT")
    assert doc["exit_reason"] == "PRE_EXPIRY_SAFEGUARD" and doc.get("exit_timestamp") is None
    case[2]["CRUDEOILM"] = CommodityPaperEngine(case[1]["CRUDEOILM"])
    step(case, 135)
    assert positions(case)[0][0] == "CLOSED"
    verify(case[0], "CRUDEOILM")


def test_pre_expiry_entry_window_rejected_with_ttl_buffer(case):
    case[2]["CRUDEOILM"].policy = policy(pre_expiry_exit_days=14, pre_expiry_exit_time="10:01:00")
    step(case, 0)
    assert not positions(case)
    with case[0]() as session:
        latest = json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)
    assert latest["decision"]["candidate_rejections"]["PRE_EXPIRY_ENTRY_CUTOFF"] == 1


def test_adverse_gap_can_exceed_stop_and_entry_stress_no_clamped_pnl(case):
    step(case, 0)
    step(case, 15)
    entry = positions(case)[0][1]
    step(case, 30, scale=100)
    assert positions(case)[0][0] == "PENDING_EXIT"
    step(case, 45, scale=150)
    state, doc = positions(case)[0]
    assert state == "CLOSED"
    assert doc["gross_realized_pnl"] < -entry["entry_stress"]["stress_loss_estimate"]
    assert doc["gross_realized_pnl"] < -doc["policy"]["max_loss_per_trade"]
    assert doc["maximum_loss"] is None and doc["loss_is_bounded"] is False
    assert doc["net_realized_pnl"] is None and doc["return_on_verified_margin"] is None
    assert doc["gross_pnl_over_verified_margin_estimate"] is None


def test_single_option_costs_are_two_executions_and_legacy_policy_is_unchanged():
    values = dict(
        all_in_cost_per_leg_execution=2,
        cost_source="SYNTHETIC COMPLETE COST MODEL",
        cost_model="COMPLETE_FLAT_PER_EXECUTED_LEG",
        cost_effective_from=AT.date(),
        cost_effective_to=AT.date(),
    )
    new, old = policy(**values), policy(legacy=True, **values)
    assert new.net(100, AT.date()) == 96 and old.net(100, AT.date()) == 92
    assert new.net(100, (AT + timedelta(days=1)).date()) is None
    assert digest(load_policy(json_value(new))) == digest(new)
    assert digest(load_policy(json_value(old))) == digest(old)


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
@pytest.mark.parametrize("legacy,executions", [(False, 2), (True, 4)])
@pytest.mark.parametrize(
    "gross,expected_naked,expected_spread", [(100, 95.5, 91), (0, -4.5, -9), (-100, -104.5, -109)]
)
def test_cost_deduction_once_for_wins_breakeven_and_losses(
    instrument, legacy, executions, gross, expected_naked, expected_spread
):
    configured = policy(
        instrument,
        legacy=legacy,
        all_in_cost_per_leg_execution=2.25,
        cost_source="SYNTHETIC COMPLETE COST MODEL",
        cost_model="COMPLETE_FLAT_PER_EXECUTED_LEG",
        cost_effective_from=AT.date(),
        cost_effective_to=AT.date(),
    )
    expected = expected_spread if legacy else expected_naked
    assert configured.net(gross, AT.date(), AT.date()) == expected
    assert gross - expected == executions * 2.25
    restored = load_policy(json_value(configured))
    assert restored.net(gross, AT.date(), AT.date()) == expected


@pytest.mark.parametrize("legacy", [False, True])
def test_net_unavailable_without_costs_or_outside_either_execution_date(legacy):
    assert policy(legacy=legacy).net(100, AT.date(), AT.date()) is None
    configured = policy(
        legacy=legacy,
        all_in_cost_per_leg_execution=2,
        cost_source="SYNTHETIC COMPLETE COST MODEL",
        cost_model="COMPLETE_FLAT_PER_EXECUTED_LEG",
        cost_effective_from=AT.date(),
        cost_effective_to=(AT + timedelta(days=1)).date(),
    )
    assert configured.net(100, (AT + timedelta(days=1)).date(), AT.date()) == (92 if legacy else 96)
    for offset in (-1, 2):
        outside = (AT + timedelta(days=offset)).date()
        assert configured.net(100, outside, AT.date()) is None
        assert configured.net(100, AT.date(), outside) is None


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
def test_naked_mark_and_realized_net_deduct_two_costs_after_restart(case, instrument):
    case[2][instrument].policy = policy(
        instrument,
        all_in_cost_per_leg_execution=2.25,
        cost_source="SYNTHETIC COMPLETE COST MODEL",
        cost_model="COMPLETE_FLAT_PER_EXECUTED_LEG",
        cost_effective_from=AT.date(),
        cost_effective_to=AT.date(),
    )
    step(case, 0, instrument=instrument)
    step(case, 15, instrument=instrument)
    step(case, 30, instrument=instrument)
    state, marked = positions(case, instrument)[0]
    assert state == "OPEN"
    assert marked["unrealized_net"] == pytest.approx(marked["unrealized_gross"] - 4.5)
    step(case, 45, instrument=instrument, scale=0.2)
    state, requested = positions(case, instrument)[0]
    assert state == "PENDING_EXIT" and requested.get("exit_timestamp") is None
    case[2][instrument] = CommodityPaperEngine(case[1][instrument])
    step(case, 60, instrument=instrument, scale=0.2)
    state, closed = positions(case, instrument)[0]
    assert state == "CLOSED"
    assert closed["net_realized_pnl"] == pytest.approx(closed["gross_realized_pnl"] - 4.5)
    verify(case[0], instrument)


def test_stress_cap_is_required_and_never_replaced_by_spread_width():
    snapshot = raw()
    chosen, rejected = candidates(snapshot, "BULL", {}, policy(max_stress_loss_per_trade=1), AT + timedelta(seconds=1))
    assert chosen is None and rejected["STRESS_LOSS_OR_PREMIUM_LIMIT"] > 0
    document = policy().model_dump()
    document.pop("stress_underlying_move_pct")
    with pytest.raises(ValueError):
        NakedPaperPolicy.model_validate(document)


def test_legacy_policy_cannot_create_new_spread_entries(case):
    case[2]["CRUDEOILM"].policy = policy(legacy=True)
    step(case, 0)
    assert not positions(case)
    with case[0]() as session:
        latest = json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)
    assert "LEGACY_SPREAD_ENTRIES_DISABLED" in latest["risk_blocks"]


def test_legacy_lots_policy_cannot_misinterpret_new_confirmed_unit_quotes():
    from app.commodity_acts.paper_execution import candidates as spreads

    p, snapshot = policy(legacy=True), raw()
    candidate, _ = spreads(snapshot, "BULL", {}, p, AT)
    old_lots_policy = policy(legacy=True, evidence={**p.evidence.model_dump(), "depth_unit": "LOTS"})
    fill, reason = execute(snapshot, candidate.model_dump(mode="json"), old_lots_policy, AT, entry=False)
    assert fill is None and reason == "LEGACY_DEPTH_INTERPRETATION_CONFLICT"


@pytest.mark.parametrize("instrument,lot", [("CRUDEOILM", 10), ("NATGASMINI", 250)])
def test_official_single_instrument_margin_is_read_only_exact_and_sanitized(instrument, lot, capsys, caplog):
    calls = []

    def margin(**kwargs):
        calls.append(kwargs)
        return {
            "data": {
                "stat": "Ok",
                "stCode": 200,
                "rmsVldtd": "OK",
                "ordMrgn": "12345.50",
                "reqdMrgn": "999999",
                "avlCash": "PRIVATE_ACCOUNT_VALUE",
                "Auth": "PRIVATE_SECRET",
            }
        }

    reader = KotakSingleMarginReader(margin, lambda: AT)
    p, snapshot = policy(instrument), raw(instrument)
    candidate, _ = candidates(snapshot, "BULL", {}, p, AT)
    record = reader.estimate(snapshot, candidate.model_dump(mode="json"), p)
    assert calls == [
        dict(
            exchange_segment="mcx_fo",
            price=str(candidate.executable_credit),
            order_type="L",
            product="NRML",
            quantity=str(lot),
            instrument_token=candidate.short_leg.instrument_token,
            transaction_type="S",
        )
    ]
    assert record.required_margin == 12345.5 and record.hedge_token is None
    assert record.measurement_kind == "SINGLE_INSTRUMENT_ESTIMATE" and not record.portfolio_verified
    assert "PRIVATE" not in record.model_dump_json() + capsys.readouterr().out + caplog.text
    assert not any("order" in name.lower() for name in dir(reader) if not name.startswith("_"))


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": "PRIVATE_SECRET"},
        {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "NOT_OK", "ordMrgn": "12345"}},
        {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "OK", "ordMrgn": "0"}},
        {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "OK", "ordMrgn": "NaN"}},
    ],
)
def test_unusable_margin_is_unknown_not_zero(response):
    p, snapshot = policy(), raw()
    candidate, _ = candidates(snapshot, "BULL", {}, p, AT)
    reader = KotakSingleMarginReader(lambda **_: response, lambda: AT)
    assert reader.estimate(snapshot, candidate.model_dump(mode="json"), p) is None


def test_missing_authorization_never_logs_in_or_calls_broker():
    sdk = SimpleNamespace(configuration=SimpleNamespace(edit_token=None, edit_sid=None))
    assert authorized_margin_reader(sdk, lambda: AT) is None


def test_nonfinite_stress_estimate_fails_closed():
    candidate, rejected = candidates(raw(), "BULL", {}, policy(stress_premium_multiple=1e308), AT)
    assert candidate is None and rejected["STRESS_ESTIMATE_INVALID"] > 0


@pytest.mark.parametrize("kind", ["current", "stale", "future", "ambiguous", "wrong_expiry"])
def test_single_margin_evidence_is_exact_dated_and_unambiguous(kind):
    at = AT - timedelta(seconds=301) if kind == "stale" else AT + timedelta(seconds=1) if kind == "future" else AT
    record = SingleMarginEvidence(
        instrument_id="CRUDEOILM",
        short_token="TEST_SHORT",
        expiry="2099-10-15",
        lot_size=10,
        required_margin=12345,
        observed_at=at,
        source="SYNTHETIC_SINGLE_MARGIN_ESTIMATE",
    )
    p = policy(margin_evidence=[record, record] if kind == "ambiguous" else [record])
    expiry = p.evidence.option_expiry + timedelta(days=1) if kind == "wrong_expiry" else p.evidence.option_expiry
    found = margin_for(p, "TEST_SHORT", None, expiry, AT)
    assert (found is not None) == (kind == "current")
    assert margin_for(p, "OTHER_TOKEN", None, expiry, AT) is None


def test_margin_exception_does_not_leak_secrets(capsys, caplog):
    def failing(**_):
        raise RuntimeError("PRIVATE_AUTH_SECRET")

    p, snapshot = policy(), raw()
    candidate, _ = candidates(snapshot, "BULL", {}, p, AT)
    assert (
        KotakSingleMarginReader(failing, lambda: AT).estimate(snapshot, candidate.model_dump(mode="json"), p) is None
    )
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err + caplog.text


def test_entry_revalidates_otm_but_exit_does_not_require_otm():
    p, snapshot = policy(), raw(seconds=15)
    candidate, _ = candidates(snapshot, "BULL", {}, p, AT + timedelta(seconds=16))
    snapshot = snapshot.model_copy(
        update={"future_price": candidate.short_leg.strike, "reference_price": candidate.short_leg.strike}
    )
    values = candidate.model_dump(mode="json")
    fill, reason = execute(snapshot, values, p, AT + timedelta(seconds=16), entry=True, after=AT)
    assert fill is None and reason == "SHORT_NO_LONGER_OTM"
    fill, reason = execute(snapshot, values, p, AT + timedelta(seconds=16), entry=False, after=AT)
    assert fill is not None and reason is None and fill["action"] == "BUY"


def test_stress_risk_is_repriced_and_rejected_before_fill(case):
    step(case, 0)
    step(case, 15, scale=10)
    state, doc = positions(case)[0]
    assert state == "ENTRY_REJECTED" and doc["rejection_reason"] == "FILL_ECONOMICS_OUTSIDE_RISK"
    assert doc.get("entry_timestamp") is None
    verify(case[0], "CRUDEOILM")


def test_global_stress_exposure_growth_requests_real_subsequent_exit(case):
    engine = case[2]["CRUDEOILM"]
    engine.config = engine.config.model_copy(update={"global_max_stress_risk": 3000})
    engine.policy = policy(max_stress_loss_per_trade=20000, stop_credit_multiple=1000)
    step(case, 0)
    step(case, 15)
    step(case, 30, scale=3)
    state, doc = positions(case)[0]
    assert state == "PENDING_EXIT" and doc["exit_reason"] == "GLOBAL_STRESS_RISK_EXIT"
    assert doc.get("exit_timestamp") is None
    step(case, 45, scale=3)
    assert positions(case)[0][0] == "CLOSED"


def test_thesis_reversal_exit_does_not_require_margin_for_a_new_short(case):
    engine = case[2]["CRUDEOILM"]
    engine.policy = policy(max_broker_margin=20000)
    engine.margin_reader = KotakSingleMarginReader(
        lambda **_: {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "OK", "ordMrgn": "12000"}}, lambda: case[3].at
    )
    step(case, 0)
    step(case, 15)
    engine.margin_reader = None
    case[3].direction = -1
    step(case, 30)
    state, doc = positions(case)[0]
    assert state == "PENDING_EXIT" and doc["exit_reason"] == "OPPOSITE_ACTS_V2_ENTRY_SIGNAL"
    step(case, 45)
    assert len(positions(case)) == 1 and positions(case)[0][0] == "CLOSED"
    verify(case[0], "CRUDEOILM")


def test_stale_future_does_not_become_verified_stress_input(case):
    step(case, 0)
    step(case, 15)
    snapshot = raw(seconds=30).model_copy(update={"source_market_timestamp": None})
    case[3].at = AT + timedelta(seconds=31)
    sid, _ = case[1]["CRUDEOILM"].observe(snapshot)
    case[2]["CRUDEOILM"].process_snapshot(sid)
    state, doc = positions(case)[0]
    assert state == "PENDING_EXIT" and doc["exit_reason"] == "STRESS_RISK_EXIT"
    assert doc["current_stress"] is None
    step(case, 45)
    assert positions(case)[0][0] == "CLOSED"


def test_authorized_margin_is_refreshed_at_fill_and_never_actual_blocked_capital(case):
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "OK", "ordMrgn": "12000"}}

    engine = case[2]["CRUDEOILM"]
    engine.policy = policy(max_broker_margin=20000)
    engine.margin_reader = KotakSingleMarginReader(read, lambda: case[3].at)
    step(case, 0)
    assert len(calls) == 1 and positions(case)[0][0] == "PENDING_PAPER_ENTRY"
    step(case, 15)
    doc = positions(case)[0][1]
    assert len(calls) == 2 and doc["margin_evidence"]["required_margin"] == 12000
    assert doc["margin_evidence"]["observed_at"] == case[3].at.isoformat()
    assert doc["margin_status"] == "VERIFIED_SINGLE_INSTRUMENT_ESTIMATE"
    assert doc["margin_evidence"]["measurement_kind"] != "ACTUAL_BLOCKED"


def test_slow_margin_lookup_cannot_authorize_a_stale_fill(case):
    engine = case[2]["CRUDEOILM"]

    def delayed(**_):
        case[3].at += timedelta(seconds=45)
        return {"data": {"stat": "Ok", "stCode": 200, "rmsVldtd": "OK", "ordMrgn": "12000"}}

    engine.margin_reader = KotakSingleMarginReader(delayed, lambda: case[3].at)
    step(case, 0)
    assert not positions(case)
    with case[0]() as session:
        latest = json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)
    assert "QUOTE_EXPIRED_DURING_MARGIN_CHECK" in latest["risk_blocks"]


def test_legacy_open_spread_recovers_without_rewriting_frozen_policy(case):
    """Seed synthetic historical spread evidence using the retained implementation."""
    from app.commodity_acts.paper_execution import candidates as spreads, pair

    snapshot = raw()
    sid, _ = case[1]["CRUDEOILM"].observe(snapshot)
    engine, p = case[2]["CRUDEOILM"], policy(legacy=True)
    candidate, _ = spreads(snapshot, "BULL", {}, p, AT)
    execution, _ = pair(snapshot, candidate.model_dump(mode="json"), p, AT, entry=True)
    original_policy = canonical(p)
    with case[0].begin() as session:
        control = session.get(CommodityPaperControl, 1)
        control.lock_version += 1
        cursor = session.get(CommodityPaperCursor, "CRUDEOILM")
        doc = json_value(
            {
                "id": "SYNTHETIC_LEGACY_SPREAD",
                "session_id": snapshot.session_id,
                "candidate": candidate,
                "direction": "BULL",
                "policy": p,
                "policy_hash": digest(p),
                "acts_policy": engine.scorer.payload(),
                "global_policy": engine._global_policy(p),
                "decision_timestamp": AT - timedelta(seconds=15),
                "entry_timestamp": AT,
                "entry_credit": execution["value"],
                "entry_execution": execution,
                "forced_exit_time": "21:55:00",
                "defined_risk": candidate.defined_max_loss_per_lot,
                "instrument_id": "CRUDEOILM",
                "mark_at": None,
                "unrealized_gross": None,
            }
        )
        control.policy_json = canonical(doc["global_policy"])
        row = CommodityPaperPosition(
            id=doc["id"],
            instrument_id="CRUDEOILM",
            state="OPEN",
            observed_at=AT.isoformat(),
            document=canonical({**doc, "state": "OPEN"}),
        )
        session.add(row)
        session.flush()
        engine._emit(session, cursor, "SYNTHETIC_HISTORICAL_IMPORT", snapshot, sid, AT, (("position", row),))
    case[2]["CRUDEOILM"] = CommodityPaperEngine(case[1]["CRUDEOILM"])
    step(case, 1830)
    assert positions(case)[0][0] == "PENDING_EXIT"
    step(case, 1845)
    doc = next(doc for state, doc in positions(case) if state == "CLOSED")
    assert canonical(doc["policy"]) == original_policy and "long_leg" in doc["candidate"]
    assert doc["gross_realized_pnl"] == pytest.approx((doc["entry_credit"] - doc["exit_debit"]) * 10)
    verify(case[0], "CRUDEOILM")


# Re-export test fixtures and helpers for the shared PostgreSQL validation suite.
__all__ = [
    "timedelta",
    "json",
    "SimpleNamespace",
    "pytest",
    "select",
    "CommodityPaperEngine",
    "verify",
    "KotakSingleMarginReader",
    "authorized_margin_reader",
    "CommodityPaperControl",
    "CommodityPaperCursor",
    "CommodityPaperPosition",
    "NakedPaperPolicy",
    "candidates",
    "execute",
    "load_policy",
    "SingleMarginEvidence",
    "margin_for",
    "digest",
    "canonical",
    "json_value",
    "AT",
    "case",
    "policy",
    "positions",
    "raw",
    "research_case",
    "step",
]
