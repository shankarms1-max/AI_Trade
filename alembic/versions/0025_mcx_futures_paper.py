"""Add the independent ACTS_V3_MCX_FUTURES paper journal (CRUDEOILM, NATGASMINI). Purely additive: no existing
table, row or constraint is modified, so NIFTY ACTS, Commodity ACTS and AST evidence are untouched."""

from alembic import op
import sqlalchemy as sa

revision = "0025_mcx_futures_paper"
down_revision = "0024_ast_direct_paper"
branch_labels = None
depends_on = None

INST = "instrument IN ('CRUDEOILM','NATGASMINI')"
STATES = ("PENDING_PAPER_ENTRY", "OPEN", "CLOSED", "ENTRY_REJECTED", "UNRESOLVED_EXPOSURE")


def upgrade():
    op.create_table(
        "mcx_futures_cursors",
        sa.Column("instrument", sa.String(16), primary_key=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("head_hash", sa.String(64), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.String(64)),
        sa.Column("engine_version", sa.String(64)),
        sa.Column("config_hash", sa.String(64)),
        sa.Column("contract_json", sa.Text(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("latest_json", sa.Text(), nullable=False),
        sa.CheckConstraint(INST, name="ck_mcx_futures_cursor_instrument"),
    )
    for name in ("CRUDEOILM", "NATGASMINI"):
        op.execute(
            "INSERT INTO mcx_futures_cursors (instrument,sequence,head_hash,lock_version,contract_json,state_json,"
            f"latest_json) VALUES ('{name}',0,'GENESIS',0,'{{}}','{{}}','{{}}')"
        )
    op.create_table(
        "mcx_futures_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instrument", sa.String(16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("at", sa.String(64), nullable=False),
        sa.Column("trade_id", sa.String(64)),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("instrument", "sequence", name="uq_mcx_futures_event_sequence"),
        sa.CheckConstraint(INST, name="ck_mcx_futures_event_instrument"),
    )
    for name in ("instrument", "event_type", "trade_id"):
        op.create_index(f"ix_mcx_futures_events_{name}", "mcx_futures_events", [name])
    op.create_table(
        "mcx_futures_trades",
        sa.Column("trade_id", sa.String(64), primary_key=True),
        sa.Column("instrument", sa.String(16), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.CheckConstraint(INST, name="ck_mcx_futures_trade_instrument"),
        sa.CheckConstraint("state IN (" + ",".join(f"'{s}'" for s in STATES) + ")", name="ck_mcx_futures_trade_state"),
    )
    for name in ("instrument", "trading_date", "state"):
        op.create_index(f"ix_mcx_futures_trades_{name}", "mcx_futures_trades", [name])


def downgrade():
    # Evidence-preserving: refuse to drop journals that contain events.
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT count(*) FROM mcx_futures_events")).scalar():
        raise RuntimeError("MCX_FUTURES_JOURNAL_NOT_EMPTY_REFUSING_DOWNGRADE")
    for table in ("mcx_futures_trades", "mcx_futures_events", "mcx_futures_cursors"):
        op.drop_table(table)
