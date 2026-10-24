"""Runtime integration and pre-eb6 schema compatibility. All evidence is synthetic."""

from copy import deepcopy
import json
from pathlib import Path

from sqlalchemy import select

from app.commodity_acts.entry import EntryPolicy, VERSION
from app.commodity_acts.paper_engine import CommodityPaperEngine
from app.commodity_acts.paper_journal import emit, verify
from app.commodity_acts.paper_models import (
    CommodityPaperControl,
    CommodityPaperCursor,
    CommodityPaperEvent,
    CommodityPaperFill,
    CommodityPaperOrder,
    CommodityPaperPosition,
)
from app.commodity_acts.paper_naked import load_policy
from app.commodity_acts.paper_read import page, summary
from app.research.manifest import canonical, digest
from tests import test_commodity_paper as fixtures
from tests.test_commodity_paper_controls import latest

case = fixtures.case
research_case = fixtures.research_case
LEGACY = Path(__file__).parent / "fixtures" / "commodity_pre_eb6_closed.json"


def test_legacy_closed_documents_and_journals_read_and_restart_without_requalification(case):
    records = json.loads(LEGACY.read_text())
    assert records["provenance"].startswith("SYNTHETIC_")
    sessions, research, _, _ = case
    with sessions.begin() as session:
        control = session.get(CommodityPaperControl, 1)
        control.lock_version = 1
        control.policy_json = canonical({"positions": 2, "risk": 14000, "daily_loss": 14000})
        for instrument in ("CRUDEOILM", "NATGASMINI"):
            cursor = session.get(CommodityPaperCursor, instrument)
            changes = []
            for document in records["positions"]:
                if document["instrument_id"] != instrument:
                    continue
                row = CommodityPaperPosition(
                    id=document["id"],
                    instrument_id=instrument,
                    state="CLOSED",
                    observed_at=document["observed_at"],
                    document=canonical(document),
                )
                session.add(row)
                session.flush()
                changes.append(("position", row))
                for kind in ("ENTRY", "EXIT"):
                    at = document["entry_timestamp" if kind == "ENTRY" else "exit_timestamp"]
                    oid = document["entry_order_id" if kind == "ENTRY" else "exit_order_id"]
                    od = {
                        "id": oid,
                        "position_id": row.id,
                        "instrument_id": instrument,
                        "kind": kind,
                        "state": "FILLED",
                        "observed_at": at,
                    }
                    order = CommodityPaperOrder(
                        id=oid,
                        instrument_id=instrument,
                        position_id=row.id,
                        state="FILLED",
                        observed_at=at,
                        document=canonical(od),
                    )
                    session.add(order)
                    session.flush()
                    changes.append(("order", order))
                    fd = {
                        "id": digest({"order_id": oid}),
                        "order_id": oid,
                        "position_id": row.id,
                        "instrument_id": instrument,
                        "kind": kind,
                        "observed_at": at,
                        "execution": document["entry_execution" if kind == "ENTRY" else "mark_execution"],
                        "broker_orders_enabled": False,
                    }
                    fill = CommodityPaperFill(
                        id=fd["id"], instrument_id=instrument, order_id=oid, observed_at=at, document=canonical(fd)
                    )
                    session.add(fill)
                    session.flush()
                    changes.append(("fill", fill))
            emit(
                session,
                cursor,
                "EXIT_FILLED",
                1,
                fixtures.raw(instrument),
                fixtures.AT,
                changes=changes,
                global_sequence=1,
            )

    def evidence():
        with sessions() as session:
            return {
                "positions": [
                    (r.id, r.document)
                    for r in session.scalars(select(CommodityPaperPosition).order_by(CommodityPaperPosition.id))
                ],
                "events": [
                    (r.instrument_id, r.sequence, r.payload, r.event_hash)
                    for r in session.scalars(
                        select(CommodityPaperEvent).order_by(
                            CommodityPaperEvent.instrument_id, CommodityPaperEvent.sequence
                        )
                    )
                ],
                "orders": [
                    (r.id, r.document)
                    for r in session.scalars(select(CommodityPaperOrder).order_by(CommodityPaperOrder.id))
                ],
                "fills": [
                    (r.id, r.document)
                    for r in session.scalars(select(CommodityPaperFill).order_by(CommodityPaperFill.id))
                ],
            }

    before = evidence()
    assert (len(before["positions"]), len(before["orders"]), len(before["fills"])) == (4, 8, 8)
    for instrument in ("CRUDEOILM", "NATGASMINI"):
        CommodityPaperEngine(research[instrument])
        verify(sessions, instrument)
        old = {r["id"]: r for r in records["positions"] if r["instrument_id"] == instrument}
        for row in page(sessions, instrument, "positions", at=fixtures.AT)["items"]:
            assert row == old[row["id"]]
            assert row["policy_hash"] == digest(load_policy(row["policy"]))
            assert "entry_qualification" not in row["decision"] and "qualification" not in row["candidate"]
            assert row["decision"]["opportunity_score"] == 65
        paper = next(
            r
            for r in summary(sessions, research[instrument].config, fixtures.AT)["instruments"]
            if r["instrument_id"] == instrument
        )
        assert paper["closed_trades"] == 2
        assert paper["realized_gross"] == sum(r["gross_realized_pnl"] for r in old.values())
        assert paper["realized_net"] == (
            None if instrument == "CRUDEOILM" else sum(r["net_realized_pnl"] for r in old.values())
        )
    assert evidence() == before


