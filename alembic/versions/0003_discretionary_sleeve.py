"""add discretionary value to trade_sleeve enum

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14
"""
from typing import Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres 12+ allows ADD VALUE inside a transaction.
    op.execute("ALTER TYPE trade_sleeve ADD VALUE IF NOT EXISTS 'discretionary'")


def downgrade() -> None:
    # Postgres doesn't support removing enum values directly. Recreate the type.
    op.execute("ALTER TYPE trade_sleeve RENAME TO trade_sleeve_old")
    op.execute("CREATE TYPE trade_sleeve AS ENUM ('trend', 'premium', 'mean_reversion')")
    op.execute(
        "ALTER TABLE trades ALTER COLUMN sleeve "
        "TYPE trade_sleeve USING sleeve::text::trade_sleeve"
    )
    op.execute(
        "ALTER TABLE positions ALTER COLUMN sleeve "
        "TYPE trade_sleeve USING sleeve::text::trade_sleeve"
    )
    op.execute("DROP TYPE trade_sleeve_old")
