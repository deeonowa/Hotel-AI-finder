"""Билеты в чате: откуда летим, какие рейсы, багаж, время вылета, выбор конкретного рейса."""

from __future__ import annotations

from datetime import date

from trip_agent.schemas import Requirements, TurnRequest


def _say(agent, sid, text):
    return agent.turn(sid, TurnRequest(text=text))


def _act(agent, sid, action):
    return agent.turn(sid, TurnRequest(action=action))


def _blocks(r, t):
    return [b for b in r.blocks if b.type == t]


def _start(agent, text="в Анталью вдвоём с 5 по 12 декабря"):
    sid = agent.new_session().session_id
    _say(agent, sid, text)
    st = agent.get_state(sid)
    return sid, _act(agent, sid, {"type": "select_variant", "variant_id": st.recommended_variant_id})


def test_selection_asks_where_from_to_add_tickets(agent):
    sid, r = _start(agent)
    (q,) = _blocks(r, "question")
    assert "Откуда летите" in q.text and "transport.origin_city" in q.paths
    moscow = next(x for x in q.quick_replies if "Москв" in x.label)
    r = _act(agent, sid, moscow.action)
    st = agent.get_state(sid)
    v = st.variant(st.selected_variant_id)
    assert st.stage.value == "selected", "выбранный отель сохранён"
    assert v.outbound and v.inbound and v.outbound.origin_city == "Москва"


def test_ticket_options_and_choosing_a_flight(agent):
    sid, _ = _start(agent, "летим вдвоём из Москвы в Анталью с 5 по 12 декабря")
    r = _say(agent, sid, "какие есть рейсы?")
    tables = _blocks(r, "comparison")
    assert len(tables) == 2 and len(tables[0].headers) >= 2
    labels = {row.label for row in tables[0].rows}
    assert {"Вылет", "Прилёт", "В пути", "Цена за всех"} <= labels
    text = r.blocks[0].markdown
    assert "багаж" in text and "местное" in text
    pick = next(s for s in r.suggestions if s.label.startswith("Туда"))
    chosen_id = pick.action.ops[0].value
    _act(agent, sid, pick.action)
    st = agent.get_state(sid)
    assert st.variant(st.selected_variant_id).outbound.id == chosen_id


def test_baggage_raises_ticket_price(agent, app):
    svc = app.locations.get("dubai")
    req = Requirements(location_id="dubai")
    req.transport.origin_city = "Москва"
    light = svc.schedule.offers("Москва", date(2027, 2, 1), "outbound", "plane", req)
    req.transport.baggage = True
    bags = svc.schedule.offers("Москва", date(2027, 2, 1), "outbound", "plane", req)
    for a, b in zip(light, bags, strict=True):
        assert b.baggage_included and b.passengers_price == a.passengers_price + a.baggage_fee_per_person * 2


def test_evening_departure_is_respected(agent):
    sid, _ = _start(agent, "летим вдвоём из Москвы в Анталью с 5 по 12 декабря")
    _say(agent, sid, "хочу вылет вечером")
    st = agent.get_state(sid)
    v = st.variant(st.selected_variant_id) or st.variants[0]
    assert 17 <= v.outbound.depart_at.hour < 23


def test_more_origin_cities(app):
    for lid in ("sochi", "antalya", "dubai", "phuket", "tbilisi"):
        assert {"Уфа", "Пермь", "Красноярск", "Владивосток"} <= set(app.catalog.get_location(lid).routes)