def test_renewed_policy_and_native_floors_both_control_subsequent_fill(case, monkeypatch):
    original = fixtures.raw
    engine = case[2]["CRUDEOILM"]
    engine.config = engine.config.model_copy(update={"auto_contract_renewal": True})
    engine.entry_policy = EntryPolicy(min_volume=1, min_open_interest=1)

    def observed(*args, **kwargs):
        row = original(*args, **kwargs)
        evidence = engine.base_policy.evidence.model_copy(
            update={
                "verified_at": row.request_started_at,
                "valid_until": row.timestamp_ist.replace(hour=22, minute=0, second=0),
                "source": "KOTAK_MCX_MASTER_PASSETCODE_EXACT_LINK_V1",
            }
        )
        return row.model_copy(
            update={"provenance": {**row.provenance, "contract_evidence": evidence.model_dump_json()}}
        )

    monkeypatch.setattr(fixtures, "raw", observed)
    fixtures.step(case, 0)
    pending = fixtures.positions(case)[0][1]
    assert pending["decision"]["strategy_id"] == VERSION
    assert pending["entry_policy"]["min_volume"] == 1
    assert "long_leg" not in pending["candidate"] and "credit_to_width" not in pending["candidate"]
    assert latest(case)["contract_renewal_status"] == "VERIFIED"
    assert pending["policy"]["evidence"] == latest(case)["contract_evidence"]
    frozen = deepcopy((pending["policy_hash"], pending["entry_policy_hash"], pending["policy"]))

    def no_volume(rows):
        return [{**row, "last_volume": "0"} if not row["exchange_token"].endswith("_F") else row for row in rows]

    fixtures.step(case, 15, corrupt=no_volume)
    waiting = fixtures.positions(case)[0]
    assert waiting[0] == "PENDING_PAPER_ENTRY"
    with case[0]() as session:
        order = session.get(CommodityPaperOrder, pending["entry_order_id"])
        assert json.loads(order.document)["last_attempt"]["reason"] == "SINGLE_SHORT_VOLUME_BELOW_CONFIGURED_MINIMUM"
    fixtures.step(case, 30)
    opened = fixtures.positions(case)[0][1]
    assert opened["state"] == "OPEN"
    assert opened["entry_timestamp"] > pending["decision_timestamp"]
    assert opened["entry_execution"]["action"] == "SELL"
    assert (opened["policy_hash"], opened["entry_policy_hash"], opened["policy"]) == frozen
    assert engine.entry_policy.min_volume == 1
    verify(case[0], "CRUDEOILM")


__all__ = ["case", "research_case"]
