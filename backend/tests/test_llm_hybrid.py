"""Гибрид LLM + правила: модель понимает, детерминированный код проверяет и решает.

Сеть не нужна: ScriptedLLM отвечает заготовками, а настоящий OpenAICompatibleClient
проверяется с подменённым транспортом (мусор вместо JSON, таймаут).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from trip_agent.agent.nlg import LlmNlg
from trip_agent.agent.nlu_llm import LlmNlu
from trip_agent.api.app import create_app
from trip_agent.config import Settings
from trip_agent.container import build
from trip_agent.schemas import Intent, TripState, TurnRequest

from .conftest import TODAY
from .test_llm import FakeLLM


class ScriptedLLM:
    """Ответ модели зависит от сообщения: {подстрока сообщения: NluResult-JSON}."""

    model = "scripted"

    def __init__(self, script: dict[str, dict], text: str = ""):
        self.script, self.text, self.calls = script, text, 0

    def complete_json(self, system, user, schema, *, temperature=0.0):
        self.calls += 1
        for key, result in self.script.items():
            if key in user:
                return schema.model_validate({"parser": "llm", "confidence": 0.9, **result})
        return schema.model_validate({"intent": "unclear", "confidence": 0.2, "parser": "llm"})

    def complete_text(self, system, user, *, temperature=0.3, max_tokens=600):
        if not self.text:
            raise RuntimeError("no text scripted")
        return self.text


def _state(**kw) -> TripState:
    return TripState(session_id="t", created_at=datetime.now(UTC), updated_at=datetime.now(UTC), **kw)


@pytest.fixture(scope="module")
def packs(app):
    return app.locations.packs()


def _nlu(llm, packs) -> LlmNlu:
    return LlmNlu(llm, "Большой Сочи", {}, locations=packs)


def _paths(res) -> dict:
    return {o.path: o.value for o in res.patch}


# ------------------------------------------------------------------ понимание


def test_free_phrasing_becomes_valid_patch(packs):
    llm = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [
                {"op": "set", "path": "location_id", "value": "antalya"},
                {"op": "set", "path": "guests.adults", "value": 3},
                {"op": "add", "path": "notes", "value": "без ночных перелётов"},
            ],
            "confidence": 0.9,
            "parser": "llm",
        }
    )
    nlu = _nlu(llm, packs)
    res = nlu.parse("В Турцию, но без ночных перелётов, нас трое", _state(), TODAY)
    assert res.parser == "llm" and res.intent == Intent.plan_trip
    assert _paths(res) == {"location_id": "antalya", "guests.adults": 3, "notes": "без ночных перелётов"}
    assert not nlu.guard_notes


def test_unknown_city_is_not_replaced_with_sochi(packs):
    """Модель «услужливо» подставила Сочи вместо Батуми — проверка правилами это ловит."""
    llm = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [{"op": "set", "path": "location_id", "value": "sochi"}, {"op": "set", "path": "dates.month", "value": 9}],
            "confidence": 0.9,
            "parser": "llm",
        }
    )
    nlu = _nlu(llm, packs)
    res = nlu.parse("хотим в Батуми в сентябре", _state(), TODAY)
    assert res.unknown_destination == "Батуми"
    assert "location_id" not in _paths(res) and _paths(res)["dates.month"] == 9
    assert any("Батуми" in n for n in nlu.guard_notes)


def test_city_not_named_in_message_is_dropped(packs):
    """«К тёплому морю» — город не назван: модель не выбирает направление за пользователя."""
    llm = FakeLLM(
        json_result={
            "intent": "inspire",
            "patch": [
                {"op": "set", "path": "location_id", "value": "dubai"},
                {"op": "set", "path": "dates.month", "value": 11},
                {"op": "set", "path": "guests.children_ages", "value": [5]},
                {"op": "add", "path": "preferences", "value": {"key": "budget_value", "priority": "important"}},
            ],
            "assumed_paths": ["guests.children_ages"],
            "confidence": 0.85,
            "parser": "llm",
        }
    )
    nlu = _nlu(llm, packs)
    res = nlu.parse("Хочу куда-то к тёплому морю в ноябре, с ребёнком, недорого", _state(), TODAY)
    paths = _paths(res)
    assert "location_id" not in paths and paths["dates.month"] == 11 and "preferences" in paths
    assert res.intent == Intent.inspire


def test_city_outside_catalog_never_passes(packs):
    llm = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [{"op": "set", "path": "location_id", "value": "istanbul"}],
            "candidate_location_ids": ["istanbul", "antalya"],
            "confidence": 0.9,
            "parser": "llm",
        }
    )
    res = _nlu(llm, packs).parse("в Турцию", _state(), TODAY)
    assert "location_id" not in _paths(res)
    assert res.candidate_location_ids == ["antalya"]


def test_bad_values_dropped_good_kept(packs):
    llm = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [
                {"op": "set", "path": "dates.check_in", "value": "2025-01-10"},  # прошлое
                {"op": "set", "path": "dates.check_out", "value": "2025-01-15"},
                {"op": "set", "path": "guests.children_ages", "value": [40]},
                {"op": "add", "path": "guests.pets", "value": "dragon"},
                {"op": "shift_days", "path": "dates", "value": 2},  # нет дат — нечего сдвигать
                {"op": "set", "path": "guests.adults", "value": 2},
            ],
            "confidence": 0.9,
            "parser": "llm",
        }
    )
    nlu = _nlu(llm, packs)
    res = nlu.parse("вдвоём", _state(), TODAY)
    assert _paths(res) == {"guests.adults": 2}
    assert len(nlu.guard_notes) >= 4


def test_low_confidence_falls_back_to_rules(packs):
    llm = FakeLLM(json_result={"intent": "plan_trip", "patch": [{"op": "set", "path": "guests.adults", "value": 7}], "confidence": 0.2})
    nlu = _nlu(llm, packs)
    res = nlu.parse("вдвоём в ноябре на неделю", _state(), TODAY)
    assert res.parser == "rules" and _paths(res)["guests.adults"] == 2
    assert "не уверена" in nlu.guard_notes[0]


def test_relative_change_from_llm_is_applied(packs, app):
    """«Можно на два дня позже и с собакой?» — операции применимы к текущему плану."""
    st = _state()
    st.requirements = st.requirements.model_copy(
        update={"dates": st.requirements.dates.model_copy(update={"check_in": TODAY.replace(day=30), "check_out": TODAY.replace(day=31)})}
    )
    llm = FakeLLM(
        json_result={
            "intent": "modify",
            "patch": [{"op": "shift_days", "path": "dates", "value": 2}, {"op": "add", "path": "guests.pets", "value": "dog"}],
            "confidence": 0.9,
        }
    )
    res = _nlu(llm, packs).parse("Можно на два дня позже и с собакой?", st, TODAY)
    assert [o.op for o in res.patch] == ["shift_days", "add"]


# ------------------------------------------------------------------ отказ модели


class _Garbage:
    """Подмена openai-транспорта: модель отвечает не-JSON или падает по таймауту."""

    def __init__(self, reply: str | None):
        self.reply, self.calls = reply, 0
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls += 1
        if self.reply is None:
            raise TimeoutError("read timeout")
        msg = type("M", (), {"content": self.reply})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


def _real_client(reply: str | None):
    from trip_agent.llm.client import OpenAICompatibleClient

    client = OpenAICompatibleClient("http://127.0.0.1:9/v1", "test", "m", timeout_s=1, cooldown_s=60)
    client._client = _Garbage(reply)
    return client


def test_invalid_json_falls_back_to_rules(packs):
    llm = _real_client("Конечно! Вот ваш отдых: пляж, солнце 🙂")
    nlu = _nlu(llm, packs)
    res = nlu.parse("вдвоём в ноябре на неделю", _state(), TODAY)
    assert res.parser == "rules" and res.intent == Intent.plan_trip
    assert "invalid structured output" in (nlu.last_error or "")
    assert llm._client.calls == 2, "один повтор с текстом ошибки, не больше"


def test_timeout_falls_back_and_cools_down(packs):
    llm = _real_client(None)
    nlu = _nlu(llm, packs)
    assert nlu.parse("вдвоём в ноябре", _state(), TODAY).parser == "rules"
    assert nlu.parse("втроём в декабре", _state(), TODAY).parser == "rules"
    assert llm._client.calls == 1, "после сбоя провайдер не дёргаем — демо не ждёт таймаут на каждом ходе"
    assert not llm.available


def test_failing_llm_keeps_whole_flow_working():
    ctx = build(Settings(agent_mode="rules"), today=lambda: TODAY, database_url="memory://", llm=FakeLLM(fail=True))
    assert ctx.mode == "llm"
    sid = ctx.agent.new_session().session_id
    r = ctx.agent.turn(sid, TurnRequest(text="Сочи с 10 по 15 ноября вдвоём у моря"))
    assert r.mode == "rules" and any(t.name == "nlu_fallback" for t in r.trace)
    assert any(b.type == "variants" for b in r.blocks)


# ------------------------------------------------------------------ границы ответственности


def test_llm_cannot_create_order():
    llm = ScriptedLLM({"оформляй": {"intent": "book"}})
    ctx = build(Settings(agent_mode="rules"), today=lambda: TODAY, database_url="memory://", llm=llm)
    agent = ctx.agent
    sid = agent.new_session().session_id
    ops = [
        {"op": "set", "path": "location_id", "value": "sochi"},
        {"op": "set", "path": "dates.check_in", "value": "2026-11-10"},
        {"op": "set", "path": "dates.check_out", "value": "2026-11-15"},
        {"op": "set", "path": "guests.adults", "value": 2},
    ]
    r = agent.turn(sid, TurnRequest(action={"type": "apply_patch", "ops": ops}))
    vid = next(b for b in r.blocks if b.type == "variants").recommended_id
    r = agent.turn(sid, TurnRequest(action={"type": "add_to_cart", "variant_id": vid}))
    assert r.stage.value == "in_cart"

    r = agent.turn(sid, TurnRequest(text="всё, оформляй"))
    assert agent.get_state(sid).order_ids == [], "заказ создаётся только кнопкой, не текстом через LLM"
    assert r.stage.value == "in_cart" and r.suggestions[0].action.type == "checkout"


def test_health_reports_llm_without_secrets():
    settings = Settings(agent_mode="auto", llm_model="qwen-test", llm_api_key="sk-very-secret")
    ctx = build(settings, today=lambda: TODAY, database_url="memory://", llm=FakeLLM())
    body = TestClient(create_app(settings, app_ctx=ctx)).get("/api/v1/health").text
    assert '"llm_configured":true' in body and "sk-very-secret" not in body


def test_no_keys_means_rules_mode():
    settings = Settings(_env_file=None, agent_mode="auto", llm_model=None, llm_base_url=None, llm_api_key=None)
    ctx = build(settings, today=lambda: TODAY, database_url="memory://")
    assert ctx.mode == "rules" and not settings.llm_enabled


# ------------------------------------------------------------------ объяснения


def test_nlg_rejects_invented_hotel_date_and_promise():
    st = _state()
    facts = {"kind": "replanned", "summary": "Пересобрал план. Рекомендация: «Бриз», на 1 880 ₽ дороже."}
    good = "Готово! Теперь лучше всего «Бриз» — он дороже на 1 880 ₽."
    assert LlmNlg(FakeLLM(text=good)).summarize_turn(st, facts).startswith(good)
    for lie in (
        "Рекомендую «Ривьеру» — она дороже на 1 880 ₽.",  # отеля нет в фактах
        "«Бриз» дороже на 1 880 ₽, зато свободен в мае.",  # дата из воздуха
        "«Бриз» дороже на 1 880 ₽, но с бесплатной отменой.",  # обещание, которого нет в тарифе
    ):
        assert LlmNlg(FakeLLM(text=lie)).summarize_turn(st, facts) == facts["summary"], lie


def test_answer_does_not_promise_check_in():
    ctx = {"hotel": {"name": "Бриз", "check_in_time": "14:00", "check_out_time": "12:00", "amenities": []}}
    q = "Прилетаем в 7:30, успеем заселиться?"
    promise = LlmNlg(FakeLLM(text="Да, успеете — заселим сразу!")).answer_question(q, _state(), ctx)
    honest = "Заселение с 14:00, так что в 7:30 номер может быть ещё не готов — можно оформить ранний заезд."
    assert promise == "Заселение с 14:00, выезд до 12:00."
    assert LlmNlg(FakeLLM(text=honest)).answer_question(q, _state(), ctx) == honest


def test_arrival_question_is_answered_by_validator(agent):
    """«Прилетаем в 7:30, успеем заселиться?» — ответ считает валидатор, а не модель."""
    sid = agent.new_session().session_id
    agent.turn(sid, TurnRequest(text="Сочи с 10 по 15 ноября вдвоём у моря"))
    r = agent.turn(sid, TurnRequest(text="Прилетаем в 7:30, успеем заселиться?"))
    texts = " ".join(b.markdown for b in r.blocks if b.type == "text")
    assert "заселение с" in texts.lower() or "успеваете" in texts
