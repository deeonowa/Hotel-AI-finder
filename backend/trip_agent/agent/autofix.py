"""Автономное исправление проблем выбранного варианта. Владелец: ML1 (+ ML2: стратегии поиска).

Агент не просто сообщает о проблеме и предлагает «ослабить условия», а САМ выбирает следующий шаг:
  1) распознаёт проблему по замечаниям валидатора и «кому не подойдёт»;
  2) выбирает стратегию (какое временное ограничение добавить к поиску);
  3) ищет — сначала в том же районе, потом во всех разрешённых;
  4) проверяет найденный вариант тем же валидатором (проблема должна исчезнуть);
  5) предлагает исправленную поездку с разницей «было → стало». Решение остаётся за пользователем.

Временные ограничения в требования пользователя НЕ записываются — это внутренний поиск агента.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..ports import Ranker, Validator
from ..requirements_ops import PatchError, apply_patch
from ..schemas import Archetype, Hotel, LocationPack, PatchOp, Requirements, Severity, Source, Variant

LONG_NIGHT_TRANSFER_MIN = 45
SHORT_TRANSFER_TARGET_MIN = 35
FLAT_CLIMB_M = 15


@dataclass
class Problem:
    code: str
    message: str  # что не так — для пользователя
    strategy: str  # что агент решил сделать
    hotel_filter: Callable[[Hotel], str | None] | None = None
    extra_ops: tuple[PatchOp, ...] = ()


@dataclass
class Fix:
    problem: Problem
    variant: Variant
    searched: str  # где искал: «в том же районе» / «во всех районах»
    checked: int  # сколько отелей проверил


def _mins(hhmm: str) -> int:
    t = datetime.strptime(hhmm, "%H:%M")
    return t.hour * 60 + t.minute


def detect(v: Variant, req: Requirements) -> list[Problem]:
    """Проблемы, которые агент умеет чинить сам, в порядке важности."""
    out: list[Problem] = []
    codes = {i.code: i for i in v.issues}
    arr = v.outbound.arrive_at.time() if v.outbound else req.transport.arrival_time

    if "early_arrival" in codes and v.hotel.early_check_in_fee_pct is None and arr is not None:
        at_hotel = arr.hour * 60 + arr.minute + (v.transfer_in.minutes if v.transfer_in else 0)

        def early_ok(h: Hotel, at_hotel: int = at_hotel) -> str | None:
            if h.early_check_in_fee_pct is not None or _mins(h.check_in_time.strftime("%H:%M")) <= at_hotel + 60:
                return None
            return "нет раннего заезда"

        out.append(
            Problem(
                code="no_early_check_in",
                message=f"{codes['early_arrival'].message.rstrip('.')}, а этот отель ранний заезд не продаёт.",
                strategy="отель, где можно гарантированно заселиться утром",
                hotel_filter=early_ok,
            )
        )

    long_in = v.transfer_in is not None and v.transfer_in.minutes >= LONG_NIGHT_TRANSFER_MIN
    if long_in and ("late_arrival" in codes or "departure_too_early" in codes):
        t = v.transfer_in
        assert t is not None
        if "late_arrival" in codes and arr is not None:
            at = (arr.hour * 60 + arr.minute + t.minutes) % (24 * 60)
            msg = (
                f"прилёт в {arr:%H:%M}, а дорога до отеля — {t.minutes} мин: в номере будете около "
                f"{at // 60:02d}:{at % 60:02d}, ночью и после долгого перелёта."
            )
        else:
            msg = f"{codes['departure_too_early'].message} Дорога до «{t.hub_name}» — {t.minutes} мин."
        out.append(
            Problem(
                code="long_night_transfer",
                message=msg,
                strategy=f"отель не дальше {SHORT_TRANSFER_TARGET_MIN} мин от «{t.hub_name}»",
                extra_ops=(
                    PatchOp(
                        op="add",
                        path="preferences",
                        value={"key": "short_transfer", "priority": "must", "value": SHORT_TRANSFER_TARGET_MIN},
                    ),
                ),
            )
        )

    hilly = next((i for i in v.fit_warnings if i.code == "hilly_walk" and i.severity == Severity.warning), None)
    if hilly is not None:

        def flat(h: Hotel) -> str | None:
            climb = h.climb_to_hotel_m if h.climb_to_hotel_m is not None else (30 if h.hilly_walk else 0)
            return None if climb <= FLAT_CLIMB_M and not h.hilly_walk else "дорога к морю в гору"

        out.append(
            Problem(
                code="hilly_walk",
                message=hilly.message,
                strategy="похожий отель с ровной дорогой к морю",
                hotel_filter=flat,
            )
        )
    return out


def find_fix(
    v: Variant,
    req: Requirements,
    *,
    ranker: Ranker,
    validator: Validator,
    location: LocationPack,
    hotels: list[Hotel],
    dismissed: set[str],
) -> Fix | None:
    """Первая проблема, для которой нашлась проверенная замена. None — чинить нечего или не нашлось."""
    ci, co = v.offer.check_in, v.offer.check_out
    for p in detect(v, req):
        if f"{v.hotel.id}:{p.code}" in dismissed:
            continue
        try:
            base, _ = apply_patch(req, list(p.extra_ops), source=Source.assumed) if p.extra_ops else (req, [])
        except PatchError:
            continue
        scopes: list[tuple[str, list[str]]] = [("в том же районе", [v.hotel.district_id])]
        if not req.district_ids or len(req.district_ids) > 1:
            scopes.append(("во всех подходящих районах", list(req.district_ids)))
        else:
            # Пользователь сам выбрал район, но внутри него проблему не решить → честно предлагаем
            # выйти за его рамки (это заметно в тексте; согласие — за пользователем).
            names = ", ".join(d.name for d in location.districts if d.id in req.district_ids)
            scopes.append((f"за пределами выбранного района ({names}) — внутри него вариантов нет", []))
        for label, districts in scopes:
            r2 = base.model_copy(deep=True)
            r2.district_ids = districts
            rr = ranker.rank(r2, hotels, ci, co, hotel_filter=p.hotel_filter)
            for alt in rr.variants:
                if alt.hotel.id == v.hotel.id:
                    continue
                alt.issues = validator.validate_variant(alt, req, location)
                if any(x.code == p.code for x in detect(alt, req)):
                    continue  # проблема не ушла — такой вариант не предлагаем
                alt.archetype = Archetype.alternative
                alt.title = "Исправленный вариант"
                return Fix(problem=p, variant=alt, searched=label, checked=rr.stats.hotels_total)
    return None
