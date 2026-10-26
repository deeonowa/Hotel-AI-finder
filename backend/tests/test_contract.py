"""Тесты контракта: ломаются, если кто-то меняет общие схемы/словари без согласования."""

from __future__ import annotations

import json

from trip_agent.ranking.prefs import SCORERS
from trip_agent.requirements_ops import PREF_LABELS
from trip_agent.schemas import Hotel, PrefKey, TurnRequest


def test_every_pref_has_handler_and_label():
    missing_scorer = [k for k in PrefKey if k not in SCORERS]
    missing_label = [k for k in PrefKey if k not in PREF_LABELS]
    assert not missing_scorer, f"добавьте обработчик в ranking/prefs.py: {missing_scorer}"
    assert not missing_label, f"добавьте подпись в requirements_ops.PREF_LABELS: {missing_label}"


def test_location_pack_and_hotels_valid(location, hotels):
    assert location.districts and location.hubs
    assert len(hotels) >= 50
    ids = [h.id for h in hotels]
    assert len(ids) == len(set(ids)), "id отелей должны быть уникальны"
    district_ids = {d.id for d in location.districts}
    assert all(h.district_id in district_ids for h in hotels)
    assert all(isinstance(h, Hotel) and h.room_types for h in hotels)


def test_turn_request_requires_exactly_one_input():
    import pytest

    with pytest.raises(ValueError):
        TurnRequest()
    with pytest.raises(ValueError):
        TurnRequest(text="x", action={"type": "reset"})
    assert TurnRequest(action={"type": "reset"}).action.type == "reset"


def test_openapi_schema_builds():
    from trip_agent.api.app import create_app
    from trip_agent.config import Settings

    api = create_app(Settings(agent_mode="rules", db_path=":memory:"))
    schema = api.openapi()
    assert "/api/v1/sessions/{session_id}/turns" in schema["paths"]
    # дискриминированные объединения должны попасть в схему — из неё генерируются TS-типы
    assert "TextBlock" in json.dumps(schema)
