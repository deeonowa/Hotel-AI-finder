"""NLU через LLM с автоматическим откатом на правила. Владелец: ML1.

Поток: промпт (контекст + белый список путей) → LLM → JSON → Pydantic(NluResult)
→ санитайзер (пути/ключи из белого списка, значения в разумных пределах) → результат.
Любая ошибка → RuleNlu. Агент никогда не падает из-за модели.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..ports import LLMClient
from ..schemas import Intent, NluResult, PrefKey, TripState
from .nlu_rules import RuleNlu, is_injection

log = logging.getLogger(__name__)
PROMPT = (Path(__file__).parent / "prompts" / "nlu_system.md").read_text(encoding="utf-8")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


class LlmNlu:
    def __init__(self, llm: LLMClient, location_name: str, districts: dict[str, str], fallback: RuleNlu | None = None):
        self.llm = llm
        self.location_name = location_name
        self.districts = districts
        self.fallback = fallback or RuleNlu()
        self.last_error: str | None = None

    def _system(self, state: TripState, today: date) -> str:
        req = state.requirements.model_dump(mode="json", exclude={"provenance"}, exclude_defaults=True)
        variants = [{"n": i + 1, "id": v.id, "hotel": v.hotel.name, "title": v.title} for i, v in enumerate(state.variants)]
        return PROMPT.format(
            today=today.isoformat(),
            weekday=WEEKDAYS[today.weekday()],
            location_name=self.location_name,
            districts=json.dumps(self.districts, ensure_ascii=False),
            stage=state.stage.value,
            requirements=json.dumps(req, ensure_ascii=False),
            variants=json.dumps(variants, ensure_ascii=False),
            pref_keys=", ".join(k.value for k in PrefKey),
        )

    def parse(self, text: str, state: TripState, today: date) -> NluResult:
        if is_injection(text):
            # defense-in-depth: явную попытку управлять агентом не отправляем в модель вовсе
            self.last_error = None
            return NluResult(intent=Intent.off_topic, confidence=0.95, parser="rules")
        try:
            res = self.llm.complete_json(self._system(state, today), text, NluResult)
            res = self._sanitize(res)
            res.parser = "llm"
            self.last_error = None
            return res
        except Exception as e:  # LLMError, ValidationError, что угодно
            self.last_error = str(e)[:300]
            log.warning("LLM NLU failed, fallback to rules: %s", self.last_error)
            return self.fallback.parse(text, state, today)

    def _sanitize(self, res: NluResult) -> NluResult:
        """Последний рубеж: даже валидный по схеме JSON может нести бессмыслицу."""
        clean = []
        for op in res.patch:
            if op.path == "district_ids" and op.op == "set":
                ids = [d for d in (op.value or []) if d in self.districts]
                if not ids:
                    continue
                op.value = ids
            if op.path == "budget.amount" and op.op == "set" and not (isinstance(op.value, int | float) and 500 <= op.value <= 10_000_000):
                continue
            if op.path == "guests.adults" and op.op == "set" and not (isinstance(op.value, int) and 1 <= op.value <= 10):
                continue
            if op.path == "guests.rooms" and op.op == "set" and not (isinstance(op.value, int) and 1 <= op.value <= 5):
                continue
            clean.append(op)
        res.patch = clean
        return res
