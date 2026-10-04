from app.notifications.models import NotificationEventCode

OPERATIONAL = {
    NotificationEventCode.COLLECTOR_STARTED, NotificationEventCode.COLLECTOR_STALE,
    NotificationEventCode.COLLECTOR_RECOVERED, NotificationEventCode.COLLECTOR_FAILED,
    NotificationEventCode.DATA_FRESHNESS_DEGRADED,
    NotificationEventCode.DATA_FRESHNESS_UNHEALTHY,
    NotificationEventCode.DATA_FRESHNESS_RECOVERED,
    NotificationEventCode.DATABASE_UNHEALTHY, NotificationEventCode.DATABASE_RECOVERED,
    NotificationEventCode.PIPELINE_PARTIAL, NotificationEventCode.PIPELINE_FAILED,
    NotificationEventCode.PIPELINE_RECOVERED,
    NotificationEventCode.MARKET_DATA_DEGRADED,
    NotificationEventCode.MARKET_DATA_RECOVERED,
}


def enabled_for(settings, event_code: NotificationEventCode) -> bool:
    if event_code in OPERATIONAL:
        return settings.telegram_notify_operational
    if event_code in {NotificationEventCode.DIRECTIONAL_REGIME_CONFIRMED,
                      NotificationEventCode.REGIME_CHANGED}:
        return settings.telegram_notify_regime
    if event_code == NotificationEventCode.STRATEGY_CANDIDATE_CREATED:
        return settings.telegram_notify_strategy
    if event_code in {NotificationEventCode.RISK_APPROVED,
                      NotificationEventCode.RISK_REJECTED_IMPORTANT}:
        return settings.telegram_notify_risk
    if event_code in {NotificationEventCode.SHADOW_ENTRY_CREATED,
                      NotificationEventCode.SHADOW_EXITED}:
        return settings.telegram_notify_shadow
    if event_code in {NotificationEventCode.DAILY_RESEARCH_SUMMARY,
                      NotificationEventCode.DAILY_OPERATIONS_SUMMARY}:
        return settings.telegram_notify_daily_summary
    return True
