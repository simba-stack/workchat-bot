"""coins + user_coin_balances + transfers + swaps

Revision ID: 0003_coins_balances_transfers
Revises: 0002_deposit_requests
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0003_coins_balances_transfers"
down_revision = "0002_deposit_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── coins ────────────────────────────────────────────────────────
    op.create_table(
        "coins",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("coingecko_id", sa.String(64), nullable=True),
        sa.Column("networks", sa.ARRAY(sa.String(16)), nullable=False, server_default="{}"),
        sa.Column("decimals", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("icon_color", sa.String(8), nullable=True),
        sa.Column("icon_url", sa.String(256), nullable=True),
        sa.Column("min_deposit", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("min_withdraw", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("withdraw_fee", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_coins_code", "coins", ["code"], unique=True)
    op.create_index("idx_coins_active", "coins", ["is_active"])

    # Seed coins
    op.execute("""
        INSERT INTO coins (code, name, coingecko_id, networks, decimals, icon_color,
                           min_deposit, min_withdraw, withdraw_fee, sort_order) VALUES
        ('USDT',  'Tether',       'tether',       ARRAY['TRC20','ERC20','BEP20','TON'], 6, '#26A17B', 1,    2,    1,    10),
        ('TON',   'Toncoin',      'the-open-network', ARRAY['TON'],                     9, '#0098EA', 1,    1,    0.05, 20),
        ('TRX',   'TRON',         'tron',         ARRAY['TRC20'],                       6, '#FF060A', 5,    5,    1,    30),
        ('BTC',   'Bitcoin',      'bitcoin',      ARRAY['BTC'],                         8, '#F7931A', 0.0001, 0.0005, 0.0001, 40),
        ('ETH',   'Ethereum',     'ethereum',     ARRAY['ERC20'],                       18, '#627EEA', 0.01, 0.01, 0.003, 50),
        ('SOL',   'Solana',       'solana',       ARRAY['SPL'],                         9, '#9945FF', 0.05, 0.1,  0.01, 60),
        ('USDC',  'USD Coin',     'usd-coin',     ARRAY['TRC20','ERC20','BEP20','SPL'], 6, '#2775CA', 1,    2,    1,    15),
        ('BNB',   'Binance Coin', 'binancecoin',  ARRAY['BEP20'],                       18, '#F3BA2F', 0.005, 0.01, 0.001, 70),
        ('DOGE',  'Dogecoin',     'dogecoin',     ARRAY['DOGE'],                        8, '#C2A633', 5,    5,    2,    80),
        ('LTC',   'Litecoin',     'litecoin',     ARRAY['LTC'],                         8, '#345D9D', 0.001, 0.001, 0.0003, 90),
        ('XAUT',  'Tether Gold',  'tether-gold',  ARRAY['ERC20'],                       6, '#D4AF37', 0.001, 0.001, 0.0005, 95),
        ('RUB',   'Российский рубль', NULL,       ARRAY[]::varchar[],                   2, '#FF3B30', 100,  100, 0,    100);
    """)

    # ─── user_coin_balances ──────────────────────────────────────────
    op.create_table(
        "user_coin_balances",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("coin_code", sa.String(16), nullable=False),
        sa.Column("balance", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "coin_code", name="uq_user_coin"),
    )
    op.create_index("idx_ucb_user", "user_coin_balances", ["user_id"])

    # ─── transfers ────────────────────────────────────────────────────
    op.create_table(
        "transfers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("from_user_id", sa.BigInteger(), nullable=False),
        sa.Column("to_user_id", sa.BigInteger(), nullable=False),
        sa.Column("coin_code", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("comment", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["from_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_user_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("idx_transfer_from", "transfers", ["from_user_id"])
    op.create_index("idx_transfer_to", "transfers", ["to_user_id"])
    op.create_index("idx_transfer_status", "transfers", ["status"])

    # ─── swaps ────────────────────────────────────────────────────────
    op.create_table(
        "swaps",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("from_coin", sa.String(16), nullable=False),
        sa.Column("to_coin", sa.String(16), nullable=False),
        sa.Column("from_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("to_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee_pct", sa.Numeric(4, 2), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_swap_user", "swaps", ["user_id"])

    # Backfill: переносим существующий User.balance_usdt в user_coin_balances(USDT)
    op.execute("""
        INSERT INTO user_coin_balances (user_id, coin_code, balance, updated_at)
        SELECT id, 'USDT', balance_usdt, NOW() FROM users WHERE balance_usdt > 0
        ON CONFLICT (user_id, coin_code) DO NOTHING;
    """)


def downgrade() -> None:
    op.drop_index("idx_swap_user", "swaps")
    op.drop_table("swaps")
    op.drop_index("idx_transfer_status", "transfers")
    op.drop_index("idx_transfer_to", "transfers")
    op.drop_index("idx_transfer_from", "transfers")
    op.drop_table("transfers")
    op.drop_index("idx_ucb_user", "user_coin_balances")
    op.drop_table("user_coin_balances")
    op.drop_index("idx_coins_active", "coins")
    op.drop_index("idx_coins_code", "coins")
    op.drop_table("coins")
