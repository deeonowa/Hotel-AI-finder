"""Postgres-хранилище сессий, корзин и заказов. Владелец: FS.

Реализует порты SessionStore/BookingRepository. Документы храним целиком в JSONB — схема живёт
в Pydantic, а колонки created_at/updated_at дают простую аналитику и чистку старых сессий.
Пул соединений потокобезопасен: FastAPI гоняет sync-хендлеры в тредпуле.
"""

from __future__ import annotations

import logging

from psycopg import sql
from psycopg_pool import ConnectionPool

from ..schemas import Cart, Order, TripState
from .migrations import migrate

log = logging.getLogger(__name__)

TABLES = ("sessions", "carts", "orders")


class PostgresStore:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10, auto_migrate: bool = True):
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=False, kwargs={"autocommit": False})
        self.pool.open(wait=True, timeout=30)
        if auto_migrate:
            with self.pool.connection() as conn:
                applied = migrate(conn)
            if applied:
                log.info("postgres migrations applied: %s", applied)

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> bool:
        with self.pool.connection() as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    def _put(self, table: str, key: str, body: str) -> None:
        q = sql.SQL(
            "INSERT INTO {t}(id, body) VALUES (%s, %s::jsonb) ON CONFLICT (id) DO UPDATE SET body = EXCLUDED.body, updated_at = now()"
        ).format(t=sql.Identifier(table))
        with self.pool.connection() as conn:
            conn.execute(q, (key, body))

    def _get(self, table: str, key: str) -> str | None:
        # body::text — чтобы валидировать как JSON (как пишем), а не через python-dict
        q = sql.SQL("SELECT body::text FROM {t} WHERE id = %s").format(t=sql.Identifier(table))
        with self.pool.connection() as conn:
            row = conn.execute(q, (key,)).fetchone()
        return row[0] if row else None

    # SessionStore
    def load(self, session_id: str) -> TripState | None:
        raw = self._get("sessions", session_id)
        return TripState.model_validate_json(raw) if raw else None

    def save(self, state: TripState) -> None:
        self._put("sessions", state.session_id, state.model_dump_json())

    # BookingRepository
    def get_cart(self, cart_id: str) -> Cart | None:
        raw = self._get("carts", cart_id)
        return Cart.model_validate_json(raw) if raw else None

    def save_cart(self, cart: Cart) -> None:
        self._put("carts", cart.id, cart.model_dump_json())

    def get_order(self, order_id: str) -> Order | None:
        raw = self._get("orders", order_id)
        return Order.model_validate_json(raw) if raw else None

    def save_order(self, order: Order) -> None:
        self._put("orders", order.id, order.model_dump_json())
