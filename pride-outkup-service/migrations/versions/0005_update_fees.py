"""Update withdraw fees to competitive values (vs Crypto Bot $5.5 USDT).

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10

С учётом интеграции Feee.io energy rental — реальный газ USDT ~$0.35,
комиссия $3.5 даёт чистый профит $3.15 за withdraw.
Crypto Bot для сравнения: $5.5 USDT.

Также подняли min_withdraw для всех чтобы покрыть газ.
"""
from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # USDT: 1 → 3.5 (главная монета)
    op.execute("UPDATE coins SET withdraw_fee = 3.5,  min_withdraw = 5    WHERE code = 'USDT'")
    op.execute("UPDATE coins SET withdraw_fee = 0.1,  min_withdraw = 0.5  WHERE code = 'TON'")
    op.execute("UPDATE coins SET withdraw_fee = 5,    min_withdraw = 20   WHERE code = 'TRX'")
    op.execute("UPDATE coins SET withdraw_fee = 0.0002, min_withdraw = 0.001 WHERE code = 'BTC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.005, min_withdraw = 0.01  WHERE code = 'ETH'")
    op.execute("UPDATE coins SET withdraw_fee = 0.02,  min_withdraw = 0.1   WHERE code = 'SOL'")
    op.execute("UPDATE coins SET withdraw_fee = 3.5,   min_withdraw = 5     WHERE code = 'USDC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.002, min_withdraw = 0.01  WHERE code = 'BNB'")
    op.execute("UPDATE coins SET withdraw_fee = 5,     min_withdraw = 10    WHERE code = 'DOGE'")
    op.execute("UPDATE coins SET withdraw_fee = 0.001, min_withdraw = 0.005 WHERE code = 'LTC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.001, min_withdraw = 0.005 WHERE code = 'XAUT'")


def downgrade() -> None:
    # Возврат к старым значениям (если что-то пошло не так)
    op.execute("UPDATE coins SET withdraw_fee = 1,    min_withdraw = 2    WHERE code = 'USDT'")
    op.execute("UPDATE coins SET withdraw_fee = 0.05, min_withdraw = 1    WHERE code = 'TON'")
    op.execute("UPDATE coins SET withdraw_fee = 1,    min_withdraw = 5    WHERE code = 'TRX'")
    op.execute("UPDATE coins SET withdraw_fee = 0.0001, min_withdraw = 0.0005 WHERE code = 'BTC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.003, min_withdraw = 0.01    WHERE code = 'ETH'")
    op.execute("UPDATE coins SET withdraw_fee = 0.01,  min_withdraw = 0.1     WHERE code = 'SOL'")
    op.execute("UPDATE coins SET withdraw_fee = 1,     min_withdraw = 2       WHERE code = 'USDC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.001, min_withdraw = 0.01    WHERE code = 'BNB'")
    op.execute("UPDATE coins SET withdraw_fee = 2,     min_withdraw = 5       WHERE code = 'DOGE'")
    op.execute("UPDATE coins SET withdraw_fee = 0.0003, min_withdraw = 0.001  WHERE code = 'LTC'")
    op.execute("UPDATE coins SET withdraw_fee = 0.0005, min_withdraw = 0.001  WHERE code = 'XAUT'")
