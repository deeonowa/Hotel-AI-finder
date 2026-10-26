"""SQLite-хранилище сессий, корзин и заказов. Владелец: FS.

Почему SQLite, а не Postgres: у жюри запуск в одну команду без внешних сервисов.
Порты SessionStore/BookingRepository позволяют заменить на Postgres/Redis без правок агента.
Храним JSON целиком — схема в Pydantic, миграции не нужны для прототипа.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..schemas import Cart, Order, TripState


class SqliteStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        for table in ("sessions", "carts", "orders"):
            self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
        self._db.commit()

    def _put(self, table: str, key: str, body: str) -> None:
        with self._lock:
            self._db.execute(
                f"INSERT INTO {table}(id, body) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body, updated_at=CURRENT_TIMESTAMP",
                (key, body),
            )
            self._db.commit()

    def _get(self, table: str, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
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
