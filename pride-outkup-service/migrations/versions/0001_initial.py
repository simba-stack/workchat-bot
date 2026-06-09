"""initial — users, orders, offers, deals, escrow, disputes, ops

Revision ID: 0001
Revises:
Create Date: 2026-06-09 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── users ────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("username", sa.String(64)),
        sa.Column("full_name", sa.String(256)),
        sa.Column("phone", sa.String(32)),
        sa.Column("kyc_level", sa.Integer, default=0, nullable=False, server_default="0"),
        sa.Column("kyc_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("kyc_data", postgresql.JSONB),
        sa.Column("kyc_video_url", sa.String(512)),
        sa.Column("kyc_decided_at", sa.DateTime(timezone=True)),
        sa.Column("kyc_decided_by", sa.String(64)),
        sa.Column("trc20_address", sa.String(64)),
        sa.Column("balance_usdt", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("is_partner", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trust_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("invited_by_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("notifications_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("anti_phishing_code", sa.String(32)),
        sa.Column("language", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("total_deals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completed_deals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cancelled_deals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("disputed_deals", sa.Integer, nullable=False, server_default="0"),
        sa.Column("avg_release_time_sec", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_volume_usdt", sa.Numeric(16, 4), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_users_kyc", "users", ["kyc_status"])
    op.create_index("idx_users_partner", "users", ["is_partner"])

    # ─── orders ───────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("order_number", sa.String(32), nullable=False, unique=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount_rub_remaining", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate_rub_per_usdt", sa.Numeric(8, 2), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(14, 4), nullable=False),
        sa.Column("pct_fee", sa.Numeric(4, 2), nullable=False),
        sa.Column("destination", sa.String(16), nullable=False),
        sa.Column("destination_addr", sa.String(64)),
        sa.Column("bank_in", sa.String(32)),
        sa.Column("bank_out", sa.String(32)),
        sa.Column("payment_method", sa.String(32)),
        sa.Column("payout_target", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("assigned_to_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("cancelled_reason", sa.String(256)),
        sa.Column("source", sa.String(32), nullable=False, server_default="miniapp"),
        sa.Column("extra", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_orders_user", "orders", ["user_id"])
    op.create_index("idx_orders_status", "orders", ["status"])
    op.create_index("idx_orders_assigned", "orders", ["assigned_to_id"])

    # ─── order_payments ──────────────────────────────────────────
    op.create_table(
        "order_payments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_number", sa.String(32), nullable=False),
        sa.Column("bank", sa.String(32), nullable=False),
        sa.Column("phone_or_card", sa.String(64), nullable=False),
        sa.Column("receiver_name", sa.String(128)),
        sa.Column("amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("manager_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("duration_minutes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("warning_sent", sa.Numeric(1, 0), nullable=False, server_default="0"),
        sa.Column("receipt_url", sa.String(512)),
        sa.Column("receipt_uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="waiting_receipt"),
        sa.Column("rejected_reason", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_payments_order", "order_payments", ["order_id"])
    op.create_index("idx_payments_status", "order_payments", ["status"])

    # ─── offers ──────────────────────────────────────────────────
    op.create_table(
        "offers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("rate_rub_per_usdt", sa.Numeric(8, 2), nullable=False),
        sa.Column("min_amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("payment_methods", postgresql.ARRAY(sa.String(32)), nullable=False, server_default="{}"),
        sa.Column("conditions", sa.String(1024)),
        sa.Column("auto_reply", sa.String(1024)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("is_pride_official", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("filled_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cancelled_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_volume_usdt", sa.Numeric(16, 4), nullable=False, server_default="0"),
        sa.Column("extra", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_offers_active", "offers", ["side", "status"])
    op.create_index("idx_offers_user", "offers", ["user_id"])
    op.create_index("idx_offers_rate", "offers", ["rate_rub_per_usdt"])

    # ─── deals ───────────────────────────────────────────────────
    op.create_table(
        "deals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("deal_number", sa.String(32), nullable=False, unique=True),
        sa.Column("offer_id", sa.BigInteger, sa.ForeignKey("offers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("buyer_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("seller_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_rub", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate_rub_per_usdt", sa.Numeric(8, 2), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(14, 4), nullable=False),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("bank", sa.String(32)),
        sa.Column("phone_or_card", sa.String(64)),
        sa.Column("receiver_name", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("receipt_url", sa.String(512)),
        sa.Column("txid", sa.String(128)),
        sa.Column("fee_usdt", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("fee_pct", sa.Numeric(4, 2), nullable=False, server_default="0.3"),
        sa.Column("cancelled_reason", sa.String(256)),
        sa.Column("extra", postgresql.JSONB),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_deals_buyer", "deals", ["buyer_id"])
    op.create_index("idx_deals_seller", "deals", ["seller_id"])
    op.create_index("idx_deals_status", "deals", ["status"])

    # ─── escrow_locks ────────────────────────────────────────────
    op.create_table(
        "escrow_locks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(14, 4), nullable=False),
        sa.Column("deal_id", sa.BigInteger, sa.ForeignKey("deals.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="locked"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_escrow_user", "escrow_locks", ["user_id"])
    op.create_index("idx_escrow_deal", "escrow_locks", ["deal_id"])
    op.create_index("idx_escrow_status", "escrow_locks", ["status"])

    # ─── disputes ────────────────────────────────────────────────
    op.create_table(
        "disputes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("deal_id", sa.BigInteger, sa.ForeignKey("deals.id", ondelete="SET NULL")),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="SET NULL")),
        sa.Column("opened_by_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reason", sa.String(1024), nullable=False),
        sa.Column("evidence_urls", postgresql.ARRAY(sa.String(512)), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(32)),
        sa.Column("resolved_by_admin", sa.String(64)),
        sa.Column("resolution_note", sa.String(2048)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_disputes_deal", "disputes", ["deal_id"])
    op.create_index("idx_disputes_order", "disputes", ["order_id"])
    op.create_index("idx_disputes_status", "disputes", ["status"])

    # ─── chat_messages ───────────────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("deal_id", sa.BigInteger, sa.ForeignKey("deals.id", ondelete="CASCADE")),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id", ondelete="CASCADE")),
        sa.Column("sender_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("text", sa.String(4000)),
        sa.Column("attachment_url", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_chat_deal", "chat_messages", ["deal_id"])
    op.create_index("idx_chat_order", "chat_messages", ["order_id"])
    op.create_index("idx_chat_created", "chat_messages", ["created_at"])

    # ─── operations_log ──────────────────────────────────────────
    op.create_table(
        "operations_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(14, 4), nullable=False),
        sa.Column("balance_before", sa.Numeric(14, 4)),
        sa.Column("balance_after", sa.Numeric(14, 4)),
        sa.Column("ref_table", sa.String(32)),
        sa.Column("ref_id", sa.BigInteger),
        sa.Column("txid", sa.String(128)),
        sa.Column("note", sa.String(512)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_oplog_user_time", "operations_log", ["user_id", "created_at"])
    op.create_index("idx_oplog_type", "operations_log", ["type"])
    op.create_index("idx_oplog_ref", "operations_log", ["ref_table", "ref_id"])

    # ─── tron_outbound_log ───────────────────────────────────────
    op.create_table(
        "tron_outbound_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("to_address", sa.String(64), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(14, 4), nullable=False),
        sa.Column("reason", sa.String(256)),
        sa.Column("txid", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("error_msg", sa.String(1024)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_tron_user", "tron_outbound_log", ["user_id"])
    op.create_index("idx_tron_status", "tron_outbound_log", ["status"])
    op.create_index("idx_tron_txid", "tron_outbound_log", ["txid"])

    # ─── settings (key-value для глобал-конфига курса, fee, etc) ──
    op.create_table(
        "kv_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Seed: default rate
    op.execute("""
        INSERT INTO kv_settings (key, value) VALUES
            ('rate_buy_usdt', '84.00'::jsonb),
            ('rate_sell_usdt', '82.00'::jsonb),
            ('pct_fee_v1', '3.5'::jsonb),
            ('pct_fee_v2', '0.3'::jsonb),
            ('feature_v2_p2p_public', 'false'::jsonb)
        ON CONFLICT (key) DO NOTHING;
    """)


def downgrade() -> None:
    op.drop_table("kv_settings")
    op.drop_table("tron_outbound_log")
    op.drop_table("operations_log")
    op.drop_table("chat_messages")
    op.drop_table("disputes")
    op.drop_table("escrow_locks")
    op.drop_table("deals")
    op.drop_table("offers")
    op.drop_table("order_payments")
    op.drop_table("orders")
    op.drop_table("users")
