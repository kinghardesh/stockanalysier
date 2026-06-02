"""assets universe table (full Alpaca-tradable list)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-02
"""
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("exchange", sa.String(16), nullable=True),
        sa.Column("asset_class", sa.String(24), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("tradable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("marginable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("shortable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("easy_to_borrow", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fractionable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_assets_exchange", "assets", ["exchange"])
    op.create_index("ix_assets_tradable", "assets", ["tradable"])


def downgrade() -> None:
    op.drop_index("ix_assets_tradable", "assets")
    op.drop_index("ix_assets_exchange", "assets")
    op.drop_table("assets")
