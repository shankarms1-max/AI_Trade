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


@pytest.mark.parametrize("terminal", ["CLOSED", "ENTRY_REJECTED"])
def test_terminal_trades_do_not_request_continuity(paper_case, terminal):  # noqa: F811
    case = paper_case
    if terminal == "ENTRY_REJECTED":
        from tests.test_forward_paper import change_short
        case.paper.observe(next_snapshot(case, mutate=change_short(case, ask=None)))
    else:
        case.paper.observe(next_snapshot(case))
        doc = trade(case)[1]
        tokens = {doc["candidate"][name]["instrument_token"] for name in ("short_leg", "long_leg")}

        def profit_book(raw):
            return raw.model_copy(update={"options": [item.model_copy(update={"bid": 19, "ask": 20})
                                                      if item.instrument_token in tokens else item
                                                      for item in raw.options]})

        case.paper.observe(next_snapshot(case, mutate=profit_book))
    assert trade(case)[0] == terminal
    assert required_paper_contracts(case.sessions) == []
    verify_journal(case.sessions)


def test_historical_null_evidence_loads_with_original_options(paper_case):  # noqa: F811
    case = paper_case
    with case.sessions() as session:
        raw = case.paper.raw(session, case.snapshot_id)
    assert raw.required_contracts is None
    assert raw.execution_options == raw.options
    assert case.repository.get(case.snapshot_id)["required_contracts"] is None


@pytest.mark.parametrize("response", [[], [{"exchange": "nse_fo", "exchange_token": "wrong"}],
                                      [{"exchange": "bse_fo", "exchange_token": "40611"}]])
def test_supplementary_missing_or_wrong_token_never_copies_old_book(market_snapshot, response):
    from tests.test_kotak_option_quotes import Client, contract
    old = contract(market_snapshot).model_copy(update={
        "bid": 100, "ask": 101, "source_market_timestamp": market_snapshot.timestamp_ist})
    broker = KotakMarketDataAdapter(Client(response))
    assert broker.capture_required_option_quotes([old]) == []


def test_supplementary_quote_is_fresh_and_strictly_parsed(market_snapshot):
    from tests.test_kotak_option_quotes import Client, contract, quote
    old = contract(market_snapshot).model_copy(update={
        "bid": 1, "ask": 2, "ltp": 3, "open_interest": 4, "volume": 5, "tick_size": .05})
    client = Client([quote()])
    broker = KotakMarketDataAdapter(client)
    fresh = broker.capture_required_option_quotes([old, old])[0]
    assert len(client.calls) == 1 and len(client.calls[0]["instrument_tokens"]) == 1
    assert (fresh.bid, fresh.ask, fresh.bid_quantity, fresh.ask_quantity) == (100.1, 100.2, 75, 150)
    assert (fresh.ltp, fresh.open_interest, fresh.volume) == (9999, 8888, 7777)
    assert fresh.source_market_timestamp.timestamp() == 1782374657
    assert fresh.tick_size is None
    assert ContractIdentity.of(fresh) == ContractIdentity.of(old)
    client.response = [quote(open_int="NaN", last_volume=-1, ltp=None, depth=None, lstup_time=None)]
    unknown = broker.capture_required_option_quotes([old])[0]
    assert all(getattr(unknown, name) is None for name in (
        "bid", "ask", "bid_quantity", "ask_quantity", "ltp", "open_interest", "volume", "source_market_timestamp"))
    client.response = []
    assert broker.capture_required_option_quotes([old]) == []


def test_supplementary_duplicate_or_ambiguous_tokens_fail_closed(market_snapshot):
    from tests.test_kotak_option_quotes import Client, contract, quote
    item = contract(market_snapshot)
    client = Client([quote(), quote()])
    broker = KotakMarketDataAdapter(client)
    assert broker.capture_required_option_quotes([item]) == []
    client.calls.clear()
    assert broker.capture_required_option_quotes([item, item.model_copy(update={"strike": item.strike + 50})]) == []
    assert client.calls == []


@pytest.mark.parametrize("mutation", [{"instrument_token": "unexpected"}, {"strike": 29000},
                                      {"expiry": None}, {"option_type": "PE"}])
def test_snapshot_rejects_supplementary_identity_substitution(market_snapshot, monkeypatch, mutation):
    broker = KotakMarketDataAdapter(SimpleNamespace())
    monkeypatch.setattr(broker, "get_nifty_spot", lambda: market_snapshot.nifty_spot)
    monkeypatch.setattr(broker, "get_nifty_expiries", lambda: [market_snapshot.expiry])
    monkeypatch.setattr(broker, "get_nifty_option_chain", lambda _: market_snapshot.options)
    monkeypatch.setattr(broker, "enrich_option_quotes", lambda items: items)
    required = market_snapshot.options[0].model_copy(update={"strike": 28000, "instrument_token": "nse_fo|held"})
    if mutation.get("expiry", "present") is None:
        mutation = {"expiry": required.expiry + timedelta(days=7)}
    monkeypatch.setattr(broker, "capture_required_option_quotes", lambda _: [required.model_copy(update=mutation)])
    with pytest.raises(ValueError, match="AMBIGUOUS_BROKER_QUOTES"):
        build_market_snapshot(broker, required_contracts=[required])


@pytest.mark.parametrize("enabled", [False, True])
def test_collector_wires_provider_only_for_forward_paper(monkeypatch, enabled):
    from worker import collector
    from tests.test_forward_paper import settings_for_test
    configuration = settings_for_test(forward_paper_enabled=enabled, database_url="sqlite:///offline-only")
    calls = []
    builder = object()

    def make_builder(settings, provider=None):
        assert settings is configuration
        assert (provider is not None) == enabled
        if provider:
            assert provider() == []
        return builder

    class Operations:
        def __init__(self, *_):
            pass

        def upsert_heartbeat(self, *_args, **_kwargs):
            pass

        def record_event(self, *_args, **_kwargs):
            pass

    class Scheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def add_listener(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(collector, "get_settings", lambda: configuration)
    monkeypatch.setattr(collector, "configure_logging", lambda *_: None)
    monkeypatch.setattr(collector, "build_engine", lambda *_: None)
    monkeypatch.setattr(collector, "build_session_factory", lambda *_: None)
    monkeypatch.setattr(collector, "required_paper_contracts", lambda _: calls.append("provider") or [])
    monkeypatch.setattr(collector, "make_live_snapshot_builder", make_builder)
    monkeypatch.setattr(collector, "make_after_snapshot_callback", lambda *_: None)
    monkeypatch.setattr(collector, "ObservabilityRepository", Operations)
    monkeypatch.setattr(collector, "BlockingScheduler", Scheduler)
    assert collector.main() == 0
    assert calls == (["provider"] if enabled else [])
