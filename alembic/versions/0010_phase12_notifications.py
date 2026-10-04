"""Add Phase 12 notification delivery storage.

Revision ID: 0010_phase12
Revises: 0009_phase11
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010_phase12"
down_revision: str | None = "0009_phase11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("event_code", sa.String(120), nullable=False),
        sa.Column("dedupe_key", sa.String(300), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("subject_ref_type", sa.String(64), nullable=True),
        sa.Column("subject_ref_id", sa.String(240), nullable=True),
        sa.Column("message_hash", sa.String(64), nullable=False),
        sa.Column("rendered_message", sa.String(4096), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transient_failure", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_type", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','SENT','FAILED','SKIPPED')", name="ck_notification_delivery_status"),
        sa.CheckConstraint("priority IN ('INFO','IMPORTANT','CRITICAL')", name="ck_notification_priority"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "dedupe_key", name="uq_notification_channel_dedupe"),
    )
    for name, column in (("created_at", "created_at"), ("status", "status"),
                         ("event_code", "event_code"), ("dedupe_key", "dedupe_key")):
        op.create_index(f"ix_notification_deliveries_{name}", "notification_deliveries", [column])


def downgrade() -> None:
    for name in ("dedupe_key", "event_code", "status", "created_at"):
        op.drop_index(f"ix_notification_deliveries_{name}", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
