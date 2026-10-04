"""Outbound-only, failure-isolated notifications."""

from app.notifications.models import NotificationEventCode, NotificationPriority

__all__ = ["NotificationEventCode", "NotificationPriority"]
