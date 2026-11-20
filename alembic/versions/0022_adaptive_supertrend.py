"""Add isolated AST_V1 evidence; no changes to existing execution schemas."""

from alembic import op
import sqlalchemy as sa

revision = "0022_adaptive_supertrend"
down_revision = "0021_commodity_paper_trades"
branch_labels = None
depends_on = None

IMMUTABLE = ("ast_optimizer_runs", "ast_session_parameters", "ast_paper_events")


def upgrade():
    op.create_table(
        "ast_optimizer_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document", sa.Text(), nullable=False),
    )
    op.create_table(
        "ast_session_parameters",
        sa.Column("trading_date", sa.Date(), primary_key=True),
        sa.Column("optimizer_run_id", sa.String(64), sa.ForeignKey("ast_optimizer_runs.id"), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
    )
    op.create_table(
        "ast_paper_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer()),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ast_single_cursor"),
    )
    op.execute(
        "INSERT INTO ast_paper_cursor (id,sequence,head_hash,lock_version,state_json,latest_json) "
        "VALUES (1,0,'GENESIS',0,'{}','{}')"
    )
    op.create_table(
        "ast_paper_trades",
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
            "state IN ('PENDING_PAPER_ENTRY','OPEN','CLOSED','ENTRY_REJECTED'," "'UNRESOLVED_EXPOSURE')",
            name="ck_ast_trade_state",
        ),
    )
    for name in ("trading_date", "closed_date"):
        op.create_index(f"ix_ast_paper_trades_{name}", "ast_paper_trades", [name])
    op.create_index("ix_ast_paper_trades_state", "ast_paper_trades", ["state"])
    op.create_table(
        "ast_paper_events",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("event_key", sa.String(220), unique=True, nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey("scalper_market_snapshots.id"), nullable=False),
        sa.Column("trade_id", sa.String(64), sa.ForeignKey("ast_paper_trades.id")),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_ast_event_trade_sequence", "ast_paper_events", ["trade_id", "sequence"])
    op.create_index("ix_ast_event_type_sequence", "ast_paper_events", ["event_type", "sequence"])
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_ast_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'AST_EVIDENCE_IS_IMMUTABLE'; END; $$"
        )
        for table in IMMUTABLE:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION reject_ast_mutation()"
            )
    elif dialect == "sqlite":
        for table in IMMUTABLE:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'AST_EVIDENCE_IS_IMMUTABLE'); END"
                )
    else:
        raise RuntimeError("AST_REQUIRES_SQLITE_OR_POSTGRESQL")


def downgrade():
    for table in (
        "ast_paper_events",
        "ast_paper_trades",
        "ast_paper_cursor",
        "ast_session_parameters",
        "ast_optimizer_runs",
    ):
        op.drop_table(table)
    if op.get_context().dialect.name == "postgresql":
        op.execute("DROP FUNCTION reject_ast_mutation()")
