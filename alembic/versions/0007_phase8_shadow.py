"""Create Phase 8 shadow lifecycle storage.

Revision ID: 0007_phase8
Revises: 0006_phase7
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_phase8"
down_revision: str | None = "0006_phase7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "shadow_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id_entry", sa.Integer(), nullable=False),
        sa.Column("risk_decision_id", sa.Integer(), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(240), nullable=False),
        sa.Column("shadow_version", sa.String(32), nullable=False),
        sa.Column("strategy_type", sa.String(24), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("entry_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_spot", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_credit", sa.Numeric(20, 6), nullable=False),
        sa.Column("entry_pricing_basis", sa.String(20), nullable=False),
        sa.Column("spread_width", sa.Numeric(20, 6), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("max_profit_per_unit", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_loss_per_unit", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_profit_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("max_loss_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("exit_market_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("exit_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(64), nullable=True),
        sa.Column("realized_pnl_per_unit", sa.Numeric(20, 6), nullable=True),
        sa.Column("realized_pnl_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("mae_per_unit", sa.Numeric(20, 6), nullable=False),
        sa.Column("mfe_per_unit", sa.Numeric(20, 6), nullable=False),
        sa.Column("mae_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("mfe_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("holding_minutes", sa.Numeric(20, 4), nullable=True),
        sa.Column("result_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED', 'INVALID')", name="ck_shadow_trade_status"),
        sa.ForeignKeyConstraint(["market_snapshot_id_entry"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["risk_decision_id"], ["risk_decisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["exit_market_snapshot_id"], ["market_snapshots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_fingerprint", "shadow_version", name="uq_shadow_trade_fingerprint_version"),
    )
    op.create_index("ix_shadow_trade_entry_snapshot", "shadow_trades", ["market_snapshot_id_entry"])
    op.create_index("ix_shadow_trade_status", "shadow_trades", ["status"])
    op.create_index("ix_shadow_trade_entry_timestamp", "shadow_trades", ["entry_timestamp"])
    op.create_table(
        "shadow_trade_marks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("shadow_trade_id", sa.Integer(), nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("short_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("long_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("valuation_basis", sa.String(20), nullable=False),
        sa.Column("exit_debit", sa.Numeric(20, 6), nullable=False),
        sa.Column("pnl_per_unit", sa.Numeric(20, 6), nullable=False),
        sa.Column("pnl_per_lot", sa.Numeric(20, 6), nullable=True),
        sa.Column("spot", sa.Numeric(20, 6), nullable=False),
        sa.Column("result_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["shadow_trade_id"], ["shadow_trades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_trade_id", "market_snapshot_id", name="uq_shadow_mark_snapshot"),
    )
    op.create_index("ix_shadow_mark_trade_id", "shadow_trade_marks", ["shadow_trade_id"])
    op.create_index("ix_shadow_mark_timestamp", "shadow_trade_marks", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_shadow_mark_timestamp", table_name="shadow_trade_marks")
    op.drop_index("ix_shadow_mark_trade_id", table_name="shadow_trade_marks")
    op.drop_table("shadow_trade_marks")
    op.drop_index("ix_shadow_trade_entry_timestamp", table_name="shadow_trades")
    op.drop_index("ix_shadow_trade_status", table_name="shadow_trades")
    op.drop_index("ix_shadow_trade_entry_snapshot", table_name="shadow_trades")
    op.drop_table("shadow_trades")
