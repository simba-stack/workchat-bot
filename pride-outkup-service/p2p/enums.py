"""Все статусы и enum-типы P2P v2.

Из ТЗ Том 12 — State Transition Matrix.
Хранятся как VARCHAR в БД (не Postgres ENUM — чтобы можно было добавлять без миграций).
"""
from enum import Enum


class AdvertisementType(str, Enum):
    """Тип объявления — кто что делает."""
    SELL = "SELL"  # Мейкер продаёт крипту → нужен Advertisement Hold
    BUY = "BUY"    # Мейкер покупает крипту → Hold не нужен


class AdvertisementStatus(str, Enum):
    """Жизненный цикл объявления (ТЗ Том 3 §3)."""
    DRAFT = "DRAFT"          # Черновик, не виден другим
    ACTIVE = "ACTIVE"        # Опубликован, можно создавать сделки
    PAUSED = "PAUSED"        # На паузе (вручную или авто)
    ARCHIVED = "ARCHIVED"    # В архиве, редактирование запрещено
    DELETED = "DELETED"      # Soft delete


class PricingMode(str, Enum):
    """Режим ценообразования."""
    FIXED = "FIXED"
    FLOATING = "FLOATING"


class TradeStatus(str, Enum):
    """Жизненный цикл сделки (ТЗ Том 4 §5)."""
    CREATED = "CREATED"                          # Сделка только создана, escrow ещё не залочен
    ESCROW_LOCKED = "ESCROW_LOCKED"              # Эскроу заблокирован, чат создан
    WAITING_FOR_PAYMENT = "WAITING_FOR_PAYMENT"  # Покупатель должен оплатить
    PAYMENT_MARKED = "PAYMENT_MARKED"            # Покупатель нажал "Я оплатил"
    PAYMENT_CONFIRMATION = "PAYMENT_CONFIRMATION"  # Продавец проверяет
    DISPUTE_OPENED = "DISPUTE_OPENED"            # Открыт спор
    ARBITRATION = "ARBITRATION"                  # Арбитр назначен
    RESOLVED = "RESOLVED"                        # Арбитр принял решение
    COMPLETED = "COMPLETED"                      # Успешно завершена
    CANCELLED = "CANCELLED"                      # Отменена


class DisputeStatus(str, Enum):
    """Статусы спора (ТЗ Том 12 §6)."""
    OPENED = "OPENED"
    ARBITRATION = "ARBITRATION"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class DisputeResolution(str, Enum):
    """Решение арбитра."""
    BUYER = "BUYER"      # USDT уходит покупателю
    SELLER = "SELLER"    # USDT возвращается продавцу
    SPLIT = "SPLIT"      # Зарезервировано на будущее
    REJECTED = "REJECTED"  # Спор закрыт без решения


class PaymentMethodType(str, Enum):
    """Типы способов оплаты."""
    SBP = "SBP"
    SBER = "SBER"
    TINKOFF = "TINKOFF"
    ALPHA = "ALPHA"
    VTB = "VTB"
    RAIF = "RAIF"
    GAZPROM = "GAZPROM"
    OZON = "OZON"
    CASH = "CASH"
    IBAN = "IBAN"
    SEPA = "SEPA"
    REVOLUT = "REVOLUT"
    PAYPAL = "PAYPAL"
    BLIK = "BLIK"
    PIX = "PIX"
    UPI = "UPI"
    OTHER = "OTHER"


class PaymentMethodStatus(str, Enum):
    """Статус способа оплаты."""
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ARCHIVED = "ARCHIVED"


class MessageType(str, Enum):
    """Типы сообщений в чате сделки."""
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    DOCUMENT = "DOCUMENT"
    VOICE = "VOICE"
    SYSTEM = "SYSTEM"        # Автоматическое системное сообщение
    ARBITRATION = "ARBITRATION"  # От арбитра
    PAYMENT_PROOF = "PAYMENT_PROOF"  # Доказательство оплаты


class MessageStatus(str, Enum):
    """Статус доставки сообщения."""
    WAITING = "WAITING"
    UPLOADING = "UPLOADING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


