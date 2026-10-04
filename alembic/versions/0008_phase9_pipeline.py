"""Create Phase 9 pipeline run storage.

Revision ID: 0008_phase9
Revises: 0007_phase8
"""
from typing import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0008_phase9"
down_revision: str | None = "0007_phase8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("feature_status", sa.String(16), nullable=False),
        sa.Column("regime_status", sa.String(16), nullable=False),
        sa.Column("ai_status", sa.String(16), nullable=False),
        sa.Column("strategy_status", sa.String(16), nullable=False),
        sa.Column("risk_status", sa.String(16), nullable=False),
        sa.Column("shadow_status", sa.String(16), nullable=False),
        sa.Column("feature_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("regime_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("ai_research_id", sa.Integer(), nullable=True),
        sa.Column("strategy_candidate_set_id", sa.Integer(), nullable=True),
        sa.Column("approved_candidate_count", sa.Integer(), nullable=False),
        sa.Column("shadow_trade_id", sa.Integer(), nullable=True),
        sa.Column("safe_error_type", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('STARTED','SUCCESS','PARTIAL','FAILED','SKIPPED')", name="ck_pipeline_status"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "pipeline_version", name="uq_pipeline_run_version"),
    )
    op.create_index("ix_pipeline_run_created_at", "pipeline_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_run_created_at", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
