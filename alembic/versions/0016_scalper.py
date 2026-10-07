"""Add isolated Phase 15 scalper snapshots and immutable paper journal."""
from alembic import op
import sqlalchemy as sa

revision = "0016_scalper"
down_revision = "0015_forward_paper"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scalper_market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("capture_key", sa.String(64), nullable=False, unique=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_market_timestamp", sa.DateTime(timezone=True)),
        sa.Column("nifty_spot", sa.Numeric(20, 6), nullable=False),
        sa.Column("nifty_future", sa.Numeric(20, 6)),
        sa.Column("future_instrument_id", sa.String(160)),
        sa.Column("future_expiry", sa.Date()),
        sa.Column("india_vix", sa.Numeric(20, 6)),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("atm_strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("feature_json", sa.Text(), nullable=False),
        sa.Column("signal_json", sa.Text(), nullable=False),
        sa.Column("context_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()))
    op.create_index("ix_scalper_snapshots_captured_at", "scalper_market_snapshots",
                    ["captured_at"])
    op.create_index("ix_scalper_snapshots_expiry", "scalper_market_snapshots", ["expiry"])
    op.create_table(
        "scalper_option_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scalper_snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("trading_symbol", sa.String(160), nullable=False),
        sa.Column("instrument_token", sa.String(160), nullable=False),
        sa.Column("source_market_timestamp", sa.DateTime(timezone=True)),
        sa.Column("bid", sa.Numeric(20, 6)), sa.Column("ask", sa.Numeric(20, 6)),
        sa.Column("bid_quantity", sa.BigInteger()), sa.Column("ask_quantity", sa.BigInteger()),
        sa.Column("depth_unit", sa.String(12), nullable=False),
        sa.Column("tick_size", sa.Numeric(20, 6)), sa.Column("ltp", sa.Numeric(20, 6)),
        sa.Column("volume", sa.BigInteger()), sa.Column("open_interest", sa.BigInteger()),
        sa.UniqueConstraint("scalper_snapshot_id", "expiry", "strike", "option_type",
                            "exchange", "trading_symbol", "instrument_token",
                            name="uq_scalper_quote_identity"),
        sa.CheckConstraint("option_type IN ('CE','PE')", name="ck_scalper_quote_type"))
    op.create_index("ix_scalper_quotes_snapshot", "scalper_option_quotes",
                    ["scalper_snapshot_id"])
    op.create_index("ix_scalper_quotes_contract", "scalper_option_quotes",
                    ["expiry", "strike", "option_type"])
    op.create_table(
        "scalper_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()), sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("confirmation_direction", sa.String(8)),
        sa.Column("confirmation_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_scalper_single_cursor"))
    op.execute(
        "INSERT INTO scalper_cursor "
        "(id, sequence, head_hash, lock_version, confirmation_count) "
        "VALUES (1, 0, 'GENESIS', 0, 0)"
    )
    op.create_table(
        "scalper_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("decision_snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False, unique=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('SIGNAL','PENDING_ENTRY','OPEN','CLOSED',"
            "'ENTRY_REJECTED','UNRESOLVED')",
            name="ck_scalper_trade_state"))
    op.create_index("ix_scalper_trades_state", "scalper_trades", ["state"])
    op.create_table(
        "scalper_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(220), nullable=False, unique=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("snapshot_id", sa.Integer(),
                  sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64), sa.ForeignKey("scalper_trades.id")),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False))
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_scalper_evidence_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
            "'SCALPER_EVIDENCE_IS_IMMUTABLE'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER scalper_evidence_immutable BEFORE UPDATE OR DELETE OR "
            "TRUNCATE ON scalper_events FOR EACH STATEMENT EXECUTE FUNCTION "
            "reject_scalper_evidence_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER scalper_evidence_no_{action.lower()} BEFORE {action} "
                "ON scalper_events BEGIN SELECT "
                "RAISE(ABORT, 'SCALPER_EVIDENCE_IS_IMMUTABLE'); END"
            )
    else:
        raise RuntimeError("scalper supports PostgreSQL and SQLite only")


def downgrade():
    op.drop_table("scalper_events")
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_scalper_evidence_mutation()")
    op.drop_index("ix_scalper_trades_state", "scalper_trades")
    op.drop_table("scalper_trades")
    op.drop_table("scalper_cursor")
    op.drop_index("ix_scalper_quotes_contract", "scalper_option_quotes")
    op.drop_index("ix_scalper_quotes_snapshot", "scalper_option_quotes")
    op.drop_table("scalper_option_quotes")
    op.drop_index("ix_scalper_snapshots_expiry", "scalper_market_snapshots")
    op.drop_index("ix_scalper_snapshots_captured_at", "scalper_market_snapshots")
    op.drop_table("scalper_market_snapshots")
