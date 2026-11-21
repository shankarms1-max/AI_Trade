"""Start an independent futures journal. Preserve all earlier AST evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0023_ast_futures_paper"
down_revision = "0022_adaptive_supertrend"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ast_futures_paper_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ast_futures_single_cursor"),
    )
    op.execute(
        "INSERT INTO ast_futures_paper_cursor (id,sequence,head_hash,lock_version,state_json,latest_json) "
        "VALUES (1,0,'GENESIS',0,'{}','{}')"
    )
    op.create_table(
        "ast_futures_paper_trades",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("closed_date", sa.Date()),
        sa.Column(
            "decision_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("scalper_market_snapshots.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING_PAPER_ENTRY','OPEN','PENDING_PAPER_EXIT','CLOSED','ENTRY_REJECTED',"
            "'UNRESOLVED_EXPOSURE')",
            name="ck_ast_futures_trade_state",
        ),
    )
    for name in ("trading_date", "closed_date"):
        op.create_index(f"ix_ast_futures_paper_trades_{name}", "ast_futures_paper_trades", [name])
    op.create_index("ix_ast_futures_paper_trades_state", "ast_futures_paper_trades", ["state"])
    op.create_table(
        "ast_futures_paper_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(220), unique=True, nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64), sa.ForeignKey("ast_futures_paper_trades.id")),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_ast_futures_event_trade_sequence", "ast_futures_paper_events", ["trade_id", "sequence"])
    op.create_index("ix_ast_futures_event_type_sequence", "ast_futures_paper_events", ["event_type", "sequence"])
    table = "ast_futures_paper_events"
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_ast_mutation()"
        )
    elif dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'AST_EVIDENCE_IS_IMMUTABLE'); END"
            )
    else:
        raise RuntimeError("AST_REQUIRES_SQLITE_OR_POSTGRESQL")


def downgrade():
    # Never silently discard a futures journal on rollback.
    if op.get_context().as_sql:
        raise RuntimeError("AST_FUTURES_DOWNGRADE_REQUIRES_ONLINE_EVIDENCE_CHECK")
    for table in ("ast_futures_paper_events", "ast_futures_paper_trades"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("AST_FUTURES_DOWNGRADE_REQUIRES_EMPTY_JOURNAL")
    for table in ("ast_futures_paper_events", "ast_futures_paper_trades", "ast_futures_paper_cursor"):
        op.drop_table(table)
