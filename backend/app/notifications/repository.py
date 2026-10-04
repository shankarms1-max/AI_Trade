from datetime import date, datetime, time, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import NotificationDeliveryRecord
from app.notifications.models import DeliveryStatus, NotificationRequest, TelegramSendResult

IST = ZoneInfo("Asia/Kolkata")


class NotificationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def reserve(self, request: NotificationRequest) -> tuple[dict[str, Any], bool]:
        digest = sha256(request.message.encode("utf-8")).hexdigest()
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(select(NotificationDeliveryRecord).where(
                    NotificationDeliveryRecord.channel == "TELEGRAM",
                    NotificationDeliveryRecord.dedupe_key == request.dedupe_key,
                ))
                if existing is not None:
                    return self._row(existing), False
                row = NotificationDeliveryRecord(
                    channel="TELEGRAM", event_code=request.event_code.value,
                    dedupe_key=request.dedupe_key, status=DeliveryStatus.PENDING.value,
                    priority=request.priority.value,
                    subject_ref_type=request.subject_ref_type,
                    subject_ref_id=request.subject_ref_id,
                    message_hash=digest, rendered_message=request.message,
                    attempt_count=0, transient_failure=False,
                )
                session.add(row)
                session.flush()
                return self._row(row), True
        except IntegrityError:
            with self._sessions() as session:
                row = session.scalar(select(NotificationDeliveryRecord).where(
                    NotificationDeliveryRecord.channel == "TELEGRAM",
                    NotificationDeliveryRecord.dedupe_key == request.dedupe_key,
                ))
                if row is None:
                    raise
                return self._row(row), False

    def finish(self, delivery_id: int, result: TelegramSendResult) -> None:
        now = datetime.now(IST)
        with self._sessions.begin() as session:
            row = session.get(NotificationDeliveryRecord, delivery_id)
            if row is None:
                raise LookupError(f"notification delivery {delivery_id} not found")
            row.attempt_count += result.attempts
            row.last_attempt_at = now
            row.transient_failure = result.transient_failure
            if result.success:
                row.status = DeliveryStatus.SENT.value
                row.sent_at = now
                row.safe_error_type = None
                row.safe_error_message = None
            else:
                row.status = DeliveryStatus.FAILED.value
                row.safe_error_type = result.safe_error_type
                row.safe_error_message = result.safe_error_message

    def skip(self, delivery_id: int, reason: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(NotificationDeliveryRecord, delivery_id)
            if row is not None:
                row.status = DeliveryStatus.SKIPPED.value
                row.safe_error_type = "ConfigurationError"
                row.safe_error_message = reason[:500]

    def retryable_failed(self, limit: int = 20, *, today: bool = False) -> list[dict[str, Any]]:
        query = select(NotificationDeliveryRecord).where(
            NotificationDeliveryRecord.status == DeliveryStatus.FAILED.value,
            NotificationDeliveryRecord.transient_failure.is_(True),
        )
        if today:
            now = datetime.now(IST)
            start = datetime.combine(now.date(), time.min, IST)
            query = query.where(NotificationDeliveryRecord.created_at >= start)
        with self._sessions() as session:
            rows = session.scalars(query.order_by(
                NotificationDeliveryRecord.created_at.asc()
            ).limit(limit)).all()
            return [self._row(row) for row in rows]

    def list(
        self, limit: int = 100, *, status: str | None = None,
        event_code: str | None = None, day: date | None = None,
    ) -> list[dict[str, Any]]:
        query = select(NotificationDeliveryRecord)
        if status:
            query = query.where(NotificationDeliveryRecord.status == status.upper())
        if event_code:
            query = query.where(NotificationDeliveryRecord.event_code == event_code.upper())
        if day:
            start = datetime.combine(day, time.min, IST)
            query = query.where(NotificationDeliveryRecord.created_at >= start,
                                NotificationDeliveryRecord.created_at < start + timedelta(days=1))
        with self._sessions() as session:
            rows = session.scalars(query.order_by(
                NotificationDeliveryRecord.created_at.desc(),
                NotificationDeliveryRecord.id.desc(),
            ).limit(limit)).all()
            return [self._public(row) for row in rows]

    def health_metrics(self, day: date) -> dict[str, Any]:
        rows = self.list(1000, day=day)
        sent = [row for row in rows if row["status"] == "SENT"]
        failed = [row for row in rows if row["status"] == "FAILED"]
        consecutive = 0
        for row in rows:
            if row["status"] == "SENT":
                break
            if row["status"] == "FAILED":
                consecutive += 1
        return {
            "last_sent_at": None if not sent else sent[0]["sent_at"],
            "last_failed_at": None if not failed else failed[0]["last_attempt_at"],
            "failed_today": len(failed), "consecutive_failures": consecutive,
            "deliveries_today": len(rows),
        }

    def get(self, delivery_id: int) -> dict[str, Any] | None:
        with self._sessions() as session:
            row = session.get(NotificationDeliveryRecord, delivery_id)
            return None if row is None else self._row(row)

    @staticmethod
    def _row(row: NotificationDeliveryRecord) -> dict[str, Any]:
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    @staticmethod
    def _public(row: NotificationDeliveryRecord) -> dict[str, Any]:
        return {key: getattr(row, key) for key in (
            "id", "channel", "event_code", "status", "priority",
            "subject_ref_type", "subject_ref_id", "attempt_count",
            "last_attempt_at", "sent_at", "safe_error_type", "safe_error_message",
            "created_at", "updated_at",
        )}
