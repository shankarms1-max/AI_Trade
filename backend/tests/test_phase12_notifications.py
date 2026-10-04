from datetime import date, datetime, timedelta
from types import SimpleNamespace
import sys
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import get_settings
from app.db.models import OperationalEventRecord
from app.db.session import get_session_factory
from app.main import app
from app.notifications.formatter import (
    format_candidate, format_daily_summary, format_operational_alert, format_regime,
    format_risk, format_shadow_entry, format_shadow_exit,
)
from app.notifications.models import (
    NotificationEventCode, NotificationPriority, NotificationRequest,
    TelegramHealthStatus, TelegramSendResult,
)
from app.notifications.repository import NotificationRepository
from app.notifications.router import NotificationRouter, is_important_risk_rejection, should_notify_directional
from app.notifications.service import NotificationService
from app.notifications.telegram import TelegramClient
from app.pipeline.service import make_after_snapshot_callback
from app.risk.models import EvaluationContext
from app.risk.service import build_and_store_risk
from app.shadow.repository import ShadowRepository
from app.shadow.service import build_shadow_entry, config_from_settings as shadow_config
from tests.test_phase2_persistence import save
from tests.test_phase7_risk import approved_config, persisted_context

IST = ZoneInfo("Asia/Kolkata")
MONDAY = datetime(2026, 10, 5, 10, 0, tzinfo=IST)


def configured_settings(**updates):
    return get_settings().model_copy(update={
        "telegram_enabled": True,
        "telegram_bot_token": SecretStr("123456:TEST_SECRET"),
        "telegram_chat_id": SecretStr("987654"),
        **updates,
    })


class FakeTelegram:
    def __init__(self, results=None):
        self.results = list(results or [TelegramSendResult(success=True, attempts=1)])
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return self.results.pop(0) if self.results else TelegramSendResult(success=True, attempts=1)


def request(key="one", event=NotificationEventCode.COLLECTOR_FAILED):
    return NotificationRequest(event_code=event, dedupe_key=key,
                               priority=NotificationPriority.IMPORTANT, message="safe message")


def mock_client(handler, retries=2):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramClient("123456:TEST_SECRET", "987654", max_retries=retries,
                          client=client, sleeper=lambda _: None)


def test_config_disabled_by_default():
    assert get_settings().telegram_enabled is False


@pytest.mark.parametrize(("token", "chat"), [(None, SecretStr("1")), (SecretStr("x"), None)])
def test_enabled_missing_credential_is_degraded(session_factory, token, chat):
    config = configured_settings(telegram_bot_token=token, telegram_chat_id=chat)
    health = NotificationService(NotificationRepository(session_factory), config).health(MONDAY)
    assert health["status"] == TelegramHealthStatus.DEGRADED.value


def test_properly_configured_not_tested(session_factory):
    assert NotificationService(NotificationRepository(session_factory), configured_settings()).health(MONDAY)["status"] == "CONFIGURED_NOT_TESTED"


def test_telegram_success():
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"ok": True})
    result = mock_client(handler).send("hello")
    assert result.success and result.attempts == 1 and len(calls) == 1
    assert "TEST_SECRET" not in str(result)


def test_telegram_timeout_retries_and_sanitizes():
    calls = []
    def handler(req):
        calls.append(req)
        raise httpx.ReadTimeout("123456:TEST_SECRET", request=req)
    result = mock_client(handler).send("hello")
    assert not result.success and result.transient_failure and result.attempts == 3
    assert "TEST_SECRET" not in (result.safe_error_message or "")


def test_telegram_connection_reset_retries():
    count = {"value": 0}
    def handler(req):
        count["value"] += 1
        raise httpx.ConnectError("connection reset", request=req)
    result = mock_client(handler, retries=1).send("hello")
    assert not result.success and result.attempts == 2 and count["value"] == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_status_retries(status):
    count = {"value": 0}
    def handler(req):
        count["value"] += 1
        return httpx.Response(status, json={"ok": False})
    result = mock_client(handler).send("hello")
    assert not result.success and result.attempts == 3 and count["value"] == 3


@pytest.mark.parametrize("status", [400, 401, 403])
def test_permanent_http_status_does_not_retry(status):
    count = {"value": 0}
    def handler(req):
        count["value"] += 1
        return httpx.Response(status, json={"ok": False})
    result = mock_client(handler).send("hello")
    assert not result.success and result.attempts == 1 and count["value"] == 1


