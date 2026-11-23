"""Read-only AST Direct monitoring API: authoritative status, bounded pages, forward paper performance."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adaptive_supertrend.direct.api import create_app, performance
from app.adaptive_supertrend.direct.models import DirectCursor, DirectEvent, DirectTrade
from app.adaptive_supertrend.direct.service import DirectPaperService
from app.db.session import build_session_factory
from tests.test_ast_direct import POLICY, tape

TOKEN = "t" * 48
PATHS = ("/api/ast-direct/status", "/api/ast-direct/trades", "/api/ast-direct/events", "/api/ast-direct/performance")


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "api-token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    return str(path)


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AST_DIRECT_CONFIG_JSON", POLICY.model_copy(update={"history_sessions": 15}).model_dump_json())
    monkeypatch.setenv("AST_DIRECT_ENABLED", "true")
    monkeypatch.setenv("AST_DIRECT_HEARTBEAT_FILE", str(tmp_path / "missing-heartbeat.json"))
    monkeypatch.setenv("AST_DIRECT_KILL_SWITCH_FILE", str(tmp_path / "KILL"))
    monkeypatch.delenv("AST_DIRECT_KILL_SWITCH", raising=False)


def client(sessions, token_file):
    return TestClient(create_app(sessions, token_file=token_file))


def get(api, path, **params):
    return api.get(path, params=params, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.fixture
def empty(engine):
    sessions = build_session_factory(engine)
    with sessions.begin() as db:
        db.add(DirectCursor(id=1, sequence=0, head_hash="GENESIS", lock_version=0, state_json="{}", latest_json="{}"))
    return sessions


@pytest.fixture
def traded(engine):
    sessions, ids = tape(engine)
    service = DirectPaperService(sessions, POLICY)
    for snapshot_id in ids:
        service.process_snapshot(snapshot_id)
    return sessions, ids


def test_empty_database_before_market_is_valid_not_an_error(empty, token_file, tmp_path):
    api = client(empty, token_file)
    status = get(api, PATHS[0]).json()
    assert status["mode"] == "PAPER" and status["live_trading"] is False
    assert status["worker"] == "STOPPED" and status["worker_enabled"] is True and status["kill_switch"] is False
    assert status["position"] is None and status["position_detail"] is None and status["trades_today"] == []
    assert status["journal_head"] == "GENESIS" and status["journal_sequence"] == 0
    assert status["journal_verification"] == "VALID" and status["last_observation_at"] is None
    fixed = {"atr_period": 22, "multiplier": 3.9, "regime": "NONE", "optimizer": "DISABLED"}
    assert status["fixed_parameters"] == fixed
    assert status["policy_configuration"]["history_sessions"] == 15
    assert status["fill_basis"] == "FUTURES_LTP_PAPER_V1" and "LTP" in status["fill_basis_note"]
    assert status["cumulative_closed_trades"] == 0 and status["cumulative_net_research_points"] is None
    for key in ("strategy_id", "strategy_version", "heartbeat", "trading_date", "in_session", "policy_hash",
                "futures_instrument", "last_completed_bar", "opening_range", "ast", "status", "blockers",
                "fill_blocker", "mark", "today_gross_points", "cumulative_gross_points", "forced_exit_status"):
        assert key in status
    assert get(api, PATHS[1]).json() == {"items": [], "total": 0, "limit": 50, "offset": 0, "has_more": False,
                                         "fill_basis": "FUTURES_LTP_PAPER_V1"}
    events = get(api, PATHS[2]).json()
    assert events["items"] == [] and events["journal_head"] == "GENESIS"
    perf = get(api, PATHS[3]).json()
    assert perf["label"] == "FORWARD_PAPER_PERFORMANCE" and perf["closed_trades"] == 0
    assert perf["win_rate"] is None and perf["daily"] == [] and perf["max_realized_drawdown_points"] == 0
    (tmp_path / "KILL").touch()
    assert get(api, PATHS[0]).json()["kill_switch"] is True


def test_authentication_is_required_and_fails_closed(empty, token_file, tmp_path):
    api = client(empty, token_file)
    health = api.get("/health")
    assert health.json() == {"status": "ok", "service": "ast-direct-read-api", "mode": "READ_ONLY",
                             "strategy_id": "AST_FUTURES_V1_DIRECT"}
    for path in PATHS:
        assert api.get(path).status_code == 401
        assert api.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert api.get(path, headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client(empty, str(tmp_path / "absent")).get(PATHS[0]).status_code == 503
    loose = tmp_path / "loose"
    loose.write_text(TOKEN)
    os.chmod(loose, 0o644)
    assert client(empty, str(loose)).get(PATHS[0], headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 503
    assert get(client(empty, None), PATHS[0]).status_code == 503


def test_only_get_routes_exist_and_mutations_are_rejected(traded, token_file):
    sessions, _ = traded
    api = client(sessions, token_file)
    methods = {method for route in api.app.routes for method in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}
    with sessions() as db:
        before = (db.scalar(select(func.count()).select_from(DirectEvent)), db.get(DirectCursor, 1).head_hash,
                  [row.document for row in db.scalars(select(DirectTrade))])
    for path in (*PATHS, "/health", "/api/ast-direct/acknowledge", "/api/ast-direct/kill-switch"):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(api, method)(path, headers={"Authorization": f"Bearer {TOKEN}"})
            assert response.status_code == 405 and response.json() == {"detail": "READ_ONLY"}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.get(path).status_code == 404
    for path in PATHS:
        get(api, path)
    with sessions() as db:
        after = (db.scalar(select(func.count()).select_from(DirectEvent)), db.get(DirectCursor, 1).head_hash,
                 [row.document for row in db.scalars(select(DirectTrade))])
    assert before == after


def test_status_trades_events_and_performance_from_real_lifecycle(traded, token_file):
    sessions, _ = traded
    api = client(sessions, token_file)
    status = get(api, PATHS[0])
    assert status.headers["cache-control"] == "private, no-store"
    body = status.json()
    assert body["journal_verification"] == "VALID" and body["journal_sequence"] > 0
    assert body["futures_instrument"]["instrument_id"] == "FUT" and body["opening_range"]["high"] is not None
    assert body["ast"]["atr_period"] == 22 and body["ast"]["multiplier"] == 3.9
    page = get(api, PATHS[1]).json()
    assert page["total"] == 1 and len(page["items"]) == 1
    trade = page["items"][0]
    assert trade["state"] == "CLOSED" and trade["side"] == "LONG" and trade["exit_reason"] == "AST_FLIP"
    assert trade["fill_basis"] == "FUTURES_LTP_PAPER_V1" and trade["atr_period"] == 22 and trade["multiplier"] == 3.9
    assert trade["gross_inr"] == trade["gross_points"] * 65 and trade["opening_range"]["high"] is not None
    assert set(trade) >= {"signal_price", "mfe_points", "mae_points", "net_research_points", "ast", "quantity"}
    assert get(api, PATHS[1], state="OPEN").json()["total"] == 0
    assert get(api, PATHS[1], side="SHORT").json()["total"] == 0
    assert get(api, PATHS[1], date=trade["trading_date"], side="LONG", state="CLOSED").json()["total"] == 1
    assert get(api, PATHS[1], date="2020-01-01").json()["total"] == 0
    assert get(api, PATHS[1], offset=1).json()["items"] == []

    lifecycle = get(api, PATHS[2], scope="lifecycle", limit=10).json()
    kinds = [item["event_type"] for item in lifecycle["items"]]
    assert kinds == ["AST_FLIP_EXIT", "PENDING_PAPER_EXIT", "ENTRY", "PENDING_PAPER_ENTRY"]
    assert lifecycle["items"][0]["summary"]["exit_reason"] == "AST_FLIP"
    everything = get(api, PATHS[2], limit=2).json()
    assert everything["total"] == body["journal_sequence"] and everything["has_more"] is True
    latest = everything["items"][0]
    assert latest["event_type"] == "OBSERVATION" and latest["sequence"] == body["journal_sequence"]
    assert everything["items"][0]["event_hash"] == body["journal_head"] == everything["journal_head"]
    second = get(api, PATHS[2], limit=2, offset=2).json()
    assert [i["sequence"] for i in second["items"]] == [body["journal_sequence"] - 2, body["journal_sequence"] - 3]

    perf = get(api, PATHS[3]).json()
    assert perf["closed_trades"] == 1 and perf["gross_points"] == trade["gross_points"]
    assert perf["daily"] == [{"date": trade["trading_date"], "trades": 1, "points": trade["gross_points"],
                              "cumulative_points": trade["gross_points"]}]
    text = json.dumps([status.json(), page, lifecycle, perf])
    assert TOKEN not in text and "KOTAK" not in text.upper() and "DATABASE_URL" not in text


def test_open_position_detail_and_paper_mark(engine, token_file):
    sessions, ids = tape(engine)
    service = DirectPaperService(sessions, POLICY)
    for snapshot_id in ids[:90]:
        service.process_snapshot(snapshot_id)
    body = get(client(sessions, token_file), PATHS[0]).json()
    detail = body["position_detail"]
    assert body["status"] == "OPEN" and detail["state"] == "OPEN" and detail["side"] == "LONG"
    assert detail["quantity"] == 65 and detail["fill_basis"] == "FUTURES_LTP_PAPER_V1"
    assert body["mark"]["trade_id"] == detail["trade_id"]
    assert body["position_mark_gross_inr"] == pytest.approx(body["mark"]["points"] * 65)


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 100001}, {"state": "HACKED"},
    {"side": "BUY"}, {"date": "yesterday"},
])
def test_invalid_trade_filters_are_rejected(empty, token_file, params):
    assert get(client(empty, token_file), PATHS[1], **params).status_code == 422


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"scope": "raw"}, {"offset": -5}])
def test_invalid_event_filters_are_rejected(empty, token_file, params):
    assert get(client(empty, token_file), PATHS[2], **params).status_code == 422


def test_forward_paper_performance_aggregation():
    def doc(day, points, minute, friction=None, state="CLOSED"):
        return {"trade_id": f"{day}-{minute}", "state": state, "trading_date": day, "closed_date": day, "side": "LONG",
                "gross_points": points, "gross_inr": points * 65 if points is not None else None,
                "net_research_points": None if friction is None else points - friction,
                "exit_timestamp": f"{day}T10:{minute:02d}:00+05:30"}

    result = performance([doc("2026-11-23", 40, 0), doc("2026-11-23", -10, 5), doc("2026-11-24", -50, 0),
                          doc("2026-11-25", 25, 0), doc("2026-11-25", None, 9, state="UNRESOLVED_EXPOSURE")])
    assert (result["closed_trades"], result["wins"], result["losses"]) == (4, 2, 2)
    assert result["win_rate"] == 0.5 and result["gross_points"] == 5 and result["average_points"] == 1.25
    assert result["gross_inr"] == 325 and result["net_research_points"] is None
    assert result["best_trade"]["gross_points"] == 40 and result["worst_trade"]["gross_points"] == -50
    assert [row["cumulative_points"] for row in result["daily"]] == [30, -20, 5]
    assert result["max_realized_drawdown_points"] == 60  # 40 -> -20 on closed-trade equity
    assert result["current_peak_points"] == 40 and result["current_drawdown_points"] == 35
    assert result["drawdown_basis"] == "CLOSED_TRADE_EQUITY_FROM_ZERO_NOT_MARK_TO_MARKET"
    assert result["excluded_unresolved_exposures"] == 1
    netted = performance([doc("2026-11-23", 10, 0, friction=2), doc("2026-11-23", -4, 1, friction=2)])
    assert netted["net_research_points"] == 2
