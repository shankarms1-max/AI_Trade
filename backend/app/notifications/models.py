from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class NotificationEventCode(str, Enum):
    COLLECTOR_STARTED = "COLLECTOR_STARTED"
    COLLECTOR_STALE = "COLLECTOR_STALE"
    COLLECTOR_RECOVERED = "COLLECTOR_RECOVERED"
    COLLECTOR_FAILED = "COLLECTOR_FAILED"
    DATA_FRESHNESS_DEGRADED = "DATA_FRESHNESS_DEGRADED"
    DATA_FRESHNESS_UNHEALTHY = "DATA_FRESHNESS_UNHEALTHY"
    DATA_FRESHNESS_RECOVERED = "DATA_FRESHNESS_RECOVERED"
    DATABASE_UNHEALTHY = "DATABASE_UNHEALTHY"
    DATABASE_RECOVERED = "DATABASE_RECOVERED"
    PIPELINE_PARTIAL = "PIPELINE_PARTIAL"
    PIPELINE_FAILED = "PIPELINE_FAILED"
    PIPELINE_RECOVERED = "PIPELINE_RECOVERED"
    MARKET_DATA_DEGRADED = "MARKET_DATA_DEGRADED"
    MARKET_DATA_RECOVERED = "MARKET_DATA_RECOVERED"
    DIRECTIONAL_REGIME_CONFIRMED = "DIRECTIONAL_REGIME_CONFIRMED"
    REGIME_CHANGED = "REGIME_CHANGED"
    STRATEGY_CANDIDATE_CREATED = "STRATEGY_CANDIDATE_CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED_IMPORTANT = "RISK_REJECTED_IMPORTANT"
    SHADOW_ENTRY_CREATED = "SHADOW_ENTRY_CREATED"
    SHADOW_EXITED = "SHADOW_EXITED"
    DAILY_RESEARCH_SUMMARY = "DAILY_RESEARCH_SUMMARY"
    DAILY_OPERATIONS_SUMMARY = "DAILY_OPERATIONS_SUMMARY"
    AI_RESEARCH_COMPLETED = "AI_RESEARCH_COMPLETED"
    TELEGRAM_TEST = "TELEGRAM_TEST"
    SERVICE_STARTED = "SERVICE_STARTED"


class NotificationPriority(str, Enum):
    INFO = "INFO"
    IMPORTANT = "IMPORTANT"
    CRITICAL = "CRITICAL"


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class TelegramHealthStatus(str, Enum):
    DISABLED = "DISABLED"
    CONFIGURED_NOT_TESTED = "CONFIGURED_NOT_TESTED"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class TelegramSendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    attempts: int = Field(ge=1)
    transient_failure: bool = False
    safe_error_type: str | None = None
    safe_error_message: str | None = None


class NotificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_code: NotificationEventCode
    dedupe_key: str = Field(min_length=1, max_length=300)
    priority: NotificationPriority
    message: str = Field(min_length=1, max_length=4096)
    subject_ref_type: str | None = None
    subject_ref_id: str | None = None