def test_delivery_dedupe_and_restart_idempotency(session_factory):
    fake = FakeTelegram()
    repo = NotificationRepository(session_factory)
    first = NotificationService(repo, configured_settings(), fake, throttle_seconds=0)
    second = NotificationService(repo, configured_settings(), fake, throttle_seconds=0)
    assert first.notify(request("same"))
    assert not second.notify(request("same"))
    assert first.notify(request("different"))
    assert len(fake.messages) == 2


def test_delivery_failure_persisted_and_retryable(session_factory):
    failed = FakeTelegram([TelegramSendResult(
        success=False, attempts=3, transient_failure=True,
        safe_error_type="ReadTimeout", safe_error_message="Telegram request timed out",
    )])
    repo = NotificationRepository(session_factory)
    service = NotificationService(repo, configured_settings(), failed, throttle_seconds=0)
    assert not service.notify(request("retry-me"))
    success = NotificationService(repo, configured_settings(), FakeTelegram(), throttle_seconds=0)
    assert success.retry_failed(limit=20) == {"eligible": 1, "sent": 1, "failed": 0}
    assert repo.list()[0]["status"] == "SENT"


def test_notification_failure_is_swallowed(session_factory):
    fake = FakeTelegram([TelegramSendResult(
        success=False, attempts=1, transient_failure=False,
        safe_error_type="HTTPError", safe_error_message="Telegram HTTP 400",
    )])
    service = NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0)
    assert service.notify(request("bad")) is False


def test_one_failure_health_is_degraded(session_factory):
    fake = FakeTelegram([TelegramSendResult(success=False, attempts=1, transient_failure=True,
                                            safe_error_type="Timeout", safe_error_message="safe")])
    service = NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0)
    service.notify(request("health-failure"))
    assert service.health(datetime.now(IST))["status"] == "DEGRADED"


@pytest.mark.parametrize(("directions", "current", "expected"), [
    (["BULLISH", "BULLISH"], "BULLISH", (True, False)),
    (["BULLISH", "BULLISH", "BULLISH"], "BULLISH", (False, False)),
    (["BEARISH", "BEARISH", "BULLISH"], "BEARISH", (True, True)),
    (["NO_TRADE", "NO_TRADE"], "NO_TRADE", (False, False)),
    (["BULLISH", "BEARISH"], "BULLISH", (False, False)),
])
def test_directional_confirmation_rules(directions, current, expected):
    assert should_notify_directional(directions, current, 2) == expected


def test_strategy_message_and_ltp_warning():
    candidate = {"strategy_type":"BULL_PUT_SPREAD", "short_leg":{"strike":22300,"option_type":"PE"},
                 "long_leg":{"strike":22200,"option_type":"PE"}, "spread_width":100,
                 "net_credit":24.5, "pricing_basis":"LTP_ESTIMATE", "selection_score":84, "spot":22420}
    text = format_candidate(candidate)
    assert "CREDIT SPREAD CANDIDATE" in text and "not executable quote" in text


def test_risk_messages_are_explicit_and_routine_rejection_routing_constant():
    approved = {"candidate_strategy":"BULL_PUT_SPREAD", "max_profit_per_lot":1000,
                "max_loss_per_lot":4000, "credit_to_width_ratio":.2,
                "source_candidate":{"short_leg":{"strike":22300,"option_type":"PE"},
                                    "long_leg":{"strike":22200,"option_type":"PE"}}}
    text = format_risk(approved, {"regime":"BULLISH","confidence":75}, approved=True)
    assert "NO REAL ORDER HAS BEEN PLACED" in text and "SHADOW ONLY" in text
    routine = {"candidate_strategy":"BULL_PUT_SPREAD", "failed_checks":["WEAK_PREMIUM"]}
    assert "WEAK_PREMIUM" in format_risk(routine, None, approved=False)
    assert not is_important_risk_rejection(routine)
    assert is_important_risk_rejection({"reason_codes":["MAX_LOSS_EXCEEDED"]})


def trade(status="OPEN"):
    return {"strategy_type":"BULL_PUT_SPREAD", "short_leg":{"strike":22300,"option_type":"PE"},
            "long_leg":{"strike":22200,"option_type":"PE"}, "entry_credit":24.5,
            "entry_pricing_basis":"BID_ASK", "entry_spot":22420, "max_profit_per_lot":1225,
            "max_loss_per_lot":3775, "entry_timestamp":MONDAY.isoformat(), "status":status,
            "exit_reason":"PROFIT_TARGET_EXIT", "exit_debit":12.1, "realized_pnl_per_lot":620,
            "mfe_per_lot":700, "mae_per_lot":-100, "holding_minutes":54}


