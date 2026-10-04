"""Add Phase 14 statistical alpha feature storage.

Revision ID: 0011_phase14
Revises: 0010_phase12
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_phase14"
down_revision: str | None = "0010_phase12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "alpha_feature_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("alpha_version", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("price_source", sa.String(16), nullable=False),
        sa.Column("price_return", sa.Numeric(24, 10), nullable=True),
        sa.Column("alpha_1", sa.Numeric(24, 10), nullable=True),
        sa.Column("atm_strike", sa.Numeric(20, 6), nullable=True),
        sa.Column("atm_ce_token", sa.String(160), nullable=True),
        sa.Column("atm_pe_token", sa.String(160), nullable=True),
        sa.Column("atm_ce_interval_volume", sa.BigInteger(), nullable=True),
        sa.Column("atm_pe_interval_volume", sa.BigInteger(), nullable=True),
        sa.Column("ce_volume_ratio", sa.Numeric(24, 10), nullable=True),
        sa.Column("pe_volume_ratio", sa.Numeric(24, 10), nullable=True),
        sa.Column("atm_volume_activity", sa.Numeric(24, 10), nullable=True),
        sa.Column("ce_observed_volatility", sa.Numeric(24, 10), nullable=True),
        sa.Column("pe_observed_volatility", sa.Numeric(24, 10), nullable=True),
        sa.Column("atm_option_volatility", sa.Numeric(24, 10), nullable=True),
        sa.Column("directional_impulse_raw", sa.Numeric(24, 10), nullable=True),
        sa.Column("alpha_2", sa.Numeric(24, 10), nullable=True),
        sa.Column("alpha_1_direction", sa.String(24), nullable=False),
        sa.Column("alpha_2_direction", sa.String(24), nullable=False),
        sa.Column("joint_alpha_direction", sa.String(40), nullable=False),
        sa.Column("consecutive_confirmation_count", sa.Integer(), nullable=False),
        sa.Column("evidence_quality", sa.String(16), nullable=False),
        sa.Column("warnings_json", json_type, nullable=False),
        sa.Column("result_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("price_source IN ('SPOT','FUTURE')", name="ck_alpha_price_source"),
        sa.CheckConstraint("evidence_quality IN ('HIGH','MEDIUM','LOW','INSUFFICIENT')", name="ck_alpha_evidence_quality"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "alpha_version", name="uq_alpha_snapshot_version"),
    )
    op.create_index("ix_alpha_feature_timestamp", "alpha_feature_snapshots", ["timestamp"])
    op.create_index("ix_alpha_feature_expiry", "alpha_feature_snapshots", ["expiry"])
    op.create_index("ix_alpha_feature_market_snapshot", "alpha_feature_snapshots", ["market_snapshot_id"])
    op.add_column(
        "pipeline_runs",
        sa.Column("alpha_status", sa.String(16), server_default="SKIPPED", nullable=False),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("alpha_feature_snapshot_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "alpha_feature_snapshot_id")
    op.drop_column("pipeline_runs", "alpha_status")
    op.drop_index("ix_alpha_feature_market_snapshot", table_name="alpha_feature_snapshots")
    op.drop_index("ix_alpha_feature_expiry", table_name="alpha_feature_snapshots")
    op.drop_index("ix_alpha_feature_timestamp", table_name="alpha_feature_snapshots")
    op.drop_table("alpha_feature_snapshots")
