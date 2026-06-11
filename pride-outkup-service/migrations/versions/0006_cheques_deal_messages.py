"""Cheques + DealMessages tables.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cheques",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("creator_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("coin_code", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("comment", sa.String(500)),
        sa.Column("status", sa.String(16), server_default="active"),
        sa.Column("redeemed_by_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_cheques_creator_user_id", "cheques", ["creator_user_id"])
    op.create_index("ix_cheques_code", "cheques", ["code"], unique=True)
    op.create_index("ix_cheques_status_creator", "cheques", ["status", "creator_user_id"])

    op.create_table(
        "deal_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("deal_id", sa.Integer(), sa.ForeignKey("deals.id"), nullable=False),
        sa.Column("from_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_deal_messages_deal_id", "deal_messages", ["deal_id"])
    op.create_index("ix_deal_messages_deal", "deal_messages", ["deal_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_deal_messages_deal", "deal_messages")
    op.drop_index("ix_deal_messages_deal_id", "deal_messages")
    op.drop_table("deal_messages")
    op.drop_index("ix_cheques_status_creator", "cheques")
    op.drop_index("ix_cheques_code", "cheques")
    op.drop_index("ix_cheques_creator_user_id", "cheques")
    op.drop_table("cheques")