def test_shadow_messages_include_simulation_wording_and_pnl():
    assert "No broker order placed" in format_shadow_entry(trade())
    exit_text = format_shadow_exit(trade("CLOSED"))
    assert "+₹620.00" in exit_text and "No real trade was executed" in exit_text


def test_operational_formatter_is_plain_and_concise():
    text = format_operational_alert("COLLECTOR", "UNHEALTHY", "Heartbeat stale", MONDAY,
                                    NotificationPriority.CRITICAL)
    assert "SYSTEM ALERT" in text and "Heartbeat stale" in text and len(text) < 4096


def test_regime_formatter_omits_no_required_headline():
    regime = {"regime":"BULLISH","confidence":74,"evidence_quality":"HIGH","timestamp":MONDAY.isoformat()}
    feature = {"spot":22420,"volatility_features":{"india_vix":14.6},
               "support_resistance":{"potential_support_clusters":[],"potential_resistance_clusters":[]}}
    assert "NIFTY REGIME CONFIRMED" in format_regime(regime, feature, changed=False)


class MondayDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return MONDAY if tz else MONDAY.replace(tzinfo=None)


def add_event(session_factory, code, at=MONDAY):
    with session_factory.begin() as session:
        row = OperationalEventRecord(component="COLLECTOR", severity="WARN", event_code=code,
                                     safe_message="Heartbeat stale", metadata_json={}, created_at=at)
        session.add(row)
        session.flush()
        return row.id


def test_operational_transition_alert_dedupe_and_recovery(session_factory, monkeypatch):
    import app.notifications.router as module
    monkeypatch.setattr(module, "datetime", MondayDateTime)
    fake = FakeTelegram()
    service = NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0)
    router = NotificationRouter(session_factory, configured_settings(), service)
    add_event(session_factory, "COLLECTOR_HEARTBEAT_MISSED")
    assert router.route_operational_events() == 1
    assert router.route_operational_events() == 0  # delivery exists; no second send
    add_event(session_factory, "COLLECTOR_RECOVERED", MONDAY + timedelta(minutes=3))
    router.route_operational_events()
    assert len(fake.messages) == 2 and "SYSTEM RECOVERED" in fake.messages[-1]


def test_sunday_suppresses_market_stale_alert(session_factory, monkeypatch):
    import app.notifications.router as module
    sunday = datetime(2026, 10, 4, 10, tzinfo=IST)
    class SundayDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return sunday if tz else sunday.replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", SundayDateTime)
    add_event(session_factory, "COLLECTOR_HEARTBEAT_MISSED", sunday)
    fake = FakeTelegram()
    router = NotificationRouter(session_factory, configured_settings(),
        NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0))
    assert router.route_operational_events() == 0 and not fake.messages


def test_daily_summary_once_and_holiday_skip(session_factory):
    fake = FakeTelegram()
    service = NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0)
    router = NotificationRouter(session_factory, configured_settings(), service)
    assert router.send_daily_summary(date(2026, 10, 5))
    assert not router.send_daily_summary(date(2026, 10, 5))
    assert len(fake.messages) == 1 and "DAILY RESEARCH & OPERATIONS SUMMARY" in fake.messages[0]
    holiday = configured_settings(collector_holidays="2026-10-06")
    assert not NotificationRouter(session_factory, holiday, service).send_daily_summary(date(2026, 10, 6))


def test_health_states_after_success_and_failures(session_factory):
    repo = NotificationRepository(session_factory)
    success = NotificationService(repo, configured_settings(), FakeTelegram(), throttle_seconds=0)
    success.notify(request("sent"))
    now = datetime.now(IST)
    assert success.health(now)["status"] == "HEALTHY"
    for index in range(3):
        failure = FakeTelegram([TelegramSendResult(success=False, attempts=1, transient_failure=True,
                                                   safe_error_type="Timeout", safe_error_message="safe")])
        NotificationService(repo, configured_settings(), failure, throttle_seconds=0).notify(request(f"fail-{index}"))
    assert success.health(now)["status"] == "FAILED"


