"""Native entry evidence is synthetic; no live MCX calibration is claimed."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from app.acts_paper import acts_v2
from app.commodity_acts.entry import EntryPolicy, VERSION, direction
from app.commodity_acts.paper_naked import candidates, decide, execute
from app.commodity_acts.paper_models import CommodityPaperCursor
from app.commodity_acts.paper_journal import verify
from app.research.manifest import digest
from tests.test_acts_paper_v2 import _decision
from tests.test_commodity_paper import AT, case, policy, positions, raw, research_case, step


@pytest.mark.parametrize("sign,label", [(1, "BULL"), (-1, "BEAR")])
@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
def test_context_only_never_supplies_a_spread_candidate(monkeypatch, sign, label, instrument):
    ctx, _, _, config = _decision(sign)
    original = acts_v2._score
    calls = []

    def context_only(context, direction, market, candidate, settings):
        assert candidate is None
        calls.append(candidate)
        return original(context, direction, market, candidate, settings)

    monkeypatch.setattr(acts_v2, "_score", context_only)
    snapshot = raw(instrument)
    chosen, rejected = candidates(snapshot, label, ctx, policy(instrument), AT)
    assert chosen is not None
    result = decide(ctx, {}, chosen, rejected, [], config)
    assert calls and result["status"] == "ENTER"
    assert result["strategy_id"] == VERSION
    forbidden = {"long_leg", "spread_width", "credit_to_width", "defined_max_loss_per_lot"}
    assert not forbidden.intersection(chosen.model_dump())
    assert set(chosen.quote_evidence) == {"short"}
    assert "execution" not in result["directional_components"]
    assert "opportunity_score" not in result
    assert result["direction"] == label
    assert chosen.short_leg.option_type == ("PE" if sign > 0 else "CE")
    evidence = chosen.qualification["evidence"]
    assert evidence["option_token"] == chosen.short_leg.instrument_token
    assert evidence["future_token"] == snapshot.future_token
    assert evidence["otm_distance_points"] > 0
    assert evidence["bid_quantity_units"] >= snapshot.lot_size
    assert evidence["ask_quantity_units"] >= snapshot.lot_size
    assert evidence["option_expiry"] == snapshot.expiry.isoformat()
    assert evidence["source_age_seconds"] == 0
    assert all(check["status"] == "DIAGNOSTIC_ONLY" for check in chosen.qualification["checks"].values())


def test_direction_factors_are_exact_shared_values_and_percentage_bands():
    ctx, _, _, config = _decision()
    original = acts_v2.decide(ctx, {}, None, {}, [], config)
    actual = direction(ctx, {}, config)
    components = {key: value for key, value in original["score_components"].items() if key != "execution"}
    assert actual["directional_components"] == components
    assert actual["raw_context_points"] == sum(components.values())
    assert actual["raw_context_max"] == 86
    assert actual["directional_score"] == pytest.approx(100 * sum(components.values()) / 86, abs=0.0001)
    assert [
        actual["directional_parameters"][key]
        for key in ("watch_score", "tradeable_score", "strong_score", "very_strong_score")
    ] == [40, 55, 70, 85]
    assert "min_credit_to_width" not in actual["directional_parameters"]
    assert not any(key.startswith("execution_") for key in actual["directional_parameters"]["weights"])
    # Altering obsolete spread-execution scales cannot alter Commodity direction.
    alternate = replace(config, max_bid_ask_spread_pct=99, min_credit_to_width=0.9)
    assert direction(ctx, {}, alternate) == actual


@pytest.mark.parametrize(
    "change",
    [
        {"bid": None},
        {"ask": None},
        {"bid_quantity": 9},
        {"ask_quantity": 9},
        {"source_market_timestamp": None},
        {"source_market_timestamp": AT - timedelta(seconds=31)},
        {"expiry": AT.date()},
        {"open_interest": None},
        {"volume": None},
    ],
)
def test_bad_single_books_cannot_create_entries(change):
    snapshot = raw()
    snapshot = snapshot.model_copy(update={"options": [q.model_copy(update=change) for q in snapshot.options]})
    chosen, rejected = candidates(snapshot, "BULL", {}, policy(), AT)
    assert chosen is None and rejected


@pytest.mark.parametrize(
    "key", ["min_open_interest", "min_volume", "min_otm_observed_steps", "min_premium_to_stress_loss"]
)
def test_native_floors_are_explicit_positive_config_not_magic_defaults(key):
    p = EntryPolicy()
    assert getattr(p, key) is None
    for value in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            EntryPolicy(**{key: value})
    chosen, rejected = candidates(raw(), "BULL", {}, policy(), AT, entry_policy=EntryPolicy(**{key: 1e9}))
    assert chosen is None and any("BELOW_CONFIGURED_MINIMUM" in reason for reason in rejected)


def test_oi_and_volume_thresholds_are_rechecked_at_subsequent_fill():
    p = policy()
    current = raw()
    chosen, _ = candidates(current, "BULL", {}, p, AT, entry_policy=EntryPolicy(min_volume=1, min_open_interest=1))
    later = raw(seconds=15)
    later = later.model_copy(update={"options": [q.model_copy(update={"volume": 0}) for q in later.options]})
    fill, failure = execute(later, chosen.model_dump(mode="json"), p, AT + timedelta(seconds=16), entry=True, after=AT)
    assert fill is None and failure == "SINGLE_SHORT_VOLUME_BELOW_CONFIGURED_MINIMUM"
    # Reducing exposure must not be blocked by an entry-only participation floor.
    close, failure = execute(
        later, chosen.model_dump(mode="json"), p, AT + timedelta(seconds=16), entry=False, after=AT
    )
    assert close is not None and failure is None and close["action"] == "BUY"


def test_quote_permutation_is_deterministic_and_selection_does_not_change_direction():
    snapshot, p = raw(), policy()
    ctx, _, _, config = _decision()
    chosen, rejected = candidates(snapshot, "BULL", ctx, p, AT)
    other, _ = candidates(
        snapshot.model_copy(update={"options": list(reversed(snapshot.options))}), "BULL", ctx, p, AT
    )
    assert chosen == other
    actual = decide(ctx, {}, chosen, rejected, [], config)
    changed = chosen.model_copy(update={"executable_credit": 999})
    comparison = decide(ctx, {}, changed, rejected, [], config)
    assert actual["directional_score"] == comparison["directional_score"]
    assert actual["directional_components"] == comparison["directional_components"]
    assert sum(item["status"] == "SELECTED" for item in actual["candidate_evaluations"]) == 1


def test_risk_blocks_preserve_native_candidate_evidence(case):
    engine = case[2]["CRUDEOILM"]
    engine.policy = policy(kill_switch=True)
    step(case, 0)
    assert not positions(case)
    with case[0]() as session:
        doc = json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)
    signal = doc["decision"]
    assert signal["entry_qualification"]["candidate_status"] == "ELIGIBLE"
    assert signal["entry_qualification"]["status"] == "BLOCKED"
    assert "KILL_SWITCH" in signal["blockers"]


def test_entry_semantics_hash_is_prospective_and_risk_policy_unchanged(case):
    original = deepcopy(policy().model_dump(mode="json"))
    step(case, 0)
    _, pending = positions(case)[0]
    assert pending["policy"] == original
    assert pending["policy_hash"] == digest(policy())
    assert pending["entry_policy"]["version"] == VERSION
    assert pending["entry_policy_hash"]
    immutable_decision = deepcopy(pending["decision"])
    step(case, 15)
    _, opened = positions(case)[0]
    assert opened["decision"] == immutable_decision
    assert opened["entry_policy_hash"] == pending["entry_policy_hash"]
    verify(case[0], "CRUDEOILM")


__all__ = ["case", "research_case"]


def test_frozen_export_diagnostic_uses_only_recorded_values(case):
    from scripts.diagnose_commodity_single_short import diagnostic

    step(case, 0)
    step(case, 15)
    step(case, 30, scale=0.2)
    step(case, 45, scale=0.2)
    state, trade = positions(case)[0]
    assert state == "CLOSED"
    original = deepcopy(trade)
    report = diagnostic(trade)
    assert report["old_compatibility_score"] is None
    assert report["gross_pnl"] == trade["gross_realized_pnl"]
    assert report["commodity_native_qualification"] == trade["decision"]["entry_qualification"]
    assert report["qualification_reconstruction"] == "STORED_CAUSAL_EVIDENCE"
    assert trade == original
    historical = {"decision": {"opportunity_score": 70}, "gross_realized_pnl": -12}
    report = diagnostic(historical)
    assert report["old_compatibility_score"] == 70 and report["gross_pnl"] == -12
    assert report["commodity_native_qualification"] is None
    assert report["qualification_reconstruction"] == "NOT_RECONSTRUCTED_MISSING_CAUSAL_INPUTS"


@pytest.mark.parametrize(
    "instrument,cash,stress,daily", [("CRUDEOILM", 2500, 6000, 6000), ("NATGASMINI", 1500, 8000, 8000)]
)
def test_approved_risk_and_lifecycle_fields_are_not_replaced_by_qualification(instrument, cash, stress, daily):
    configured = policy(
        instrument,
        max_loss_per_trade=cash,
        max_stress_loss_per_trade=stress,
        daily_loss_limit=daily,
        max_trades_per_session=2,
    )
    assert configured.lots == configured.max_positions == 1
    assert configured.max_trades_per_session == 2
    assert configured.max_loss_per_trade == cash
    assert configured.max_stress_loss_per_trade == stress and configured.daily_loss_limit == daily
    assert configured.max_quote_age_seconds == 30
    assert configured.profit_capture_pct == 50 and configured.stop_credit_multiple == 2
    assert configured.trailing_activation_pct == 25 and configured.trailing_giveback_pct == 15
    assert configured.max_holding_minutes == 30


def test_legacy_research_policy_transitions_by_append_without_rewriting_evidence(research_case):
    from sqlalchemy import select
    from app.commodity_acts.models import CommodityActsDecision, CommodityActsEvent
    from app.commodity_acts.repository import verify_journal
    from app.commodity_acts.service import CommodityResearch

    sessions, services, clock = research_case
    service = services["CRUDEOILM"]
    previous = {key: value for key, value in service.policy.items() if key != "directional_context"}
    previous["stage"] = "SCORE_TRANSFERABILITY"
    service.policy, service.policy_hash = previous, digest(previous)
    first_id, _ = service.observe(raw())
    with sessions() as session:
        first = session.scalar(
            select(CommodityActsDecision).where(CommodityActsDecision.snapshot_id == first_id)
        ).document
        journal = [(row.sequence, row.payload, row.event_hash) for row in session.scalars(select(CommodityActsEvent))]
    from tests.test_scalper import settings

    resumed = CommodityResearch(sessions, settings(), service.config, service.spec, clock=lambda: clock.at)
    clock.at = AT + timedelta(seconds=16)
    resumed.observe(raw(seconds=15))
    with sessions() as session:
        assert (
            session.scalar(select(CommodityActsDecision).where(CommodityActsDecision.snapshot_id == first_id)).document
            == first
        )
        after = [
            (row.sequence, row.payload, row.event_hash)
            for row in session.scalars(select(CommodityActsEvent).order_by(CommodityActsEvent.sequence))
        ]
        assert after[: len(journal)] == journal and len(after) == len(journal) + 1
    verify_journal(sessions, "CRUDEOILM")


def test_mismatched_single_option_direction_cannot_enter():
    ctx, _, _, config = _decision(1)
    chosen, rejected = candidates(raw(), "BEAR", ctx, policy(), AT)
    result = decide(ctx, {}, chosen, rejected, [], config)
    assert result["status"] == "NO_SETUP"
    assert "SINGLE_SHORT_DIRECTION_MISMATCH" in result["hard_blockers"]


def test_approved_two_entry_limit_blocks_a_third_but_does_not_delay_exits(case):
    case[2]["CRUDEOILM"].policy = policy(max_trades_per_session=2)
    step(case, 0)
    step(case, 15)
    step(case, 30, scale=0.2)
    step(case, 45, scale=0.2)
    step(case, 60, scale=0.2)
    step(case, 1860, scale=0.2)
    step(case, 1875, scale=0.2)
    rows = positions(case)
    assert len(rows) == 2 and all(state == "CLOSED" for state, _ in rows)
    with case[0]() as session:
        latest = json.loads(session.get(CommodityPaperCursor, "CRUDEOILM").latest_json)
    assert "SESSION_TRADE_LIMIT" in latest["decision"]["hard_blockers"]
    verify(case[0], "CRUDEOILM")


def test_configured_alpha_veto_and_missing_future_remain_hard_blocks():
    ctx, _, _, config = _decision(1, "STRONG_BEAR")
    config = replace(config, veto_strong_opposite_alpha=True)
    result = direction(ctx, {}, config)
    assert "CONFIGURED_STRONG_OPPOSITE_ALPHA_VETO" in result["hard_blockers"]
    assert result["status"] == "NO_SETUP"
    ctx["future_instrument_id"] = None
    assert "FUTURES_REFERENCE_UNAVAILABLE" in direction(ctx, {}, config)["hard_blockers"]


def test_read_projection_exposes_new_direction_and_preserves_legacy_labels():
    from app.commodity_acts.read import view

    ctx, _, _, config = _decision()
    actual = direction(ctx, {}, config)
    projected = view({"stage": "DIRECTIONAL_CONTEXT_RESEARCH", "acts": actual})
    for key in (
        "directional_score",
        "directional_score_max",
        "raw_context_points",
        "raw_context_max",
        "directional_components",
        "directional_parameters",
    ):
        assert projected["acts"][key] == actual[key]
    legacy = view(
        {"stage": "SCORE_TRANSFERABILITY", "research_scope": "historical scope", "acts": {"opportunity_score": 60}}
    )
    assert legacy["stage"] == "SCORE_TRANSFERABILITY" and legacy["research_scope"] == "historical scope"
    assert legacy["acts"]["opportunity_score"] == 60 and "directional_score" not in legacy["acts"]
