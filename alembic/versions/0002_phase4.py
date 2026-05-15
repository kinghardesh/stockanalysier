"""phase 4: trades.model_used denormalization + daily_summary + cancel_requested flag

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Denormalize model_used onto trades so EOD attribution doesn't require a join.
    op.add_column("trades", sa.Column("model_used", sa.String(64), nullable=True))
    op.create_index("ix_trades_model_used", "trades", ["model_used"])

    # Cancel/replace flag so the position monitor can request an exit
    # without racing the order stream consumer.
    op.add_column("trades", sa.Column("cancel_requested", sa.Boolean(), nullable=False,
                                       server_default=sa.text("false")))

    op.create_table(
        "daily_summary",
        sa.Column("trading_date", sa.Date(), primary_key=True),
        sa.Column("starting_equity", sa.Numeric(18, 6), nullable=False),
        sa.Column("ending_equity", sa.Numeric(18, 6), nullable=False),
        sa.Column("total_pnl", sa.Numeric(18, 6), nullable=False),
        sa.Column("fill_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("mechanical_pnl", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("llm_pnl", sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("by_model", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("by_sleeve", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("proposals_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("proposals_executed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("proposals_rejected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("daily_summary")
    op.drop_index("ix_trades_model_used", "trades")
    op.drop_column("trades", "cancel_requested")
    op.drop_column("trades", "model_used")
