"""P2P REST API — CQRS split.

- commands.py — POST endpoints, выполняются через Orchestrator
- queries.py  — GET endpoints, читают напрямую из БД
- admin.py    — арбитр / суперадмин эндпоинты
"""
