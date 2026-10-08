"""Collector -> persisted snapshot -> real forward-paper exact-contract execution."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.broker.kotak.market_data import KotakMarketDataAdapter
from app.broker.kotak.depth import quote_identity
from app.data.snapshot import build_market_snapshot
from app.features.engine import build_market_features
from app.paper.continuity import required_paper_contracts
from app.paper.service import verify_journal
from app.research.quotes import ContractIdentity, exact_contract
from tests.test_forward_paper import (  # noqa: F401
    isolated_settings, paper_case, trade, events, next_snapshot,
)


def capture(case, monkeypatch, *, move=True, omit=None, duplicate_requests=False):
    at = case.raw.timestamp_ist + timedelta(seconds=180)
    original = case.raw.options
    # Move in the favourable direction so this test isolates quoting from structural exits.
    direction = 1 if trade(case)[1]["candidate"]["strategy_type"] == "BULL_PUT_SPREAD" else -1
    shift = direction * 1500 if move else 0
    chain = [item.model_copy(update={
        "strike": item.strike + shift,
        "instrument_token": f"new-{item.instrument_token}" if move else item.instrument_token,
        "source_market_timestamp": at}) for item in original]
    by_token = {quote_identity(item): item for item in [*original, *chain]}
    requests = []

    def quotes(*, instrument_tokens, quote_type):
        assert quote_type == "all"
        requests.extend(instrument_tokens)
        result = []
        for request in instrument_tokens:
            key = request["exchange_segment"], request["instrument_token"]
            item = by_token[key]
            if key[1] == omit:
                continue
            result.append({"exchange": key[0], "exchange_token": key[1],
                           "lstup_time": str(int(at.timestamp())), "ltp": str(item.ltp),
                           "open_int": str(item.open_interest), "last_volume": str(item.volume),
                           "depth": {"buy": [{"price": item.bid, "quantity": 10000}],
                                     "sell": [{"price": item.ask, "quantity": 10000}]}})
        return result

    broker = KotakMarketDataAdapter(SimpleNamespace(quotes=quotes))
    monkeypatch.setattr(broker, "get_nifty_spot", lambda: case.raw.nifty_spot + shift)
    monkeypatch.setattr(broker, "get_nifty_expiries", lambda: [case.raw.expiry])
    monkeypatch.setattr(broker, "get_nifty_option_chain", lambda expiry: chain)
    monkeypatch.setattr(broker, "get_nifty_lot_size", lambda: case.raw.lot_size)
    monkeypatch.setattr(broker, "get_india_vix", lambda: case.raw.india_vix)
    monkeypatch.setattr("app.data.snapshot.datetime", SimpleNamespace(now=lambda tz: at))
    required = required_paper_contracts(case.sessions)
    assert all(item.bid is None and item.ltp is None and item.volume is None
               and item.source_market_timestamp is None for item in required)
    raw = build_market_snapshot(
        broker, required_contracts=required * (2 if duplicate_requests else 1))
    saved = case.repository.save_market_snapshot(raw, at)
    case.clock[0] = at
    with case.sessions() as session:
        persisted = case.paper.raw(session, saved.snapshot_id)
    return saved.snapshot_id, persisted, requests


@pytest.mark.parametrize("pending", [False, True])
def test_held_and_pending_exact_legs_survive_rolling_chain(
    paper_case, monkeypatch, pending,  # noqa: F811
):
    case = paper_case
    if not pending:
        case.paper.observe(next_snapshot(case))
        assert trade(case)[0] == "OPEN"
    required = required_paper_contracts(case.sessions)
    snapshot_id, raw, requested = capture(case, monkeypatch, duplicate_requests=True)
    assert len(raw.required_contracts.requested) == len(raw.required_contracts.quotes) == 2
    assert all(ContractIdentity.of(item) not in {ContractIdentity.of(leg) for leg in raw.options}
               for item in required)
    for leg in required:
        key = quote_identity(leg)
        assert sum(item == {"exchange_segment": key[0], "instrument_token": key[1]}
                   for item in requested) == 1
        found, reason = exact_contract(raw, ContractIdentity.of(leg))
        assert found and reason == "OK" and found.source_market_timestamp == raw.timestamp_ist
    identities = [ContractIdentity.of(item) for item in raw.execution_options]
    assert len(identities) == len(set(identities))
    # Extra held contracts do not change P14 indicators / chain membership.
    assert build_market_features(snapshot_id, raw, []).model_dump() == build_market_features(
        snapshot_id, raw.model_copy(update={"required_contracts": None}), []).model_dump()
    case.paper.observe(snapshot_id)
    assert trade(case)[0] == "OPEN"
    expected = "ENTRY_EXECUTION" if pending else "MARK"
    assert any(kind == expected and event["snapshot_id"] == snapshot_id
               for kind, event in events(case))
    verify_journal(case.sessions)


def test_true_missing_exact_quote_still_unresolved(paper_case, monkeypatch):  # noqa: F811
    case = paper_case
    case.paper.observe(next_snapshot(case))
    missing = quote_identity(required_paper_contracts(case.sessions)[0])[1]
    snapshot_id, raw, _ = capture(case, monkeypatch, omit=missing)
    assert len(raw.required_contracts.quotes) == 1
    case.paper.observe(snapshot_id)
    state, doc = trade(case)
    assert state == "UNRESOLVED_EXPOSURE"
    assert doc["unresolved_reason"] == "MISSING_EXACT_CONTRACT"
    assert required_paper_contracts(case.sessions) == []
    verify_journal(case.sessions)


def test_already_in_chain_is_not_requested_twice(paper_case, monkeypatch):  # noqa: F811
    _, raw, requests = capture(paper_case, monkeypatch, move=False)
    assert raw.required_contracts.requested == raw.required_contracts.quotes == []
    assert len(requests) == len(raw.options)


def test_no_exposure_preserves_chain_capture(session_factory, market_snapshot, monkeypatch):
    case = SimpleNamespace(raw=market_snapshot)
    broker = KotakMarketDataAdapter(SimpleNamespace())
    monkeypatch.setattr(broker, "get_nifty_spot", lambda: case.raw.nifty_spot)
    monkeypatch.setattr(broker, "get_nifty_expiries", lambda: [case.raw.expiry])
    monkeypatch.setattr(broker, "get_nifty_option_chain", lambda expiry: case.raw.options)
    monkeypatch.setattr(broker, "get_india_vix", lambda: None)
    monkeypatch.setattr(broker, "enrich_option_quotes", lambda items: items)
    monkeypatch.setattr(broker, "capture_required_option_quotes",
                        lambda _: pytest.fail("extra request"))
    raw = build_market_snapshot(
        broker, required_contracts=required_paper_contracts(session_factory))
    assert raw.required_contracts is None and raw.options == market_snapshot.options
