"""Create Phase 2 raw market-data tables.

Revision ID: 0001_phase2
Revises:
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_phase2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp_ist", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collection_bucket_ist", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nifty_spot", sa.Numeric(20, 6), nullable=False),
        sa.Column("nifty_future", sa.Numeric(20, 6), nullable=True),
        sa.Column("india_vix", sa.Numeric(20, 6), nullable=True),
        sa.Column("atm_strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collection_bucket_ist"),
    )
    op.create_index("ix_market_snapshots_timestamp_ist", "market_snapshots", ["timestamp_ist"])
    op.create_index("ix_market_snapshots_expiry", "market_snapshots", ["expiry"])

    op.create_table(
        "option_contract_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("trading_symbol", sa.String(160), nullable=False),
        sa.Column("instrument_token", sa.String(160), nullable=True),
        sa.Column("ltp", sa.Numeric(20, 6), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("previous_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("change_in_open_interest", sa.BigInteger(), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("implied_volatility", sa.Numeric(20, 6), nullable=True),
        sa.Column("bid", sa.Numeric(20, 6), nullable=True),
        sa.Column("ask", sa.Numeric(20, 6), nullable=True),
        sa.Column("delta", sa.Numeric(24, 10), nullable=True),
        sa.Column("gamma", sa.Numeric(24, 10), nullable=True),
        sa.Column("theta", sa.Numeric(24, 10), nullable=True),
        sa.Column("vega", sa.Numeric(24, 10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("option_type IN ('CE', 'PE')", name="ck_option_type"),
        sa.ForeignKeyConstraint(["market_snapshot_id"], ["market_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market_snapshot_id", "expiry", "strike", "option_type",
            name="uq_option_contract_per_snapshot",
        ),
    )
    op.create_index("ix_option_contract_snapshot_id", "option_contract_snapshots", ["market_snapshot_id"])
    op.create_index("ix_option_contract_strike", "option_contract_snapshots", ["strike"])
    op.create_index("ix_option_contract_option_type", "option_contract_snapshots", ["option_type"])
    op.create_index("ix_option_contract_expiry", "option_contract_snapshots", ["expiry"])

    op.create_table(
        "collector_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("contracts_received", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(120), nullable=True),
        sa.Column("safe_error_message", sa.String(500), nullable=True),
        sa.CheckConstraint("status IN ('STARTED', 'SUCCESS', 'FAILED')", name="ck_collector_status"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["market_snapshots.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_collector_runs_started_at", "collector_runs", ["started_at"])
    op.create_index("ix_collector_runs_snapshot_id", "collector_runs", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_collector_runs_snapshot_id", table_name="collector_runs")
    op.drop_index("ix_collector_runs_started_at", table_name="collector_runs")
    op.drop_table("collector_runs")
    op.drop_index("ix_option_contract_expiry", table_name="option_contract_snapshots")
    op.drop_index("ix_option_contract_option_type", table_name="option_contract_snapshots")
    op.drop_index("ix_option_contract_strike", table_name="option_contract_snapshots")
    op.drop_index("ix_option_contract_snapshot_id", table_name="option_contract_snapshots")
    op.drop_table("option_contract_snapshots")
    op.drop_index("ix_market_snapshots_expiry", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_timestamp_ist", table_name="market_snapshots")
    op.drop_table("market_snapshots")
