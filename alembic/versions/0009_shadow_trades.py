"""shadow_trades table (would-be trades from the screen->LLM->risk pipeline)

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-02
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("screen_date", sa.Date(), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("signal", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=True),
        sa.Column("entry", sa.Numeric(18, 6), nullable=True),
        sa.Column("stop", sa.Numeric(18, 6), nullable=True),
        sa.Column("target", sa.Numeric(18, 6), nullable=True),
        sa.Column("tier", sa.String(24), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("horizon", sa.String(12), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_shadow_trades_date", "shadow_trades", ["screen_date"])


def downgrade() -> None:
    op.drop_index("ix_shadow_trades_date", "shadow_trades")
    op.drop_table("shadow_trades")
