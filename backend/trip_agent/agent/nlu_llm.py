"""Гибридный NLU: LLM понимает, правила проверяют. Владелец: ML1.

Поток: промпт (контекст + белый список путей) → LLM → JSON → Pydantic(NluResult)
→ санитайзер (пути/ключи из белого списка, значения в разумных пределах)
→ сверка с правилами (город назван в тексте? направление вне каталога?)
→ пробное применение операций к текущим требованиям (apply_patch) → результат.

Любая ошибка модели (сеть, таймаут, невалидный JSON) или низкая уверенность → RuleNlu.
Модель меняет только Requirements через PatchOp: цены, варианты, корзину и заказ она не трогает.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from ..ports import LLMClient
from ..requirements_ops import PatchError, apply_patch
from ..schemas import Intent, LocationPack, NluResult, PatchOp, PetKind, PrefKey, TripState
from .nlu_rules import RuleNlu, _norm, is_injection

log = logging.getLogger(__name__)
PROMPT = (Path(__file__).parent / "prompts" / "nlu_system.md").read_text(encoding="utf-8")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
PLANNING = (Intent.plan_trip, Intent.modify, Intent.inspire)


class LlmNlu:
    def __init__(
        self,
        llm: LLMClient,
        location_name: str,
        districts: dict[str, str],
        fallback: RuleNlu | None = None,
        *,
        locations: Sequence[LocationPack] | None = None,
        min_confidence: float = 0.5,
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
        self.min_confidence = min_confidence
        self.last_error: str | None = None
        self.guard_notes: list[str] = []  # что отбросила проверка — уходит в trace хода

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
        self.last_error, self.guard_notes = None, []
        if is_injection(text):
            # defense-in-depth: явную попытку управлять агентом не отправляем в модель вовсе
            return NluResult(intent=Intent.off_topic, confidence=0.95, parser="rules")
        rules = self.fallback.parse(text, state, today)  # детерминированное «второе мнение», без сети
        try:
            res = self.llm.complete_json(self._system(state, today), text, NluResult)
        except Exception as e:  # LLMError, ValidationError, таймаут — что угодно
            self.last_error = str(e)[:300]
            log.warning("LLM NLU failed, fallback to rules: %s", self.last_error)
            return rules
        if res.confidence < self.min_confidence:
            self.guard_notes.append(f"модель не уверена ({res.confidence:.2f}) → правила")
            return rules
        res = self._sanitize(res, state, text=text, today=today, rules=rules)
        res.parser = "llm"
        return res

    def _sanitize(
        self,
        res: NluResult,
        state: TripState | None = None,
        *,
        text: str = "",
        today: date | None = None,
        rules: NluResult | None = None,
    ) -> NluResult:
        """Последний рубеж: даже валидный по схеме JSON может нести бессмыслицу или выдумку."""
        drop = self.guard_notes.append
        mentioned = self.fallback._destinations(_norm(text))[0] if text else None
        unknown = res.unknown_destination or (rules.unknown_destination if rules else None)
        if unknown and not mentioned:
            # «в Батуми»: направления нет в каталоге — не подменяем его Сочи или чем-то похожим
            res.unknown_destination = unknown
            if any(o.path in ("location_id", "district_ids") for o in res.patch):
                drop(f"«{unknown}» нет в каталоге — направление от модели не применяю")
            res.patch = [o for o in res.patch if o.path not in ("location_id", "district_ids")]
            if res.intent not in PLANNING:
                res.intent = rules.intent if rules and rules.intent in PLANNING else Intent.plan_trip
        elif res.unknown_destination and mentioned:
            res.unknown_destination = None  # город из каталога назван явно — это не «неизвестное направление»

        current = state.requirements.location_id if state else None
        chosen_before = bool(state and "location_id" in state.requirements.provenance)
        grounded = []
        for op in res.patch:
            if op.path != "location_id":
                grounded.append(op)
            elif op.op != "set" or (self.locations and op.value not in self.locations):
                drop(f"направление {op.value!r} не из каталога")
            elif mentioned is not None and op.value not in mentioned and not (op.value == current and chosen_before):
                drop(f"направление {op.value!r} не названо в сообщении")
            else:
                grounded.append(op)

        clean = []
        loc_ops = [o for o in grounded if o.path == "location_id"]
        target = loc_ops[-1].value if loc_ops else current
        for op in grounded:
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
            if not _value_ok(op, today):
                drop(f"{op.path}={op.value!r} вне допустимых значений")
                continue
            clean.append(op)
        res.patch = self._applicable(clean, state, today) if state else clean
        if self.locations:
            res.candidate_location_ids = [x for x in res.candidate_location_ids if x in self.locations]
        return res

    def _applicable(self, ops: list[PatchOp], state: TripState, today: date | None) -> list[PatchOp]:
        """Пробно применить к текущим требованиям: битые операции выкидываем, остальное оставляем.

        Операции с датами проверяем вместе — заезд и выезд меняются парой."""
        try:
            apply_patch(state.requirements, ops, today=today)
            return ops
        except PatchError:
            pass
        dates = [o for o in ops if o.path.startswith("dates")]
        groups = [[o] for o in ops if not o.path.startswith("dates")] + ([dates] if dates else [])
        kept: list[PatchOp] = []
        for g in groups:
            try:
                apply_patch(state.requirements, kept + g, today=today)
                kept += g
            except PatchError as e:
                self.guard_notes.append(f"не применить {', '.join(o.path for o in g)}: {str(e)[:80]}")
        return [o for o in ops if any(o is k for k in kept)]  # исходный порядок


def _value_ok(op: PatchOp, today: date | None) -> bool:
    """Границы значений, которые схема Requirements не выражает (или выражает слишком мягко)."""
    v, path = op.value, op.path
    if path == "guests.pets":
        return op.op == "remove" or v in {p.value for p in PetKind}
    if path == "notes":
        return op.op == "remove" or (isinstance(v, str) and 0 < len(v) <= 200)
    if op.op != "set":
        return True
    if path == "budget.amount":
        return isinstance(v, int | float) and 500 <= v <= 10_000_000
    if path == "guests.adults":
        return isinstance(v, int) and 1 <= v <= 10
    if path == "guests.rooms":
        return isinstance(v, int) and 1 <= v <= 5
    if path == "guests.children_ages":
        return isinstance(v, list) and len(v) <= 8 and all(isinstance(a, int) and 0 <= a <= 17 for a in v)
    if path in ("dates.check_in", "dates.check_out"):
        try:
            d = date.fromisoformat(v) if isinstance(v, str) else v
        except ValueError:
            return False
        return isinstance(d, date) and (today is None or d >= today)  # прошедшая дата — выдумка или опечатка модели
    if path == "transport.origin_city":
        return v is None or (isinstance(v, str) and 0 < len(v) <= 60)
    return True
