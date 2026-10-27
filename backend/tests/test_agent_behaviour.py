"""Поведение агента по итогам ревью: подтверждение перед покупкой, пересборка без лишней замены,
автономное исправление проблем, регрессии NLU."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from trip_agent.agent.nlu_rules import RuleNlu
from trip_agent.booking.service import BookingError
from trip_agent.schemas import TripState, TurnRequest
from trip_agent.validation.rules import pending_confirmations

from .conftest import TODAY


def _types(r) -> list[str]:
    return [b.type for b in r.blocks]


def _block(r, t):
    return next(b for b in r.blocks if b.type == t)


def _text(r) -> str:
    return " ".join(b.markdown for b in r.blocks if b.type == "text")


def _act(agent, sid, **a):
    return agent.turn(sid, TurnRequest(action=a))


def _say(agent, sid, text):
    return agent.turn(sid, TurnRequest(text=text))


# ------------------------------------------------------------------ P0: подтверждение
def test_assumed_dates_and_guests_must_be_confirmed_before_order(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "к морю с ребёнком в ноябре на неделю")  # даты и состав — предположены
    st = agent.get_state(sid)
    groups = {g for g, _ in pending_confirmations(st.requirements)}
    assert groups == {"dates", "guests"}

    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    r = _act(agent, sid, type="checkout")
    assert "order" not in _types(r), "заказ с неподтверждёнными предположениями создаваться не должен"
    q = _block(r, "question")
    assert "предположил" in q.text and "дети" in q.text

    # город пользователь тоже не называл (направлений несколько) — его подтверждают вместе с датами и составом
    assert "направление" in q.text
    r = _act(agent, sid, type="confirm_assumptions", paths=["location", "dates", "guests"], then_checkout=True)
    assert _block(r, "order").order.number.startswith("OST-")


def test_booking_service_itself_refuses_unconfirmed(agent):
    """Защита не только в диалоге: сервис бронирования перепроверяет корзину сам."""
    sid = agent.new_session().session_id
    _say(agent, sid, "к морю с ребёнком в ноябре на неделю")
    st = agent.get_state(sid)
    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    st = agent.get_state(sid)
    cart = agent.booking.repo.get_cart(st.cart_id)
    with pytest.raises(BookingError):
        agent.booking.checkout(cart, None, variant=st.variant(st.selected_variant_id), req=st.requirements)


def test_explicit_dates_and_guests_need_no_confirmation(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")
    assert pending_confirmations(agent.get_state(sid).requirements) == []


# ------------------------------------------------------------------ P1: пересборка
def test_replan_keeps_chosen_hotel_and_updates_cart(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")
    st = agent.get_state(sid)
    chosen = st.variants[1]  # пользователь выбрал НЕ рекомендованный
    _act(agent, sid, type="add_to_cart", variant_id=chosen.id)

    r = _say(agent, sid, "а если на два дня позже?")
    st = agent.get_state(sid)
    assert r.stage.value == "in_cart", "корзина не должна сбрасываться"
    assert st.variant(st.selected_variant_id).hotel.id == chosen.hotel.id, "выбранный отель сохранён"
    cart = _block(r, "cart").cart
    assert any(i.kind == "hotel_stay" and "12.11–19.11" in i.details for i in cart.items), "корзина пересчитана на новые даты"
    assert any("сохранён" in k for k in _block(r, "diff").kept)


def test_replan_explains_why_chosen_hotel_was_replaced(agent, hotels):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")
    st = agent.get_state(sid)
    by_id = {h.id: h for h in hotels}
    no_pets = next((v for v in st.variants if by_id[v.hotel.id].pets.allowed is not True), None)
    if no_pets is None:
        pytest.skip("в выдаче нет отеля без животных")
    _act(agent, sid, type="add_to_cart", variant_id=no_pets.id)
    r = _say(agent, sid, "с нами поедет собака")
    assert "пришлось заменить" in _block(r, "diff").summary
    assert agent.get_state(sid).cart_id is None, "корзина под старый отель очищена"


def test_reselecting_the_same_hotel_is_idempotent_and_keeps_next_step(agent):
    """Старая карточка не должна снова запускать выбор и плодить одинаковые ответы."""
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")
    chosen = agent.get_state(sid).recommended_variant_id
    _act(agent, sid, type="select_variant", variant_id=chosen)

    r = _act(agent, sid, type="select_variant", variant_id=chosen)
    assert _types(r) == ["text"]
    assert "уже выбран" in _text(r)
    assert any(s.label == "В корзину" for s in r.suggestions)
    assert agent.get_state(sid).selected_variant_id == chosen


def test_question_after_selection_does_not_restart_selection(agent):
    """Вопрос между шагами — нормальная часть диалога, а не повторный выбор отеля."""
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября у моря")
    chosen = agent.get_state(sid).recommended_variant_id
    _act(agent, sid, type="select_variant", variant_id=chosen)

    r = _say(agent, sid, "есть там бассейн?")
    assert "variants" not in _types(r)
    assert agent.get_state(sid).selected_variant_id == chosen


# ------------------------------------------------------------------ P1: автоисправление
def test_agent_fixes_long_night_transfer_on_its_own(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября, Лазаревское")
    st = agent.get_state(sid)
    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    r = _say(agent, sid, "прилетаем в 23:50")

    trace = {t.name: t.detail for t in r.trace}
    assert "long_night_transfer" in trace["autofix"]
    alt = next(b for b in r.blocks if b.type == "variants").items[0]
    assert alt.title == "Исправленный вариант"
    assert alt.transfer_in.minutes <= 35
    labels = [s.label for s in r.suggestions]
    assert any(x.startswith("Заменить") for x in labels) and any(x.startswith("Оставить") for x in labels)

    # отказ от исправления запоминается — агент не навязывает его снова
    keep = next(s for s in r.suggestions if s.label.startswith("Оставить"))
    _act(agent, sid, **keep.action.model_dump())
    r = _say(agent, sid, "прилетаем в 23:55")
    assert "variants" not in _types(r)


def test_accepting_fix_replaces_cart(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "вдвоём с 10 по 17 ноября, Лазаревское")
    st = agent.get_state(sid)
    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    r = _say(agent, sid, "прилетаем в 23:50")
    take = next(s for s in r.suggestions if s.label.startswith("Заменить"))
    r = _act(agent, sid, **take.action.model_dump())
    st = agent.get_state(sid)
    assert r.stage.value == "in_cart"
    assert st.variant(st.selected_variant_id).transfer_in.minutes <= 35


# ------------------------------------------------------------------ регрессии NLU
@pytest.mark.parametrize(
    ("text", "ages"),
    [
        ("к морю с ребёнком в ноябре", [7]),  # был баг: «с» принималось за число детей → ребёнок терялся
        ("с детьми в декабре", [7, 7]),
        ("двое детей и мы с женой", [7, 7]),
        ("едем с малышом", [1]),
        ("сын 5 лет и дочь 9 лет", [5, 9]),
    ],
)
def test_children_parsing(text, ages):
    st = TripState(session_id="t", created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    res = RuleNlu().parse(text, st, TODAY)
    got = next((o.value for o in res.patch if o.path == "guests.children_ages"), None)
    assert got == ages


# ------------------------------------------------------------------ P2: билеты
def test_tickets_included_when_origin_known(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "летим вдвоём из Москвы, с 10 по 17 ноября, у моря")
    v = _block(r, "variants").items[0]
    assert v.outbound and v.inbound and v.outbound.is_demo
    assert v.outbound.arrive_at.date().isoformat() == "2026-11-10"
    assert v.total_price >= v.offer.price_total + v.outbound.passengers_price + v.inbound.passengers_price
    assert not any(i.code == "arrival_unknown" for i in v.issues), "время прилёта известно из билета"
    _act(agent, sid, type="add_to_cart", variant_id=v.id)
    cart = agent.booking.repo.get_cart(agent.get_state(sid).cart_id)
    assert sum(1 for i in cart.items if i.kind == "ticket") == 2


def test_direct_only_finds_nearest_date_with_direct_flights(agent):
    sid = agent.new_session().session_id
    _say(agent, sid, "летим вдвоём из Екатеринбурга, с 10 по 17 ноября, у моря")
    st = agent.get_state(sid)
    _act(agent, sid, type="add_to_cart", variant_id=st.recommended_variant_id)
    r = _say(agent, sid, "хочу перелёт без пересадок")
    st = agent.get_state(sid)
    if st.variants and st.variant(st.selected_variant_id or "") and st.variant(st.selected_variant_id).outbound.stops == 0:
        return  # в эту дату прямой рейс нашёлся сразу — тоже корректно
    assert r.stage.value == "in_cart", "выбор пользователя не теряется, пока решаем вопрос с билетами"
    first = r.suggestions[0]
    assert "прямые" in first.label
    r = _act(agent, sid, **first.action.model_dump(mode="json"))
    st = agent.get_state(sid)
    v = st.variant(st.selected_variant_id)
    assert v.outbound.stops == 0 and v.inbound.stops == 0


def test_own_arrival_time_means_no_tickets(agent):
    sid = agent.new_session().session_id
    r = _say(agent, sid, "из Москвы, прилетаем в 9:30, вдвоём 10-17 ноября")
    v = _block(r, "variants").items[0]
    assert v.outbound is None, "пользователь назвал свой рейс — билеты не навязываем"
