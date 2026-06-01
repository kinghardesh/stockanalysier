"""market data warehouse: daily_bars, company_fundamentals, market_data

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-01
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Permanent daily OHLCV history.
    op.create_table(
        "daily_bars",
        sa.Column("ticker", sa.String(16), primary_key=True),
        sa.Column("bar_date", sa.Date(), primary_key=True),
        sa.Column("open", sa.Numeric(18, 6), nullable=False),
        sa.Column("high", sa.Numeric(18, 6), nullable=False),
        sa.Column("low", sa.Numeric(18, 6), nullable=False),
        sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(18, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_daily_bars_bar_date", "daily_bars", ["bar_date"])

    # Permanent latest-snapshot fundamentals.
    op.create_table(
        "company_fundamentals",
        sa.Column("ticker", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("sector", sa.String(64), nullable=True),
        sa.Column("industry", sa.String(128), nullable=True),
        sa.Column("market_cap", sa.Numeric(24, 2), nullable=True),
        sa.Column("pe_ratio", sa.Numeric(14, 4), nullable=True),
        sa.Column("forward_pe", sa.Numeric(14, 4), nullable=True),
        sa.Column("eps", sa.Numeric(14, 4), nullable=True),
        sa.Column("dividend_yield", sa.Numeric(10, 6), nullable=True),
        sa.Column("beta", sa.Numeric(10, 4), nullable=True),
        sa.Column("fifty_two_week_high", sa.Numeric(18, 6), nullable=True),
        sa.Column("fifty_two_week_low", sa.Numeric(18, 6), nullable=True),
        sa.Column("next_earnings_date", sa.Date(), nullable=True),
        sa.Column("raw", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # Hot intraday market events (bars + trades), 30-day retention then archived.
    op.create_table(
        "market_data",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("price", sa.Numeric(18, 6), nullable=True),
        sa.Column("open", sa.Numeric(18, 6), nullable=True),
        sa.Column("high", sa.Numeric(18, 6), nullable=True),
        sa.Column("low", sa.Numeric(18, 6), nullable=True),
        sa.Column("close", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_market_data_ticker_time", "market_data", ["ticker", "event_time"])
    op.create_index("ix_market_data_ingested_at", "market_data", ["ingested_at"])


def downgrade() -> None:
    op.drop_index("ix_market_data_ingested_at", "market_data")
    op.drop_index("ix_market_data_ticker_time", "market_data")
    op.drop_table("market_data")
    op.drop_table("company_fundamentals")
    op.drop_index("ix_daily_bars_bar_date", "daily_bars")
    op.drop_table("daily_bars")
