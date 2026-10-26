"""Политика диалога: что спросить, что предположить, какие «скрытые» требования вывести. Владелец: ML1.

Принцип: спрашиваем ТОЛЬКО то, без чего поиск бессмысленен (когда ехать). Всё остальное
предполагаем и показываем чипсами с источником «предположил» — пользователь правит в один клик.
Лимит: не больше MAX_QUESTIONS уточнений за сессию до первых вариантов.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..requirements_ops import PREF_LABELS, fmt_rub
from ..schemas import (
    ApplyPatchAction,
    AssumptionChip,
    PatchOp,
    Preference,
    PrefKey,
    Priority,
    QuestionBlock,
    QuickReply,
    Requirements,
    Source,
)

MAX_QUESTIONS = 2
DEFAULT_NIGHTS = 7
MONTHS_RU = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]
MONTHS_GEN = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


def implicit_preferences(req: Requirements) -> list[Preference]:
    """«Скрытые» требования из контекста. Возвращает только то, чего ещё нет."""
    have = {p.key for p in req.preferences}
    out: list[Preference] = []

    def add(key: PrefKey, prio: Priority) -> None:
        if key not in have:
            out.append(Preference(key=key, priority=prio, source=Source.assumed))
            have.add(key)

    kids = req.guests.children_ages
    if kids:
        add(PrefKey.family_friendly, Priority.important)
        add(PrefKey.short_transfer, Priority.nice)
    if kids and min(kids) <= 3:
        add(PrefKey.accessible, Priority.nice)  # коляска: ровная дорога, лифт
    month = req.dates.check_in.month if req.dates.check_in else req.dates.month
    if month in (6, 7, 8, 9) and PrefKey.mountains not in have and PrefKey.ski_access not in have:
        add(PrefKey.near_sea, Priority.nice)
    if PrefKey.ski_access in have and month in (12, 1, 2, 3):
        add(PrefKey.mountains, Priority.must)
    return out


def critical_question(req: Requirements, asked: list[str], asked_count: int) -> QuestionBlock | None:
    """Единственный по-настоящему критичный пробел — когда. Состав гостей предполагаем (2 взрослых)."""
    if asked_count >= MAX_QUESTIONS:
        return None
    d = req.dates
    if not d.check_in and not d.month:
        return QuestionBlock(
            text="Когда планируете поездку? Можно примерно — месяц и сколько ночей.",
            paths=["dates.check_in", "dates.month", "dates.nights"],
            quick_replies=[
                QuickReply(label="Ближайшие выходные", text="на ближайшие выходные"),
                QuickReply(label="В ноябре на неделю", text="в ноябре на неделю"),
                QuickReply(label="В декабре на неделю", text="в декабре на неделю"),
                QuickReply(label="На новогодние", text="на новогодние праздники"),
            ],
        )
    return None


def resolve_dates(req: Requirements, today: date) -> tuple[list[PatchOp], list[str]]:
    """Достроить точные даты из «месяц + ночи». Возвращает (ops, пути-предположения)."""
    d = req.dates
    ops: list[PatchOp] = []
    assumed: list[str] = []
    nights = d.nights
    if nights is None:
        nights = DEFAULT_NIGHTS if not (d.check_in and d.check_out) else None
        if nights:
            ops.append(PatchOp(op="set", path="dates.nights", value=nights))
            assumed.append("dates.nights")
    if d.check_in and not d.check_out and nights:
        ops.append(PatchOp(op="set", path="dates.check_out", value=(d.check_in + timedelta(days=nights)).isoformat()))
    elif not d.check_in and d.month:
        year = d.year or (today.year + (1 if d.month < today.month else 0))
        start = date(year, d.month, 1)
        earliest = today + timedelta(days=3)
        if start < earliest:
            start = earliest
        # ближайшая суббота — типичный заезд
        start += timedelta(days=(5 - start.weekday()) % 7)
        ci = start
        co = ci + timedelta(days=nights or DEFAULT_NIGHTS)
        ops += [
            PatchOp(op="set", path="dates.check_in", value=ci.isoformat()),
            PatchOp(op="set", path="dates.check_out", value=co.isoformat()),
        ]
        assumed += ["dates.check_in", "dates.check_out"]
    return ops, assumed


def guests_label(req: Requirements) -> str:
    a = req.guests.adults or 2
    s = {1: "1 взрослый"}.get(a, f"{a} взрослых")
    if req.guests.children_ages:
        ages = ", ".join(str(x) for x in req.guests.children_ages)
        n = len(req.guests.children_ages)
        s += f" + {n} {'ребёнок' if n == 1 else 'детей'} ({ages})"
    if req.guests.pets:
        s += " + " + ", ".join({"dog": "собака", "cat": "кошка"}.get(p.value, "питомец") for p in req.guests.pets)
    return s


def dates_label(req: Requirements) -> str:
    d = req.dates
    if d.check_in and d.check_out:
        n = (d.check_out - d.check_in).days
        return f"{d.check_in.day} {MONTHS_GEN[d.check_in.month - 1]} — {d.check_out.day} {MONTHS_GEN[d.check_out.month - 1]}, {n} ноч."
    if d.month:
        return f"в {MONTHS_RU[d.month - 1]}" + (f", {d.nights} ноч." if d.nights else "")
    return "даты не заданы"


def _shift(label: str, days: int) -> QuickReply:
    return QuickReply(label=label, action=ApplyPatchAction(ops=[PatchOp(op="shift_days", path="dates", value=days)], label=label))


def assumption_chips(req: Requirements) -> list[AssumptionChip]:
    """Чипсы «Я понял так»: всё ключевое, источник виден, к предположениям — быстрые правки."""
    prov = req.provenance
    chips: list[AssumptionChip] = [
        AssumptionChip(
            path="dates",
            label="Даты",
            value_label=dates_label(req),
            source=prov.get("dates.check_in", Source.user),
            options=[_shift("−1 неделя", -7), _shift("+1 неделя", 7), _shift("+2 дня", 2)],
        ),
        AssumptionChip(
            path="guests",
            label="Кто едет",
            value_label=guests_label(req),
            source=prov.get("guests.adults", Source.default if req.guests.adults is None else Source.user),
            options=[
                QuickReply(
                    label="Вдвоём",
                    action=ApplyPatchAction(
                        ops=[
                            PatchOp(op="set", path="guests.adults", value=2),
                            PatchOp(op="set", path="guests.children_ages", value=[]),
                        ]
                    ),
                ),
                QuickReply(label="+ ребёнок", text="с ребёнком 6 лет"),
                QuickReply(label="+ собака", action=ApplyPatchAction(ops=[PatchOp(op="add", path="guests.pets", value="dog")])),
            ],
        ),
    ]
    b = req.budget
    if b.amount:
        scope = {"total_trip": "на всю поездку", "hotel_total": "на отель", "per_night": "за ночь"}[b.scope.value]
        chips.append(
            AssumptionChip(
                path="budget",
                label="Бюджет",
                value_label=f"до {fmt_rub(b.amount)} {scope}",
                source=prov.get("budget.amount", Source.user),
                options=[
                    QuickReply(label="−15%", action=ApplyPatchAction(ops=[PatchOp(op="scale", path="budget.amount", value=0.85)]))
                ],
            )
        )
    else:
        chips.append(
            AssumptionChip(
                path="budget",
                label="Бюджет",
                value_label="не ограничен — подберу по соотношению цена/качество",
                source=Source.default,
                options=[
                    QuickReply(
                        label=f"до {fmt_rub(v)}", action=ApplyPatchAction(ops=[PatchOp(op="set", path="budget.amount", value=v)])
                    )
                    for v in (60_000, 100_000, 150_000)
                ],
            )
        )
    for p in req.preferences:
        chips.append(
            AssumptionChip(
                path=f"preferences.{p.key.value}",
                label="Обязательно"
                if p.priority == Priority.must
                else "Важно"
                if p.priority == Priority.important
                else "Желательно",
                value_label=PREF_LABELS.get(p.key, p.key.value),
                source=p.source,
                options=[
                    QuickReply(
                        label="Убрать", action=ApplyPatchAction(ops=[PatchOp(op="remove", path="preferences", value=p.key.value)])
                    )
                ],
            )
        )
    if req.district_ids:
        chips.append(
            AssumptionChip(
                path="district_ids",
                label="Район",
                value_label=", ".join(req.district_ids),
                source=prov.get("district_ids", Source.user),
                options=[
                    QuickReply(label="Любой район", action=ApplyPatchAction(ops=[PatchOp(op="unset", path="district_ids")]))
                ],
            )
        )
    return chips
