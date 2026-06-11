"""P2P Industrial-grade: расширения Offer/Deal + PriceIndex + FeatureFlag.

Revision ID: 0007
Revises: 0006

Все новые колонки nullable / с server_default -> существующие записи
продолжат работать. На PostgreSQL используем прямые op.add_column (не batch).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Offers: новые колонки ──────────────────────────────────────────
    op.add_column("offers", sa.Column("price_type", sa.String(8), server_default="fixed", nullable=False))
    op.add_column("offers", sa.Column("float_margin_pct", sa.Numeric(6, 2), nullable=True))
    op.add_column("offers", sa.Column("coin", sa.String(16), server_default="USDT", nullable=False))
    op.add_column("offers", sa.Column("fiat", sa.String(8), server_default="RUB", nullable=False))
    op.add_column("offers", sa.Column("pay_window_min", sa.Integer(), server_default="30", nullable=False))
    op.add_column("offers", sa.Column("min_taker_completed", sa.Integer(), server_default="0", nullable=False))
    op.add_column("offers", sa.Column("require_kyc", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("offers", sa.Column("region", sa.String(16), nullable=True))
    op.add_column("offers", sa.Column("paused_reason", sa.String(64), nullable=True))
    op.create_index("idx_offers_coin_fiat_side", "offers", ["coin", "fiat", "side", "status"])

    # ─── Deals: новые колонки ───────────────────────────────────────────
    op.add_column("deals", sa.Column("coin", sa.String(16), server_default="USDT", nullable=False))
    op.add_column("deals", sa.Column("fiat", sa.String(8), server_default="RUB", nullable=False))
    op.add_column("deals", sa.Column("pay_deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("idx_deals_pay_deadline", "deals", ["status", "pay_deadline_at"])

    # ─── Users: maker tier ──────────────────────────────────────────────
    op.add_column("users", sa.Column("maker_tier", sa.String(16), server_default="none", nullable=False))
    op.add_column("users", sa.Column("maker_tier_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("cancel_cooldown_until", sa.DateTime(timezone=True), nullable=True))

    # ─── PriceIndex ─────────────────────────────────────────────────────
    op.create_table(
        "price_indices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("coin", sa.String(16), nullable=False),
        sa.Column("fiat", sa.String(8), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("source", sa.String(32), server_default="coingecko"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ux_price_indices_pair", "price_indices", ["coin", "fiat"], unique=True)

    # ─── FeatureFlag ────────────────────────────────────────────────────
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_status", sa.String(16), nullable=True),
        sa.Column("last_check_note", sa.String(512), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    op.create_index("ux_feature_flags_key", "feature_flags", ["key"], unique=True)
    op.create_index("idx_feature_flags_category", "feature_flags", ["category"])


def downgrade() -> None:
    op.drop_index("idx_feature_flags_category", "feature_flags")
    op.drop_index("ux_feature_flags_key", "feature_flags")
    op.drop_table("feature_flags")

    op.drop_index("ux_price_indices_pair", "price_indices")
    op.drop_table("price_indices")

    op.drop_column("users", "cancel_cooldown_until")
    op.drop_column("users", "maker_tier_updated_at")
    op.drop_column("users", "maker_tier")

    op.drop_index("idx_deals_pay_deadline", "deals")
    op.drop_column("deals", "pay_deadline_at")
    op.drop_column("deals", "fiat")
    op.drop_column("deals", "coin")

    op.drop_index("idx_offers_coin_fiat_side", "offers")
    op.drop_column("offers", "paused_reason")
    op.drop_column("offers", "region")
    op.drop_column("offers", "require_kyc")
    op.drop_column("offers", "min_taker_completed")
    op.drop_column("offers", "pay_window_min")
    op.drop_column("offers", "fiat")
    op.drop_column("offers", "coin")
    op.drop_column("offers", "float_margin_pct")
    op.drop_column("offers", "price_type")
