"""Additive Phase 14.1 event-time, continuity, and research audit fields.

Revision ID: 0012_phase14_1
Revises: 0011_phase14
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_phase14_1"
down_revision: str | None = "0011_phase14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, kind in (
        ("future_instrument_id", sa.String(160)), ("future_expiry", sa.Date()),
        ("source_market_timestamp", sa.DateTime(timezone=True)),
        ("request_started_at", sa.DateTime(timezone=True)),
        ("response_received_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("market_snapshots", sa.Column(name, kind, nullable=True))
    op.add_column("option_contract_snapshots", sa.Column(
        "exchange", sa.String(16), server_default="nse_fo", nullable=False))
    for name, kind in (
        ("source_market_timestamp", sa.DateTime(timezone=True)),
        ("bid_quantity", sa.BigInteger()), ("ask_quantity", sa.BigInteger()),
    ):
        op.add_column("option_contract_snapshots", sa.Column(name, kind, nullable=True))
    op.add_column("alpha_feature_snapshots", sa.Column(
        "calculation_mode", sa.String(24), server_default="HISTORICAL_REPLAY", nullable=False))
    for name, kind in (
        ("session_id", sa.String(32)), ("session_date", sa.Date()),
        ("lookback_clock_mode", sa.String(20)), ("actual_horizon_seconds", sa.Integer()),
        ("signed_log_return", sa.Numeric(24, 10)), ("hypothesis_type", sa.String(16)),
        ("validity_state", sa.String(16)), ("participation_state", sa.String(16)),
        ("underlying_horizon_volatility", sa.Numeric(24, 10)),
        ("confirmation_reset_reason", sa.String(80)),
        ("source_market_timestamp", sa.DateTime(timezone=True)),
        ("response_received_at", sa.DateTime(timezone=True)),
        ("feature_calculated_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("alpha_feature_snapshots", sa.Column(name, kind, nullable=True))
    with op.batch_alter_table("alpha_feature_snapshots") as batch:
        batch.drop_constraint("uq_alpha_snapshot_version", type_="unique")
        batch.create_unique_constraint("uq_alpha_snapshot_version_mode",
                                       ["market_snapshot_id", "alpha_version", "calculation_mode"])
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "research_experiment_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("parameters_json", json_type, nullable=False),
        sa.Column("train_period", json_type, nullable=True),
        sa.Column("validation_period", json_type, nullable=True),
        sa.Column("test_period", json_type, nullable=True),
        sa.Column("split_name", sa.String(20), nullable=False),
        sa.Column("result_summary", json_type, nullable=False),
        sa.Column("sample_tier", sa.String(24), nullable=False),
    )
    op.create_index("ix_research_experiment_config_hash", "research_experiment_registry", ["config_hash"])


def downgrade() -> None:
    op.drop_index("ix_research_experiment_config_hash", table_name="research_experiment_registry")
    op.drop_table("research_experiment_registry")
    with op.batch_alter_table("alpha_feature_snapshots") as batch:
        batch.drop_constraint("uq_alpha_snapshot_version_mode", type_="unique")
        batch.create_unique_constraint("uq_alpha_snapshot_version",
                                       ["market_snapshot_id", "alpha_version"])
    for name in ("feature_calculated_at", "response_received_at", "source_market_timestamp",
                 "confirmation_reset_reason", "underlying_horizon_volatility", "participation_state",
                 "validity_state", "hypothesis_type", "signed_log_return", "actual_horizon_seconds",
                 "lookback_clock_mode", "session_date", "session_id", "calculation_mode"):
        op.drop_column("alpha_feature_snapshots", name)
    for name in ("ask_quantity", "bid_quantity", "source_market_timestamp", "exchange"):
        op.drop_column("option_contract_snapshots", name)
    for name in ("response_received_at", "request_started_at", "source_market_timestamp",
                 "future_expiry", "future_instrument_id"):
        op.drop_column("market_snapshots", name)
