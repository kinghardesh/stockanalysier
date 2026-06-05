"""trade_lessons — permanent AI-analyzed post-mortems of losing trades

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-05
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_lessons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("ticker",       sa.String(16),          nullable=False),
        sa.Column("sector",       sa.String(64),          nullable=True),
        sa.Column("signal_type",  sa.String(32),          nullable=True),
        sa.Column("entry_price",  sa.Numeric(18, 6),      nullable=True),
        sa.Column("exit_price",   sa.Numeric(18, 6),      nullable=True),
        sa.Column("qty",          sa.Integer(),           nullable=True),
        sa.Column("realized_pnl", sa.Numeric(18, 6),      nullable=True),
        sa.Column("pnl_pct",      sa.Numeric(10, 4),      nullable=True),
        sa.Column("entry_thesis", sa.Text(),              nullable=True),
        sa.Column("loss_reason",  sa.Text(),              nullable=True),
        sa.Column("lesson",       sa.Text(),              nullable=True),
        sa.Column("raw_context",  postgresql.JSONB,       nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_trade_lessons_ticker", "trade_lessons", ["ticker"])
    op.create_index("ix_trade_lessons_sector", "trade_lessons", ["sector"])


def downgrade() -> None:
    op.drop_index("ix_trade_lessons_sector", "trade_lessons")
    op.drop_index("ix_trade_lessons_ticker", "trade_lessons")
    op.drop_table("trade_lessons")
