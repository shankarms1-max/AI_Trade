"""Add Phase 11 observability storage.

Revision ID: 0009_phase11
Revises: 0008_phase9
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_phase11"
down_revision: str | None = "0008_phase9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("stage_timings_ms", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                  server_default=sa.text("'{}'"), nullable=False),
    )
    op.create_table(
        "collector_heartbeats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("worker_id", sa.String(120), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("safe_error_type", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["last_snapshot_id"], ["market_snapshots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id"),
    )
    op.create_index("ix_collector_heartbeat_last_at", "collector_heartbeats", ["last_heartbeat_at"])
    op.create_table(
        "operational_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("event_code", sa.String(120), nullable=False),
        sa.Column("error_type", sa.String(120), nullable=True),
        sa.Column("safe_message", sa.String(500), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=True),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=True),
        sa.Column("context", sa.String(120), nullable=True),
        sa.Column("metadata_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("severity IN ('INFO','WARN','ERROR','CRITICAL')", name="ck_operational_event_severity"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["market_snapshots.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, column in (("created_at", "created_at"), ("component", "component"),
                         ("severity", "severity"), ("code", "event_code")):
        op.create_index(f"ix_operational_events_{name}", "operational_events", [column])


def downgrade() -> None:
    for name in ("code", "severity", "component", "created_at"):
        op.drop_index(f"ix_operational_events_{name}", table_name="operational_events")
    op.drop_table("operational_events")
    op.drop_index("ix_collector_heartbeat_last_at", table_name="collector_heartbeats")
    op.drop_table("collector_heartbeats")
    op.drop_column("pipeline_runs", "stage_timings_ms")
