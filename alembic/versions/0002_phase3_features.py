"""Create Phase 3 derived feature storage.

Revision ID: 0002_phase3
Revises: 0001_phase2
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase3"
down_revision: str | None = "0001_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_feature_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("feature_version", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("local_pcr_oi", sa.Numeric(24, 10), nullable=True),
        sa.Column("local_pcr_oi_change", sa.Numeric(24, 10), nullable=True),
        sa.Column("futures_basis", sa.Numeric(20, 6), nullable=True),
        sa.Column("india_vix", sa.Numeric(20, 6), nullable=True),
        sa.Column("vix_regime", sa.String(16), nullable=True),
        sa.Column("nearest_support", sa.Numeric(20, 6), nullable=True),
        sa.Column("nearest_resistance", sa.Numeric(20, 6), nullable=True),
        sa.Column(
            "feature_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "feature_version", name="uq_feature_snapshot_version"),
    )
    op.create_index("ix_feature_snapshots_timestamp", "market_feature_snapshots", ["timestamp"])
    op.create_index("ix_feature_snapshots_expiry", "market_feature_snapshots", ["expiry"])
    op.create_index("ix_feature_snapshots_market_snapshot_id", "market_feature_snapshots", ["market_snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_feature_snapshots_market_snapshot_id", table_name="market_feature_snapshots")
    op.drop_index("ix_feature_snapshots_expiry", table_name="market_feature_snapshots")
    op.drop_index("ix_feature_snapshots_timestamp", table_name="market_feature_snapshots")
    op.drop_table("market_feature_snapshots")
