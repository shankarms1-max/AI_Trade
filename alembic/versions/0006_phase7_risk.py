"""Create Phase 7 risk decisions and preserve broker lot size.

Revision ID: 0006_phase7
Revises: 0005_phase6
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_phase7"
down_revision: str | None = "0005_phase6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("market_snapshots", sa.Column("lot_size", sa.Integer(), nullable=True))
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("regime_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("strategy_candidate_set_id", sa.Integer(), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("risk_version", sa.String(32), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(240), nullable=True),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("strategy_type", sa.String(24), nullable=False),
        sa.Column("max_profit_per_unit", sa.Numeric(20, 6), nullable=True),
        sa.Column("max_loss_per_unit", sa.Numeric(20, 6), nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("max_profit_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("max_loss_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("failed_check_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("decision IN ('APPROVED', 'REJECTED', 'NOT_APPLICABLE')", name="ck_risk_decision"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["regime_snapshot_id"], ["market_regime_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_candidate_set_id"], ["strategy_candidate_sets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_snapshot_id", "strategy_version", "risk_version", "candidate_fingerprint", name="uq_risk_decision_candidate_version"),
    )
    op.create_index("ix_risk_decision_candidate_set_id", "risk_decisions", ["strategy_candidate_set_id"])
    op.create_index("ix_risk_decision_created_at", "risk_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_risk_decision_created_at", table_name="risk_decisions")
    op.drop_index("ix_risk_decision_candidate_set_id", table_name="risk_decisions")
    op.drop_table("risk_decisions")
    op.drop_column("market_snapshots", "lot_size")
