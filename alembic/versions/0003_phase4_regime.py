"""Create Phase 4 market regime results.

Revision ID: 0003_phase4
Revises: 0002_phase3
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_phase4"
down_revision: str | None = "0002_phase3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_regime_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("feature_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("regime_version", sa.String(32), nullable=False),
        sa.Column("regime", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(7, 2), nullable=False),
        sa.Column("evidence_quality", sa.String(16), nullable=False),
        sa.Column("bull_score", sa.Numeric(12, 4), nullable=False),
        sa.Column("bear_score", sa.Numeric(12, 4), nullable=False),
        sa.Column("range_score", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "result_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("regime IN ('BULLISH', 'BEARISH', 'RANGE', 'NO_TRADE')", name="ck_market_regime"),
        sa.CheckConstraint(
            "evidence_quality IN ('HIGH', 'MEDIUM', 'LOW', 'INSUFFICIENT')",
            name="ck_regime_evidence_quality",
        ),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["feature_snapshot_id"], ["market_feature_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "regime_version", name="uq_regime_snapshot_version"),
    )
    op.create_index("ix_regime_feature_snapshot_id", "market_regime_snapshots", ["feature_snapshot_id"])
    op.create_index("ix_regime_market_snapshot_id", "market_regime_snapshots", ["market_snapshot_id"])
    op.create_index("ix_regime_created_at", "market_regime_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_regime_created_at", table_name="market_regime_snapshots")
    op.drop_index("ix_regime_market_snapshot_id", table_name="market_regime_snapshots")
    op.drop_index("ix_regime_feature_snapshot_id", table_name="market_regime_snapshots")
    op.drop_table("market_regime_snapshots")