class LedgerAccountType(str, Enum):
    """Типы бухгалтерских счетов (ТЗ Том 21 §4)."""
    USER_AVAILABLE = "USER_AVAILABLE"         # Свободный баланс юзера
    USER_ESCROW = "USER_ESCROW"               # Эскроу под конкретную сделку
    ADVERTISEMENT_HOLD = "ADVERTISEMENT_HOLD"  # Заморозка под объявление
    USER_FROZEN = "USER_FROZEN"               # Заморожено админом/AML
    USER_PENDING = "USER_PENDING"             # Временное состояние внутри операции
    SYSTEM_FEES = "SYSTEM_FEES"               # Системный счёт комиссий
    PLATFORM_REVENUE = "PLATFORM_REVENUE"     # Доход платформы
    WITHDRAWAL_PENDING = "WITHDRAWAL_PENDING"  # Ожидает вывода
    DEPOSIT_PENDING = "DEPOSIT_PENDING"       # Ожидает пополнения
    FRAUD_HOLD = "FRAUD_HOLD"                 # Заблокировано Fraud Engine
    AML_HOLD = "AML_HOLD"                     # Заблокировано AML
    INSURANCE_FUND = "INSURANCE_FUND"         # Страховой фонд


class WalletBalanceCategory(str, Enum):
    """Категории остатков в Wallet Projection (ТЗ Том 20 §3)."""
    AVAILABLE = "AVAILABLE"
    ADVERTISEMENT_HOLD = "ADVERTISEMENT_HOLD"
    TRADE_ESCROW = "TRADE_ESCROW"
    FROZEN = "FROZEN"
    PENDING = "PENDING"


class WorkflowStatus(str, Enum):
    """Статус workflow в Orchestrator (ТЗ Том 16 §5)."""
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"
    DEAD = "DEAD"


class OutboxStatus(str, Enum):
    """Статус события в Outbox (ТЗ Том 19 §6)."""
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class EventType(str, Enum):
    """Доменные события (ТЗ Том 4 §5, неполный список — расширяется)."""
    ADVERTISEMENT_CREATED = "AdvertisementCreated"
    ADVERTISEMENT_UPDATED = "AdvertisementUpdated"
    ADVERTISEMENT_PAUSED = "AdvertisementPaused"
    ADVERTISEMENT_RESUMED = "AdvertisementResumed"
    ADVERTISEMENT_DELETED = "AdvertisementDeleted"
    TRADE_CREATED = "TradeCreated"
    TRADE_CANCELLED = "TradeCancelled"
    TRADE_PAYMENT_MARKED = "TradePaymentMarked"
    TRADE_PAYMENT_CONFIRMED = "TradePaymentConfirmed"
    TRADE_COMPLETED = "TradeCompleted"
    TRADE_DISPUTED = "TradeDisputed"
    TRADE_RESOLVED = "TradeResolved"
    TRADE_EXPIRED = "TradeExpired"
    WALLET_RESERVED = "WalletReserved"
    WALLET_RELEASED = "WalletReleased"
    WALLET_TRANSFERRED = "WalletTransferred"
    WALLET_FROZEN = "WalletFrozen"
    WALLET_UNFROZEN = "WalletUnfrozen"
    WALLET_UPDATED = "WalletUpdated"
    CHAT_MESSAGE_SENT = "ChatMessageSent"
    CHAT_FILE_UPLOADED = "ChatFileUploaded"
    NOTIFICATION_CREATED = "NotificationCreated"
    USER_ONLINE = "UserOnline"
    USER_OFFLINE = "UserOffline"
    MERCHANT_RATING_CHANGED = "MerchantRatingChanged"
    DISPUTE_OPENED = "DisputeOpened"
    DISPUTE_RESOLVED = "DisputeResolved"
    DISPUTE_CLOSED = "DisputeClosed"
    RECON_FAILED = "ReconciliationFailed"
    SYSTEM_ALERT = "SystemAlert"


class RiskDecision(str, Enum):
    """Решение Risk Engine."""
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    DENY = "DENY"


class P2PUserRole(str, Enum):
    """Роли P2P (отдельно от ролей биржи) — ТЗ Том 9."""
    GUEST = "GUEST"
    USER = "USER"
    MERCHANT = "MERCHANT"
    ARBITRATOR = "ARBITRATOR"
    SUPPORT = "SUPPORT"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"
    SYSTEM = "SYSTEM"
