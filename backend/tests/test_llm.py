"""LLM-слой без сети: поддельная модель проверяет откат на правила, санитайзер и проверку чисел."""

from __future__ import annotations

from datetime import UTC, datetime

from trip_agent.agent.nlg import LlmNlg
from trip_agent.agent.nlu_llm import LlmNlu
from trip_agent.schemas import Intent, NluResult, PatchOp, TripState

from .conftest import TODAY


class FakeLLM:
    model = "fake"

    def __init__(self, json_result=None, text="", fail=False):
        self.json_result, self.text, self.fail = json_result, text, fail
        self.prompts: list[str] = []

    def complete_json(self, system, user, schema, *, temperature=0.0):
        self.prompts.append(system)
        if self.fail:
            raise RuntimeError("provider down")
        return schema.model_validate(self.json_result)

    def complete_text(self, system, user, *, temperature=0.3, max_tokens=600):
        if self.fail:
            raise RuntimeError("provider down")
        return self.text


def _state() -> TripState:
    return TripState(session_id="t", created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


def test_llm_nlu_uses_model_and_sanitizes():
    fake = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [
                {"op": "set", "path": "budget.amount", "value": 1},  # абсурд → выкинуть
                {"op": "set", "path": "district_ids", "value": ["mars", "adler"]},  # чужой район → отфильтровать
                {"op": "set", "path": "guests.adults", "value": 2},
            ],
            "parser": "llm",
        }
    )
    nlu = LlmNlu(fake, "Большой Сочи", {"adler": "Адлер"})
    res = nlu.parse("вдвоём в Адлер", _state(), TODAY)
    assert res.parser == "llm" and res.intent == Intent.plan_trip
    paths = {o.path: o.value for o in res.patch}
    assert "budget.amount" not in paths
    assert paths["district_ids"] == ["adler"]
    assert "Сегодня: 2026-10-26" in fake.prompts[0], "в промпт подставляется дата для относительных дат"


def test_llm_nlu_falls_back_to_rules_on_failure():
    nlu = LlmNlu(FakeLLM(fail=True), "Большой Сочи", {"adler": "Адлер"})
    res = nlu.parse("вдвоём в ноябре на неделю", _state(), TODAY)
    assert res.parser == "rules" and res.intent == Intent.plan_trip
    assert nlu.last_error


def test_nlg_rejects_invented_numbers(agent):
    st = _state()
    facts = {"kind": "replanned", "summary": "Пересобрал план. Рекомендация: «Бриз», на 1 880 ₽ дороже."}
    honest = LlmNlg(FakeLLM(text="Готово: «Бриз» стал дороже на 1 880 ₽."))
    liar = LlmNlg(FakeLLM(text="Готово: «Бриз» теперь стоит 999 ₽!"))
    assert "1 880" in honest.summarize_turn(st, facts)
    assert "999" not in liar.summarize_turn(st, facts), "выдуманная цена должна отбрасываться"


def test_patch_from_llm_is_whitelisted():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NluResult.model_validate({"intent": "modify", "patch": [{"op": "set", "path": "offer.price_total", "value": 1}]})
    assert PatchOp(op="set", path="guests.adults", value=2)


def test_llm_nlu_cities_only_from_catalog(app):
    """Модель не может отправить в выдуманный город; район другой страны переключает направление."""
    packs = app.locations.packs()
    fake = FakeLLM(
        json_result={
            "intent": "plan_trip",
            "patch": [
                {"op": "set", "path": "location_id", "value": "batumi"},  # нет в каталоге → выкинуть
                {"op": "set", "path": "district_ids", "value": ["patong"]},  # район Пхукета → и город тоже
            ],
            "candidate_location_ids": ["sochi", "atlantis"],
            "parser": "llm",
        }
    )
    nlu = LlmNlu(fake, "Большой Сочи", {}, locations=packs)
    res = nlu.parse("в Патонг", _state(), TODAY)
    paths = {o.path: o.value for o in res.patch}
    assert paths["location_id"] == "phuket" and paths["district_ids"] == ["patong"]
    assert res.candidate_location_ids == ["sochi"]
    assert '"dubai"' in fake.prompts[0] and "Дубай" in fake.prompts[0], "в промпте весь каталог направлений"
