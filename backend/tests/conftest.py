from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from trip_agent.config import Settings
from trip_agent.container import build

TODAY = date(2026, 10, 26)

# Задан → весь набор (агент, API) работает на Postgres, каждая сессия pytest — в своей схеме.
# Не задан → БД в памяти, а Postgres-тесты хранилища пропускаются (или падают при REQUIRE_POSTGRES=1).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
REQUIRE_POSTGRES = os.environ.get("REQUIRE_POSTGRES") == "1"


@pytest.fixture(scope="session")
def pg_url():
    """URL на изолированную схему test_<uuid> в TEST_DATABASE_URL; схема удаляется после прогона."""
    if not TEST_DATABASE_URL:
        if REQUIRE_POSTGRES:
            pytest.fail("REQUIRE_POSTGRES=1, но TEST_DATABASE_URL не задан")
        pytest.skip("TEST_DATABASE_URL не задан — Postgres-тесты пропущены")
    from urllib.parse import quote

    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        sep = "&" if "?" in TEST_DATABASE_URL else "?"
        yield f"{TEST_DATABASE_URL}{sep}options={quote(f'-c search_path={schema}')}"
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(scope="session")
def database_url(request):
    return request.getfixturevalue("pg_url") if TEST_DATABASE_URL or REQUIRE_POSTGRES else "memory://"


@pytest.fixture(scope="session")
def app(database_url):
    """Агент в режиме правил (без LLM), фиксированное «сегодня», БД — Postgres или в памяти."""
    ctx = build(Settings(agent_mode="rules"), today=lambda: TODAY, database_url=database_url)
    yield ctx
    ctx.store.close()


@pytest.fixture()
def agent(app):
    return app.agent


@pytest.fixture(scope="session")
def location(app):
    return app.catalog.get_location("sochi")


@pytest.fixture(scope="session")
def hotels(app):
    return app.catalog.list_hotels("sochi")
