"""Synthetic contract-roll tests; no market calls and no production evidence."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.commodity_acts.broker import CommodityDataError, KotakCommodityMarketData, resolve_master
from app.commodity_acts.contracts import RenewalError, renew
from app.commodity_acts.paper_journal import verify
from app.research.manifest import digest
from tests import test_commodity_paper as fixtures
from tests.test_commodity_acts import AT, MarketClient, config, master
from tests.test_commodity_paper_controls import positions, latest

case = fixtures.case
research_case = fixtures.research_case


def contracts(instrument="CRUDEOILM"):
    rows = []
    for index in (0, 1):
        options_at = AT.replace(month=10 + index, day=15)
        future_at = options_at.replace(day=19)
        token = f"TEST_{instrument}_{index}_F"
        for original in master(instrument):
            row = dict(original)
            future = row["pOptionType"] == "XX"
            at = future_at if future else options_at
            row.update(
                pSymbol=token if future else f"{index}_{row['pSymbol']}",
                pTrdSymbol=f"{index}_{row['pTrdSymbol']}",
                pAssetCode="ASSET" if future else token,
                pExpiryDate=at.strftime("%d%b%Y"),
                lExpiryDate=int(at.replace(tzinfo=timezone.utc).timestamp()),
                lLotSize=10 if instrument == "CRUDEOILM" else 250,
                lPrecision=2,
                dTickSize=(100 if instrument == "CRUDEOILM" else 10) if future else 5,
            )
            rows.append(row)
    return rows


def policy(instrument="CRUDEOILM"):
    p = fixtures.policy(instrument)
    return p.model_copy(update={"evidence": p.evidence.model_copy(update={"tick_size": 0.05})})


def select(rows, at, instrument="CRUDEOILM", held=()):
    return resolve_master(
        rows,
        config().specification(instrument),
        at.date(),
        renewal_policy=policy(instrument),
        at=at,
        required_options=held,
    )


@pytest.mark.parametrize("instrument", ["CRUDEOILM", "NATGASMINI"])
def test_next_eligible_expiry_selects_its_actual_underlying_future(instrument):
    rows = contracts(instrument)
    at = AT.replace(day=14, hour=20)
    future, options = select(rows, at, instrument)
    assert future["resolved_token"] == f"TEST_{instrument}_1_F"
    assert future["resolved_expiry"].month == 11
    assert {o["resolved_expiry"].month for o in options} == {11}
    assert {o["pAssetCode"] for o in options} == {future["resolved_token"]}


@pytest.mark.parametrize("seconds,expected", [(59, 0), (60, 1), (61, 1)])
def test_pre_expiry_ttl_boundary_is_not_weakened(seconds, expected):
    at = AT.replace(day=14, hour=19, minute=58, second=0) + timedelta(seconds=seconds)
    future, _ = select(contracts(), at)
    assert future["resolved_token"] == f"TEST_CRUDEOILM_{expected}_F"


def held_leg(row):
    return {
        "expiry": datetime.strptime(row["pExpiryDate"], "%d%b%Y").date().isoformat(),
        "instrument_token": row["pSymbol"],
        "strike": row["dStrikePrice;"] / 100,
        "option_type": row["pOptionType"],
        "trading_symbol": row["pTrdSymbol"],
        "exchange": "mcx_fo",
    }


def test_held_option_pins_capture_after_cutoff_until_management_completes():
    rows = contracts()
    leg = held_leg(rows[1])
    future, options = select(rows, AT.replace(day=14, hour=21), held=[leg])
    assert future["resolved_token"] == "TEST_CRUDEOILM_0_F"
    assert any(r["resolved_token"] == leg["instrument_token"] for r in options)


@pytest.mark.parametrize("field,value", [("instrument_token", "WRONG"), ("strike", 1), ("trading_symbol", "WRONG")])
def test_held_identity_mismatch_fails_closed(field, value):
    rows = contracts()
    leg = held_leg(rows[1])
    leg[field] = value
    with pytest.raises(CommodityDataError, match="HELD_CONTRACT_METADATA"):
        select(rows, AT.replace(day=14, hour=21), held=[leg])


@pytest.mark.parametrize(
    "problem",
    [
        "missing_link",
        "mixed_link",
        "unavailable_future",
        "lot",
        "option_tick",
        "precision",
        "future_tick",
        "duplicate_future",
        "future_expiry",
    ],
)
def test_unverified_metadata_cannot_be_renewed(problem):
    rows = contracts()
    if problem == "missing_link":
        for row in rows:
            row.pop("pAssetCode", None)
    elif problem == "mixed_link":
        rows[1]["pAssetCode"] = "WRONG"
    elif problem == "unavailable_future":
        for row in rows[1:43]:
            row["pAssetCode"] = "UNKNOWN"
    elif problem == "lot":
        rows[1]["lLotSize"] = 999
    elif problem == "option_tick":
        rows[1]["dTickSize"] = 10
    elif problem == "precision":
        rows[1].pop("lPrecision")
    elif problem == "future_tick":
        rows[0]["dTickSize"] = 50
    elif problem == "duplicate_future":
        rows.append(dict(rows[0]))
    elif problem == "future_expiry":
        rows[0].update(pExpiryDate="14Oct2099", lExpiryDate=int(AT.replace(day=14, tzinfo=timezone.utc).timestamp()))
    with pytest.raises(CommodityDataError):
        select(rows, AT)


def capture(at=AT, instrument="CRUDEOILM", client=None, market=None):
    client = client or MarketClient(instrument, at)
    client.rows = contracts(instrument)
    market = market or KotakCommodityMarketData(client, clock=lambda: client.at, renewal_policy=policy(instrument))
    return market.capture(config().specification(instrument)), market, client


def test_metadata_is_cached_but_cutoff_reselection_occurs_without_more_discovery():
    raw, market, client = capture(at=AT.replace(day=14, hour=19, minute=58))
    assert raw.future_token == "TEST_CRUDEOILM_0_F"
    client.at = AT.replace(day=14, hour=20)
    rolled = market.capture(config().specification("CRUDEOILM"))
    assert rolled.future_token == "TEST_CRUDEOILM_1_F"
    assert sum(method == "search" for method, _ in client.calls) == 1
    client.at += timedelta(days=1)
    market.capture(config().specification("CRUDEOILM"))
    assert sum(method == "search" for method, _ in client.calls) == 2


def test_renewal_preserves_all_risk_rules_and_only_uses_causal_actual_evidence():
    raw, _, _ = capture()
    p = policy()
    before = p.model_dump()
    current = renew(p, raw, config().specification("CRUDEOILM"), AT + timedelta(seconds=1))
    assert current.evidence.future_token == raw.future_token
    assert current.evidence.option_expiry == raw.expiry
    assert current.evidence.valid_until == AT.replace(hour=22)
    assert current.evidence.verified_at == AT
    assert {k: v for k, v in current.model_dump().items() if k != "evidence"} == {
        k: v for k, v in before.items() if k != "evidence"
    }
    assert p.model_dump() == before
    assert digest(current) != digest(p)


@pytest.mark.parametrize("bad", ["future", "prior_day", "source", "lot", "tick", "long_ttl", "missing", "stale_quote"])
def test_future_stale_or_incompatible_evidence_blocks_entry(bad):
    raw, _, _ = capture()
    data = json.loads(raw.provenance["contract_evidence"])
    if bad == "future":
        data["verified_at"] = (AT + timedelta(seconds=1)).isoformat()
    elif bad == "prior_day":
        data["verified_at"] = (AT - timedelta(days=1)).isoformat()
    elif bad == "source":
        data["source"] = "UNVERIFIED_SYNTHETIC_SOURCE"
    elif bad == "lot":
        data["lot_size"] = 250
        data["rupees_per_point_per_lot"] = 250
    elif bad == "tick":
        data["tick_size"] = 0.1
    elif bad == "long_ttl":
        data["valid_until"] = (AT + timedelta(days=2)).isoformat()
    elif bad == "missing":
        data = {}
    raw = raw.model_copy(update={"provenance": {**raw.provenance, "contract_evidence": json.dumps(data)}})
    with pytest.raises(RenewalError):
        renew(
            policy(),
            raw,
            config().specification("CRUDEOILM"),
            AT + timedelta(seconds=31 if bad == "stale_quote" else 0),
        )


def test_failed_renewal_does_not_delay_frozen_position_exit_or_rewrite_policy(case):
    fixtures.step(case, 0)
    fixtures.step(case, 15)
    engine = case[2]["CRUDEOILM"]
    before = positions(case)[0][1]
    frozen_hash, frozen_policy = before["policy_hash"], before["policy"]
    engine.config = engine.config.model_copy(update={"auto_contract_renewal": True})
    fixtures.step(case, 30, scale=0.2)
    fixtures.step(case, 45, scale=0.2)
    after = positions(case)[0][1]
    assert after["state"] == "CLOSED"
    assert after["policy_hash"] == frozen_hash and after["policy"] == frozen_policy
    assert "CONTRACT_RENEWAL_EVIDENCE_UNAVAILABLE" in latest(case)["risk_blocks"]
    verify(case[0], "CRUDEOILM")


def test_daily_evidence_is_journaled_but_pending_and_open_policy_stays_frozen(case, monkeypatch):
    original = fixtures.raw
    engine = case[2]["CRUDEOILM"]
    engine.config = engine.config.model_copy(update={"auto_contract_renewal": True})

    def observed(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        evidence = engine.base_policy.evidence.model_copy(
            update={
                "verified_at": snapshot.request_started_at,
                "valid_until": snapshot.timestamp_ist.replace(hour=22, minute=0, second=0),
                "source": "KOTAK_MCX_MASTER_PASSETCODE_EXACT_LINK_V1",
            }
        )
        return snapshot.model_copy(
            update={"provenance": {**snapshot.provenance, "contract_evidence": evidence.model_dump_json()}}
        )

    monkeypatch.setattr(fixtures, "raw", observed)
    fixtures.step(case, 0)
    pending = positions(case)[0][1]
    assert pending["state"] == "PENDING_PAPER_ENTRY"
    frozen = (pending["policy_hash"], pending["policy"])
    assert pending["policy"]["evidence"]["valid_until"] != engine.base_policy.evidence.valid_until.isoformat()
    fixtures.step(case, 15)
    opened = positions(case)[0][1]
    assert opened["state"] == "OPEN"
    assert (opened["policy_hash"], opened["policy"]) == frozen
    assert latest(case)["contract_renewal_status"] == "VERIFIED"
    assert latest(case)["contract_evidence"]["verified_at"] != opened["policy"]["evidence"]["verified_at"]
    verify(case[0], "CRUDEOILM")
