"""add in_universe flag to assets (curated liquid scan universe)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-02
"""
from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column(
        "in_universe", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.create_index("ix_assets_in_universe", "assets", ["in_universe"])


def downgrade() -> None:
    op.drop_index("ix_assets_in_universe", "assets")
    op.drop_column("assets", "in_universe")
