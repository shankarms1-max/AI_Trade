"""Create Phase 6 strategy candidate storage.

Revision ID: 0005_phase6
Revises: 0004_phase5
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_phase6"
down_revision: str | None = "0004_phase5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_candidate_sets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("regime_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("regime", sa.String(16), nullable=False),
        sa.Column("strategy_type", sa.String(24), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("candidate_count >= 0", name="ck_strategy_candidate_count"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regime_snapshot_id"], ["market_regime_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "strategy_version", name="uq_strategy_candidate_set_version"),
    )
    op.create_index("ix_strategy_candidate_regime_snapshot_id", "strategy_candidate_sets", ["regime_snapshot_id"])
    op.create_index("ix_strategy_candidate_created_at", "strategy_candidate_sets", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_strategy_candidate_created_at", table_name="strategy_candidate_sets")
    op.drop_index("ix_strategy_candidate_regime_snapshot_id", table_name="strategy_candidate_sets")
    op.drop_table("strategy_candidate_sets")
