"""Несколько направлений в разных странах: данные, понимание города, сравнение направлений, документы."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from trip_agent.agent.nlu_rules import RuleNlu
from trip_agent.api.app import create_app
from trip_agent.config import Settings
from trip_agent.schemas import Intent, Requirements, TripState, TurnRequest

from .conftest import TODAY

CITIES = {"sochi": "RU", "antalya": "TR", "dubai": "AE", "phuket": "TH", "tbilisi": "GE"}


def _say(agent, sid, text):
    return agent.turn(sid, TurnRequest(text=text))


def _act(agent, sid, **a):
    return agent.turn(sid, TurnRequest(action=a))


def _blocks(r, t):
    return [b for b in r.blocks if b.type == t]


def _state(location_id: str = "sochi") -> TripState:
    now = date(2026, 10, 26)
    from datetime import datetime

    ts = datetime(now.year, now.month, now.day)
    return TripState(session_id="t", created_at=ts, updated_at=ts, requirements=Requirements(location_id=location_id))


@pytest.fixture(scope="module")
def nlu(app):
    return RuleNlu(app.locations.packs())


# ------------------------------------------------------------------ данные
def test_all_cities_are_loaded_with_real_data(app):
    assert set(app.locations.ids) == set(CITIES)
    for lid, cc in CITIES.items():
        loc = app.catalog.get_location(lid)
        hotels = app.catalog.list_hotels(lid)
        assert loc.country_code == cc
        assert len(hotels) >= 700, f"{lid}: мало отелей"
        assert all(h.location_id == lid and h.provenance.source == "osm" for h in hotels)
        assert {h.district_id for h in hotels} <= {d.id for d in loc.districts}
        zone = next(iter(loc.climate))
        assert len(loc.climate[zone]) == 12, f"{lid}: нет климата"
        assert loc.routes.get("Москва"), f"{lid}: нет рейсов из Москвы"
        # за границей — правила въезда с источником и датой проверки; внутри страны их нет
        assert (loc.entry is not None) == (cc != "RU")
        if loc.entry:
            assert loc.entry.sources and loc.entry.visa_free_days


def test_district_ids_are_unique_across_cities(app):
    ids = [d.id for lp in app.locations.packs() for d in lp.districts]
    assert len(ids) == len(set(ids))


# ------------------------------------------------------------------ понимание
@pytest.mark.parametrize(
    "text,loc,districts",
    [
        ("хотим в Турцию на море в сентябре", "antalya", []),
        ("летим из Москвы в Дубай на неделю", "dubai", []),
        ("в Патонг на 10 ночей в декабре", "phuket", ["patong"]),
        ("Тбилиси на выходные, жить в Сололаки", "tbilisi", ["old_tbilisi"]),
        ("вдвоём в Адлер в ноябре", "sochi", ["adler"]),
    ],
)
def test_rules_understand_city_country_and_district(nlu, text, loc, districts):
    res = nlu.parse(text, _state(), TODAY)
    got = {o.path: o.value for o in res.patch}
    assert got.get("location_id") == loc
    assert got.get("district_ids", []) == districts


def test_origin_city_is_not_destination(nlu):
    res = nlu.parse("летим из Тбилиси в Сочи вдвоём в ноябре", _state(), TODAY)
    got = {o.path: o.value for o in res.patch}
    assert got["location_id"] == "sochi" and got["transport.origin_city"] == "Тбилиси"


def test_old_town_means_old_town_of_current_city(nlu):
    res = nlu.parse("лучше поближе к старому городу", _state("tbilisi"), TODAY)
    got = {o.path: o.value for o in res.patch}
    assert "location_id" not in got and got["district_ids"] == ["old_tbilisi"]


def test_two_cities_mean_compare(nlu):
    res = nlu.parse("Сочи или Анталья в октябре?", _state(), TODAY)
    assert res.intent == Intent.inspire and res.candidate_location_ids == ["antalya", "sochi"]


def test_unknown_city_is_reported_not_substituted(nlu):
    res = nlu.parse("хотим в Батуми в сентябре", _state(), TODAY)
    assert res.unknown_destination == "Батуми"
    assert not any(o.path == "location_id" for o in res.patch)


# ------------------------------------------------------------------ подбор в других странах
def test_plan_abroad_uses_that_city_and_warns_about_documents(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "летим вдвоём из Москвы в Анталью с 10 по 17 ноября")
    (vb,) = _blocks(r, "variants")
    loc = agent.catalog.get_location("antalya")
    assert all(agent.catalog.get_hotel(v.hotel.id).location_id == "antalya" for v in vb.items)
    v = vb.items[0]
    # документы — одной строкой в ответе (не на каждой карточке), полностью — в корзине перед покупкой
    assert any("Турция" in b.markdown and "загранпаспорт" in b.markdown for b in _blocks(r, "text"))
    assert not {"abroad_documents", "abroad_money"} & {i.code for i in v.issues}
    r2 = _act(agent, sid, type="add_to_cart", variant_id=v.id)
    cart = _blocks(r2, "cart")[0].cart
    assert {"abroad_documents", "abroad_money"} <= {i.code for i in cart.issues}
    # билеты: вылет по Москве, прилёт по местному времени (Москва и Анталья — UTC+3)
    route = loc.routes["Москва"][0]
    assert v.outbound and v.outbound.hub_id == "AYT"
    assert v.outbound.arrive_at - v.outbound.depart_at == timedelta(minutes=v.outbound.duration_min)
    assert v.outbound.duration_min >= route.minutes


def test_ticket_times_respect_time_zones(app):
    svc = app.locations.get("phuket")
    req = Requirements(location_id="phuket")
    out = svc.schedule.offers("Москва", date(2027, 2, 1), "outbound", "plane", req)
    o = next(x for x in out if x.stops == 0)
    # Пхукет на 4 часа впереди Москвы: прилёт по местному = вылет + полёт + 4 ч
    assert o.arrive_at - o.depart_at == timedelta(minutes=o.duration_min, hours=4)


def test_switching_city_mid_dialog_resets_districts_and_cart(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём в Адлер с 10 по 17 ноября")
    st = agent.get_state(sid)
    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    r = _say(agent, sid, "а давай лучше в Дубай")
    st = agent.get_state(sid)
    assert st.requirements.location_id == "dubai" and st.requirements.district_ids == []
    assert st.cart_id is None, "корзину с сочинским отелем оформлять нельзя"
    diff = _blocks(r, "diff")[0]
    assert any("Дубай" in c for c in diff.requirement_changes)
    assert all(agent.catalog.get_hotel(v.hotel.id).location_id == "dubai" for v in st.variants)


def test_cold_sea_suggests_warmer_destinations(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "вдвоём на море с 5 по 12 декабря, хотим купаться")
    assert agent.get_state(sid).requirements.location_id == "sochi"
    warm = [b for b in _blocks(r, "destinations") if b.title == "Где море теплее"]
    assert warm, "в декабре море в Сочи холодное — надо показать, где теплее"
    assert all(c.scope == "location" and c.climate.sea_temp_c >= 22 and not c.cons for c in warm[0].items)


def test_where_to_go_compares_cities(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "куда поехать на море в январе, чтобы купаться? летим из Москвы вдвоём")
    (db,) = _blocks(r, "destinations")
    assert all(c.scope == "location" for c in db.items)
    top = db.items[0]
    assert top.climate.sea_temp_c >= 25 and top.flight_minutes
    # выбор карточки → подбор отелей в этом городе
    r = agent.turn(sid, TurnRequest(action=top.choose.action))
    assert _blocks(r, "variants") and agent.get_state(sid).requirements.location_id == top.location_id


def test_unknown_city_gets_honest_answer(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "хотим в Батуми в сентябре на неделю")
    assert "Батуми" in r.blocks[0].markdown and "нет в каталоге" in r.blocks[0].markdown
    assert _blocks(r, "destinations")


def test_city_must_be_confirmed_only_if_user_did_not_name_it(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")  # город не назван → Сочи по умолчанию
    assert [g for g, _ in agent._pending(agent.get_state(sid))] == ["location"]
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём в Сочи с 10 по 17 ноября у моря")
    assert agent._pending(agent.get_state(sid)) == []


def test_visa_free_limit_is_checked(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "Пхукет на два месяца с января, один")
    v = _blocks(r, "variants")[0].items[0]
    assert any(i.code == "visa_stay_limit" for i in v.issues)


def test_visa_question_is_answered_from_checked_data(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "в Тбилиси вдвоём на неделю в мае")
    r = _say(agent, sid, "нужна ли виза?")
    assert "365" in r.blocks[0].markdown and "страховка" in r.blocks[0].markdown


def test_smalltalk_does_not_crash(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "привет! что ты умеешь?")
    assert r.suggestions


def test_locations_endpoint(app):
    c = TestClient(create_app(Settings(agent_mode="rules"), app_ctx=app))
    locs = c.get("/api/v1/locations").json()
    assert {x["id"] for x in locs} == set(CITIES)
    assert {x["abroad"] for x in locs} == {True, False}
    h = c.get("/api/v1/health").json()
    assert h["hotels_loaded"] == sum(x["hotels"] for x in locs)
