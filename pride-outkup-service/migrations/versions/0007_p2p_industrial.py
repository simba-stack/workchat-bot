"""P2P Industrial-grade: расширения Offer/Deal + PriceIndex + FeatureFlag + MakerStatsSnapshot.

Revision ID: 0007
Revises: 0006

Все новые колонки nullable / с дефолтом → существующие записи продолжат работать.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Offers: новые колонки ──────────────────────────────────────────
    with op.batch_alter_table("offers") as b:
        b.add_column(sa.Column("price_type", sa.String(8), server_default="fixed", nullable=False))
        # 'fixed' = price_value RUB/USDT statique; 'float' = margin% от индекса
        b.add_column(sa.Column("float_margin_pct", sa.Numeric(6, 2), nullable=True))
        # для float: e.g. 100.50 = +0.5% от индекса
        b.add_column(sa.Column("coin", sa.String(16), server_default="USDT", nullable=False))
        b.add_column(sa.Column("fiat", sa.String(8), server_default="RUB", nullable=False))
        b.add_column(sa.Column("pay_window_min", sa.Integer(), server_default="30", nullable=False))
        b.add_column(sa.Column("min_taker_completed", sa.Integer(), server_default="0", nullable=False))
        b.add_column(sa.Column("require_kyc", sa.Boolean(), server_default="false", nullable=False))
        b.add_column(sa.Column("region", sa.String(16), nullable=True))
        b.add_column(sa.Column("paused_reason", sa.String(64), nullable=True))
        # авто-paused при выходе из price band

    op.create_index("idx_offers_coin_fiat_side", "offers", ["coin", "fiat", "side", "status"])

    # ─── Deals: новые колонки ───────────────────────────────────────────
    with op.batch_alter_table("deals") as b:
        b.add_column(sa.Column("coin", sa.String(16), server_default="USDT", nullable=False))
        b.add_column(sa.Column("fiat", sa.String(8), server_default="RUB", nullable=False))
        b.add_column(sa.Column("pay_deadline_at", sa.DateTime(timezone=True), nullable=True))
        # parallel к expires_at — используем для consistent naming с спекой

    op.create_index("idx_deals_pay_deadline", "deals", ["status", "pay_deadline_at"])

    # ─── Users: maker tier ──────────────────────────────────────────────
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("maker_tier", sa.String(16), server_default="none", nullable=False))
        # none|bronze|silver|gold|official
        b.add_column(sa.Column("maker_tier_updated_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("cancel_cooldown_until", sa.DateTime(timezone=True), nullable=True))
        # anti-fraud: до этой даты юзер не может открывать новые deals

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
        # p2p|wallet|cheques|swap|kyc|admin|bot|miniapp
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("config", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_status", sa.String(16), nullable=True),
        # ok|fail|unknown
        sa.Column("last_check_note", sa.String(512), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    op.create_index("ux_feature_flags_key", "feature_flags", ["key"], unique=True)
    op.create_index("idx_feature_flags_category", "feature_flags", ["category"])

    # ─── DealAppeal — отдельная таблица для аудита аппеляций ───────────
    # (Dispute уже есть, но добавим расширенные поля через JSONB extra)
    # Не дублируем — пользуемся существующим Dispute.


def downgrade() -> None:
    op.drop_index("idx_feature_flags_category", "feature_flags")
    op.drop_index("ux_feature_flags_key", "feature_flags")
    op.drop_table("feature_flags")

    op.drop_index("ux_price_indices_pair", "price_indices")
    op.drop_table("price_indices")

    with op.batch_alter_table("users") as b:
        b.drop_column("cancel_cooldown_until")
        b.drop_column("maker_tier_updated_at")
        b.drop_column("maker_tier")

    op.drop_index("idx_deals_pay_deadline", "deals")
    with op.batch_alter_table("deals") as b:
        b.drop_column("pay_deadline_at")
        b.drop_column("fiat")
        b.drop_column("coin")

    op.drop_index("idx_offers_coin_fiat_side", "offers")
    with op.batch_alter_table("offers") as b:
        b.drop_column("paused_reason")
        b.drop_column("region")
        b.drop_column("require_kyc")
        b.drop_column("min_taker_completed")
        b.drop_column("pay_window_min")
        b.drop_column("fiat")
        b.drop_column("coin")
        b.drop_column("float_margin_pct")
        b.drop_column("price_type")
