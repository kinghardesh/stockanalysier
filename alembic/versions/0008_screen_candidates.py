"""screen_candidates table (daily mechanical screen output)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-02
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "screen_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("screen_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("signal", sa.String(20), nullable=False),
        sa.Column("score", sa.Numeric(12, 4), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("sma50", sa.Numeric(18, 6), nullable=True),
        sa.Column("sma200", sa.Numeric(18, 6), nullable=True),
        sa.Column("rsi", sa.Numeric(8, 4), nullable=True),
        sa.Column("suggested_stop", sa.Numeric(18, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_screen_candidates_date_rank", "screen_candidates",
                    ["screen_date", "rank"])


def downgrade() -> None:
    op.drop_index("ix_screen_candidates_date_rank", "screen_candidates")
    op.drop_table("screen_candidates")
