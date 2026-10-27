"""NLU через LLM с автоматическим откатом на правила. Владелец: ML1.

Поток: промпт (контекст + белый список путей) → LLM → JSON → Pydantic(NluResult)
→ санитайзер (пути/ключи из белого списка, значения в разумных пределах) → результат.
Любая ошибка → RuleNlu. Агент никогда не падает из-за модели.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from ..ports import LLMClient
from ..schemas import Intent, LocationPack, NluResult, PatchOp, PrefKey, TripState
from .nlu_rules import RuleNlu, is_injection

log = logging.getLogger(__name__)
PROMPT = (Path(__file__).parent / "prompts" / "nlu_system.md").read_text(encoding="utf-8")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


class LlmNlu:
    def __init__(
        self,
        llm: LLMClient,
        location_name: str,
        districts: dict[str, str],
        fallback: RuleNlu | None = None,
        *,
        locations: Sequence[LocationPack] | None = None,
    ):
        """locations — все доступные направления: модель выбирает location_id и районы только из них."""
        self.llm = llm
        self.location_name = location_name
        self.locations = {lp.id: lp for lp in locations or []}
        # район → его локация (для sochi-only режима — без локации)
        self.district_loc: dict[str, str | None] = {d: None for d in districts}
        for lp in self.locations.values():
            self.district_loc.update({d.id: lp.id for d in lp.districts})
        self.districts = {**districts, **{d.id: d.name for lp in self.locations.values() for d in lp.districts}}
        self.fallback = fallback or RuleNlu(list(self.locations.values()) or None)
        self.last_error: str | None = None

    def _catalog(self) -> str:
        if not self.locations:
            return json.dumps({"default": {"name": self.location_name, "districts": self.districts}}, ensure_ascii=False)
        return json.dumps(
            {
                lp.id: {"город": lp.name, "страна": lp.country, "районы": {d.id: d.name for d in lp.districts}}
                for lp in self.locations.values()
            },
            ensure_ascii=False,
        )

    def _system(self, state: TripState, today: date) -> str:
        req = state.requirements.model_dump(mode="json", exclude={"provenance"}, exclude_defaults=True)
        variants = [{"n": i + 1, "id": v.id, "hotel": v.hotel.name, "title": v.title} for i, v in enumerate(state.variants)]
        return PROMPT.format(
            today=today.isoformat(),
            weekday=WEEKDAYS[today.weekday()],
            location_name=self.location_name,
            locations=self._catalog(),
            current_location=state.requirements.location_id,
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
            res = self._sanitize(res, state)
            res.parser = "llm"
            self.last_error = None
            return res
        except Exception as e:  # LLMError, ValidationError, что угодно
            self.last_error = str(e)[:300]
            log.warning("LLM NLU failed, fallback to rules: %s", self.last_error)
            return self.fallback.parse(text, state, today)

    def _sanitize(self, res: NluResult, state: TripState | None = None) -> NluResult:
        """Последний рубеж: даже валидный по схеме JSON может нести бессмыслицу."""
        clean = []
        loc_ops = [o for o in res.patch if o.path == "location_id" and o.op == "set" and (not self.locations or o.value in self.locations)]
        target = loc_ops[-1].value if loc_ops else (state.requirements.location_id if state else None)
        for op in res.patch:
            if op.path == "location_id" and (op.op != "set" or (self.locations and op.value not in self.locations)):
                continue  # выдуманный город — не применяем
            if op.path == "district_ids" and op.op == "set":
                ids = [d for d in (op.value or []) if d in self.districts]
                owners = {self.district_loc.get(d) for d in ids} - {None}
                if self.locations and len(owners) == 1 and not loc_ops and owners != {target}:
                    # «в Патонг» без названия страны → направление следует из района
                    (owner,) = owners
                    clean.append(PatchOp(op="set", path="location_id", value=owner))
                    target = owner
                ids = [d for d in ids if self.district_loc.get(d) in (None, target)]
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
        if self.locations:
            res.candidate_location_ids = [x for x in res.candidate_location_ids if x in self.locations]
        return res
