"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-14
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    signal_source = postgresql.ENUM(
        "sma_crossover", "news", "filing", "rsi_mean_reversion", name="signal_source"
    )
    proposal_side = postgresql.ENUM("buy", "sell", name="proposal_side")
    proposal_tier = postgresql.ENUM("tier_1", "tier_2", "tier_3", name="proposal_tier")
    trade_status = postgresql.ENUM(
        "pending", "filled", "partial", "cancelled", "rejected", name="trade_status"
    )
    trade_sleeve = postgresql.ENUM("trend", "premium", "mean_reversion", name="trade_sleeve")
    risk_event_type = postgresql.ENUM(
        "rejection", "circuit_breaker", "kill_switch", name="risk_event_type"
    )

    bind = op.get_bind()
    for e in (signal_source, proposal_side, proposal_tier, trade_status, trade_sleeve, risk_event_type):
        e.create(bind, checkfirst=True)

    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("source", postgresql.ENUM(name="signal_source", create_type=False), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("signal_type", sa.String(64), nullable=False),
        sa.Column("raw_data", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_signals_timestamp", "signals", ["timestamp"])
    op.create_index("ix_signals_ticker", "signals", ["ticker"])

    op.create_table(
        "trade_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", postgresql.ENUM(name="proposal_side", create_type=False), nullable=False),
        sa.Column("proposed_size_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("target_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("thesis", sa.Text, nullable=False),
        sa.Column("confidence", sa.Integer, nullable=False),
        sa.Column("model_used", sa.String(64), nullable=True),
        sa.Column("tier", postgresql.ENUM(name="proposal_tier", create_type=False), nullable=False),
        sa.Column("rejected_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("confidence BETWEEN 1 AND 10", name="ck_confidence_range"),
    )
    op.create_index("ix_proposals_ticker", "trade_proposals", ["ticker"])
    op.create_index("ix_proposals_created_at", "trade_proposals", ["created_at"])

    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trade_proposals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("alpaca_order_id", sa.String(64), nullable=True),
        sa.Column("status", postgresql.ENUM(name="trade_status", create_type=False), nullable=False),
        sa.Column("filled_qty", sa.Numeric(18, 6), nullable=True),
        sa.Column("filled_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(18, 6), nullable=True),
        sa.Column("sleeve", postgresql.ENUM(name="trade_sleeve", create_type=False), nullable=False),
    )
    op.create_index("ix_trades_alpaca_order_id", "trades", ["alpaca_order_id"])
    op.create_index("ix_trades_opened_at", "trades", ["opened_at"])
    op.create_index("ix_trades_closed_at", "trades", ["closed_at"])

    op.create_table(
        "positions",
        sa.Column("ticker", sa.String(16), primary_key=True),
        sa.Column("qty", sa.Numeric(18, 6), nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("current_stop", sa.Numeric(18, 6), nullable=True),
        sa.Column("current_target", sa.Numeric(18, 6), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sleeve", postgresql.ENUM(name="trade_sleeve", create_type=False), nullable=False),
    )

    op.create_table(
        "risk_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("event_type", postgresql.ENUM(name="risk_event_type", create_type=False),
                  nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("related_proposal_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trade_proposals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("account_state_snapshot", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_risk_events_timestamp", "risk_events", ["timestamp"])

    op.create_table(
        "sanitization_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=True),
        sa.Column("original_excerpt", sa.Text, nullable=False),
        sa.Column("stripped_fragments", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("sanitized_text", sa.Text, nullable=False),
    )
    op.create_index("ix_sanitization_log_timestamp", "sanitization_log", ["timestamp"])


def downgrade() -> None:
    op.drop_table("sanitization_log")
    op.drop_table("risk_events")
    op.drop_table("positions")
    op.drop_table("trades")
    op.drop_table("trade_proposals")
    op.drop_table("signals")
    for name in ("risk_event_type", "trade_sleeve", "trade_status",
                 "proposal_tier", "proposal_side", "signal_source"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
