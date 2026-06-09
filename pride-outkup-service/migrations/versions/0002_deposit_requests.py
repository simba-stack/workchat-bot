"""deposit_requests table

Revision ID: 0002_deposit_requests
Revises: 0001_initial
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0002_deposit_requests"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deposit_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("base_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("exact_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("to_address", sa.String(64), nullable=False),
        sa.Column("network", sa.String(16), nullable=False, server_default="TRC20"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("matched_tx_id", sa.String(128), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_amount", sa.Numeric(14, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_dr_user", "deposit_requests", ["user_id"])
    op.create_index("idx_dr_status", "deposit_requests", ["status"])
    op.create_index("idx_dr_exact", "deposit_requests", ["exact_amount", "status"])


def downgrade() -> None:
    op.drop_index("idx_dr_exact", "deposit_requests")
    op.drop_index("idx_dr_status", "deposit_requests")
    op.drop_index("idx_dr_user", "deposit_requests")
    op.drop_table("deposit_requests")
