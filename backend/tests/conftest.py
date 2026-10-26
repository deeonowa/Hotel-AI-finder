from __future__ import annotations

from datetime import date

import pytest
from trip_agent.config import Settings
from trip_agent.container import build

TODAY = date(2026, 10, 26)


@pytest.fixture(scope="session")
def app():
    """Агент в режиме правил (без LLM), фиксированное «сегодня», БД в памяти."""
    return build(Settings(agent_mode="rules"), today=lambda: TODAY, db_path=":memory:")


@pytest.fixture()
def agent(app):
    return app.agent


@pytest.fixture(scope="session")
def location(app):
    return app.catalog.get_location("sochi")


@pytest.fixture(scope="session")
def hotels(app):
    return app.catalog.list_hotels("sochi")
