"""PRIDE P2P v2 — enterprise marketplace module.

Архитектура (см. EPS Том 1-23):
- Ledger Engine — единственный источник истины для финансов (double-entry)
- Wallet Projection — projection поверх Ledger (для быстрого чтения)
- State Engine — контролирует State Transition Matrix
- Policy Engine — все бизнес-правила в одном месте
- Transaction Orchestrator — единая точка выполнения workflow
- Outbox Pattern — гарантированная доставка событий
- Lock Manager — pessimistic locks через advisory + SELECT FOR UPDATE
- Audit Log — immutable, fixated в той же транзакции что и business data

Принципы:
- Fail Closed (любая неопределённость → запрет операции)
- Idempotency на каждом mutate-endpoint
- Σ Debit = Σ Credit для каждой Ledger Transaction
- Никаких прямых вызовов между сервисами — только через Orchestrator
- Decimal для денег, никогда float
- Все даты UTC

Файловая структура:
  p2p/
  ├── enums.py       — статусы, типы
  ├── models.py      — SQLAlchemy модели (14 таблиц)
  ├── policies.py    — Policy Engine + дефолтные политики
  ├── locks.py       — Lock Manager (advisory + row locks)
  ├── ledger.py      — Ledger Engine (double-entry)
  ├── wallet.py      — Wallet Projection Engine
  ├── state.py       — State Machine (transition matrix)
  ├── outbox.py      — Outbox Pattern + Inbox
  ├── audit.py       — Audit Log helpers
  ├── orchestrator.py — Transaction Orchestrator (фундамент workflow)
  ├── workflows/     — конкретные бизнес-процессы
  ├── api/           — Command + Query endpoints
  └── workers/       — Outbox Publisher, Scheduler, Reconciliation
"""
