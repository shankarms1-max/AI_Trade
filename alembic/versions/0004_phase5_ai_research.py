"""Create Phase 5 AI research storage.

Revision ID: 0004_phase5
Revises: 0003_phase4
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_phase5"
down_revision: str | None = "0003_phase4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_research_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("feature_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("regime_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("ai_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("market_view", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Numeric(7, 2), nullable=True),
        sa.Column("original_ai_confidence", sa.Numeric(7, 2), nullable=True),
        sa.Column("confidence_capped", sa.Boolean(), nullable=False),
        sa.Column("agreement_status", sa.String(20), nullable=True),
        sa.Column("result_json", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(16, 8), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_type", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('STARTED', 'SUCCESS', 'FAILED')", name="ck_ai_research_status"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["feature_snapshot_id"], ["market_feature_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regime_snapshot_id"], ["market_regime_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "ai_version", name="uq_ai_research_version"),
    )
    op.create_index("ix_ai_research_feature_snapshot_id", "ai_research_snapshots", ["feature_snapshot_id"])
    op.create_index("ix_ai_research_regime_snapshot_id", "ai_research_snapshots", ["regime_snapshot_id"])
    op.create_index("ix_ai_research_created_at", "ai_research_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_research_created_at", table_name="ai_research_snapshots")
    op.drop_index("ix_ai_research_regime_snapshot_id", table_name="ai_research_snapshots")
    op.drop_index("ix_ai_research_feature_snapshot_id", table_name="ai_research_snapshots")
    op.drop_table("ai_research_snapshots")
