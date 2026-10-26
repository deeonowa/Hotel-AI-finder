"""In-memory хранилище для офлайн-инструментов (eval, экспорт OpenAPI, `trip_agent chat`).

Включается через DATABASE_URL=memory:// — сервису Postgres не нужен, чтобы прогнать оценку
или выгрузить схему. В проде (docker compose) всегда PostgresStore.
"""

from __future__ import annotations

import threading

from ..schemas import Cart, Order, TripState


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {"sessions": {}, "carts": {}, "orders": {}}

    def close(self) -> None:
        pass

    def ping(self) -> bool:
        return True

    def _put(self, table: str, key: str, body: str) -> None:
        with self._lock:
            self._data[table][key] = body

    def _get(self, table: str, key: str) -> str | None:
        with self._lock:
            return self._data[table].get(key)

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
