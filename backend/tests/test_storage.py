"""Хранилище: одинаковое поведение Postgres и in-memory, миграции идемпотентны."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from trip_agent.schemas import Cart, Order, TripState
from trip_agent.storage import MEMORY_URL, open_store

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    url = MEMORY_URL if request.param == "memory" else request.getfixturevalue("pg_url")
    s = open_store(url)
    yield s
    s.close()


def _state(sid: str, **kw) -> TripState:
    return TripState(session_id=sid, created_at=NOW, updated_at=NOW, **kw)


def test_session_roundtrip_and_upsert(store):
    assert store.load("nope") is None
    st = _state("s-roundtrip", user_id="u1", dismissed_fixes=["h1:far"])
    store.save(st)
    assert store.load("s-roundtrip") == st

    st2 = st.model_copy(update={"version": 3, "turn_count": 2})
    store.save(st2)
    assert store.load("s-roundtrip") == st2


def test_cart_and_order_roundtrip(store):
    assert store.get_cart("nope") is None and store.get_order("nope") is None
    cart = Cart(id="c-1", session_id="s-1", total=12345, updated_at=NOW)
    store.save_cart(cart)
    assert store.get_cart("c-1") == cart

    order = Order(id="o-1", number="OST-2610-0001", session_id="s-1", items=[], total=12345, created_at=NOW, guest_name="Иван")
    store.save_order(order)
    assert store.get_order("o-1") == order
    paid = order.model_copy(update={"paid_at": NOW, "payment_ref": "SIM-1"})
    store.save_order(paid)
    assert store.get_order("o-1") == paid


def test_concurrent_writes(store):
    def work(i: int) -> None:
        store.save(_state(f"s-par-{i % 5}", version=i))

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, range(40)))
    assert all(store.load(f"s-par-{i}") is not None for i in range(5))


def test_migrations_idempotent_and_tracked(pg_url):
    import psycopg
    from trip_agent.storage.migrations import MIGRATIONS, migrate

    s = open_store(pg_url)  # первый старт накатывает схему
    s.close()
    with psycopg.connect(pg_url) as conn:
        assert migrate(conn) == []  # повторно — ничего
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [v for v, _, _ in MIGRATIONS]
        cols = dict(conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'sessions'").fetchall())
        assert cols["body"] == "jsonb"


def test_postgres_updated_at_bumps(pg_url):
    import psycopg

    s = open_store(pg_url)
    try:
        s.save(_state("s-ts"))
        with psycopg.connect(pg_url) as conn:
            (t1,) = conn.execute("SELECT updated_at FROM sessions WHERE id = 's-ts'").fetchone()
        s.save(_state("s-ts", version=1))
        with psycopg.connect(pg_url) as conn:
            (t2,) = conn.execute("SELECT updated_at FROM sessions WHERE id = 's-ts'").fetchone()
        assert t2 >= t1
    finally:
        s.close()


def test_bad_database_url():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        open_store("sqlite:///x.db")
