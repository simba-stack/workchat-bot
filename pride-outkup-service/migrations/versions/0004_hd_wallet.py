"""HD-Wallet: system_secrets + user_deposit_addresses

Revision ID: 0004_hd_wallet
Revises: 0003_coins_balances_transfers
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


revision = "0004_hd_wallet"
down_revision = "0003_coins_balances_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_secrets",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(2048), nullable=False),
        sa.Column("is_encrypted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    op.create_table(
        "user_deposit_addresses",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("coin_code", sa.String(16), nullable=False),
        sa.Column("network", sa.String(16), nullable=False),
        sa.Column("address", sa.String(80), nullable=False),
        sa.Column("derivation_index", sa.Integer(), nullable=False),
        sa.Column("last_balance", sa.String(32), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "coin_code", "network", name="uq_uda_user_coin_net"),
    )
    op.create_index("idx_uda_user", "user_deposit_addresses", ["user_id"])
    op.create_index("idx_uda_address", "user_deposit_addresses", ["address"])


def downgrade() -> None:
    op.drop_index("idx_uda_address", "user_deposit_addresses")
    op.drop_index("idx_uda_user", "user_deposit_addresses")
    op.drop_table("user_deposit_addresses")
    op.drop_table("system_secrets")
