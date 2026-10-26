"""Хранилища сессий/корзин/заказов. Выбор реализации — по DATABASE_URL (см. open_store)."""

from __future__ import annotations

from .memory import MemoryStore

MEMORY_URL = "memory://"


def open_store(url: str):  # -> PostgresStore | MemoryStore
    if url == MEMORY_URL:
        return MemoryStore()
    if not url.startswith(("postgresql://", "postgres://")):
        raise ValueError(f"DATABASE_URL: ожидается postgresql://... или {MEMORY_URL}, получено {url.split('://')[0]}://")
    from .postgres import PostgresStore  # psycopg импортируем только когда нужен

    return PostgresStore(url)
