"""add time_horizon enum + column on trade_proposals

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-31
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    time_horizon = postgresql.ENUM(
        "intraday", "swing", "position", name="time_horizon"
    )
    time_horizon.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "trade_proposals",
        sa.Column(
            "time_horizon",
            postgresql.ENUM(name="time_horizon", create_type=False),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_trade_proposals_time_horizon", "trade_proposals", ["time_horizon"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_proposals_time_horizon", "trade_proposals")
    op.drop_column("trade_proposals", "time_horizon")
    op.execute("DROP TYPE IF EXISTS time_horizon")