def test_notifications_api_and_health_contains_telegram(session_factory):
    service = NotificationService(NotificationRepository(session_factory), configured_settings(), FakeTelegram(), throttle_seconds=0)
    service.notify(request("api"))
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        client = TestClient(app)
        listed = client.get("/api/notifications?limit=20")
        assert listed.status_code == 200 and listed.json()[0]["event_code"] == "COLLECTOR_FAILED"
        body = client.get("/api/system/health").json()
        assert "telegram" in body["notifications"]
        assert "bot_token" not in str(body).lower() and "chat_id" not in str(body).lower()
    finally:
        app.dependency_overrides.clear()


def test_pipeline_persists_when_notification_router_fails(repository, session_factory, market_snapshot):
    snapshot_id = save(repository, market_snapshot).snapshot_id
    class BrokenRouter:
        def notify_research(self, _): raise RuntimeError("telegram unavailable")
        def route_operational_events(self): raise AssertionError("not reached")
    callback = make_after_snapshot_callback(session_factory, get_settings(), BrokenRouter())
    result = callback(snapshot_id)
    assert result.status.value == "SUCCESS"


def test_telegram_failure_does_not_roll_back_shadow(repository, session_factory, market_snapshot):
    snapshot_id, risk_repository = persisted_context(repository, session_factory, market_snapshot)
    build_and_store_risk(risk_repository, snapshot_id, approved_config(), EvaluationContext.HISTORICAL)
    entry = build_shadow_entry(ShadowRepository(session_factory), snapshot_id,
                               shadow_config(get_settings()))
    assert entry.created and entry.trade is not None
    fake = FakeTelegram([TelegramSendResult(success=False, attempts=1,
                                            safe_error_type="HTTPError",
                                            safe_error_message="Telegram HTTP 400")])
    NotificationService(NotificationRepository(session_factory), configured_settings(),
                        fake, throttle_seconds=0).notify(request("shadow-failure"))
    assert ShadowRepository(session_factory).get_trade(entry.trade.id) is not None


def test_daily_formatter_combines_research_and_operations():
    research = {"date":"2026-10-05","snapshots_collected":1,"snapshots_with_usable_oi":1,
                "regime_counts":{},"candidate_sets_created":0,"approved_risk_decisions":0,
                "shadow_entries":0,"shadow_exits":0,"shadow_realized_pnl":0,"ai_calls":0,"ai_cost":0}
    ops = {"collector":{"success":1,"failure":0},"pipeline":{"success":1,"partial":0,"failure":0},
           "operational_events":{"WARN":0,"ERROR":0,"CRITICAL":0}}
    text = format_daily_summary(research, ops)
    assert "DAILY RESEARCH & OPERATIONS SUMMARY" in text and "Collector success/failure" in text


def test_notification_router_with_no_research_records_sends_nothing(session_factory):
    fake = FakeTelegram()
    router = NotificationRouter(session_factory, configured_settings(),
        NotificationService(NotificationRepository(session_factory), configured_settings(), fake, throttle_seconds=0))
    assert router.notify_research(999999) == 0 and not fake.messages


def test_manual_telegram_script_disabled(monkeypatch, capsys):
    import scripts.test_telegram as script
    monkeypatch.setattr(script, "get_settings", lambda: get_settings().model_copy(update={"telegram_enabled": False}))
    assert script.main() == 0
    assert capsys.readouterr().out.strip() == "TELEGRAM_DISABLED"


def test_manual_telegram_script_mocked_success(session_factory, monkeypatch, capsys):
    import scripts.test_telegram as script
    url = str(session_factory.kw["bind"].url)
    config = configured_settings(database_url=SecretStr(url))
    monkeypatch.setattr(script, "get_settings", lambda: config)
    monkeypatch.setattr(script.NotificationService, "notify", lambda self, value: True)
    assert script.main() == 0
    assert capsys.readouterr().out.strip() == "TELEGRAM_TEST_SENT"


def test_retry_script_invokes_failed_only(monkeypatch, capsys):
    import scripts.retry_failed_notifications as script
    config = configured_settings(database_url=SecretStr("sqlite+pysqlite:///:memory:"))
    monkeypatch.setattr(script, "get_settings", lambda: config)
    monkeypatch.setattr(script.NotificationService, "retry_failed",
                        lambda self, **kwargs: {"eligible": 1, "sent": 1, "failed": 0})
    monkeypatch.setattr(sys, "argv", ["retry_failed_notifications.py", "--today", "--limit", "20"])
    assert script.main() == 0
    output = capsys.readouterr().out
    assert "eligible=1" in output and "sent=1" in output
