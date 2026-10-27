"""Проверка состыковок и целостности. Детерминированно, без LLM. Владелец: ML2.

Правило: непроверяемое — не «ок», а явное Issue(info) с вопросом/действием.
Время прибытия НЕ спрашиваем анкетой на старте: спрашиваем здесь, когда оно реально влияет
(выбран вариант → нужно проверить заселение). Это и есть «уточнять только то, без чего нельзя».
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time

from ..requirements_ops import fmt_rub
from ..schemas import (
    ArrivalMode,
    BudgetScope,
    Cart,
    CartItemKind,
    Issue,
    LocationPack,
    PatchOp,
    QuickReply,
    Requirements,
    Severity,
    Source,
    Variant,
)
from ..schemas.actions import AddExtraAction, ApplyPatchAction, ConfirmAssumptionsAction

AIRPORT_BUFFER_MIN = 120
RAIL_BUFFER_MIN = 30


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def pending_confirmations(req: Requirements, location_name: str | None = None) -> list[tuple[str, str]]:
    """Что агент ПРЕДПОЛОЖИЛ из ключевого для заказа и пользователь ещё не подтвердил.

    Возвращает [(группа, человекочитаемое значение)], группы: "location", "dates", "guests".
    Для подбора вариантов предположения допустимы; для оформления заказа — нет.
    location_name передаётся, когда направлений несколько: город, который пользователь не называл, тоже догадка.
    """
    out: list[tuple[str, str]] = []
    prov = req.provenance
    if location_name and prov.get("location_id") != Source.user:
        out.append(("location", f"направление: {location_name} (вы его не называли)"))
    d = req.dates
    if d.check_in and d.check_out and prov.get("dates.check_in") != Source.user:
        out.append(
            (
                "dates",
                f"даты {d.check_in.day} {MONTHS_GEN[d.check_in.month - 1]} — {d.check_out.day} "
                f"{MONTHS_GEN[d.check_out.month - 1]} ({(d.check_out - d.check_in).days} ноч.)",
            )
        )
    g = req.guests
    if (
        g.adults is None
        or prov.get("guests.adults") != Source.user
        or (g.children_ages and prov.get("guests.children_ages") != Source.user)
        or (g.rooms and g.rooms > 1 and prov.get("guests.rooms") != Source.user)
    ):
        adults = g.adults or 2
        label = f"{adults} взросл." + (f" + дети ({', '.join(map(str, g.children_ages))} лет)" if g.children_ages else "")
        if g.rooms and g.rooms > 1:
            label += f", {g.rooms} номера"
        out.append(("guests", label))
    return out


def _t(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _hhmm(m: int) -> str:
    m %= 24 * 60
    return f"{m // 60:02d}:{m % 60:02d}"


def arrival_quick_replies() -> list[QuickReply]:
    """Кнопки для вопроса «во сколько прибываете?» — используются оркестратором."""

    def at(label: str, hhmm: str) -> QuickReply:
        h, m = map(int, hhmm.split(":"))
        return QuickReply(
            label=label,
            action=ApplyPatchAction(
                ops=[PatchOp(op="set", path="transport.arrival_time", value=f"{h:02d}:{m:02d}")],
                label=f"Прибытие {label.lower()}",
            ),
        )

    return [
        at("Утром (~08:00)", "08:00"),
        at("Днём (~13:00)", "13:00"),
        at("Вечером (~19:00)", "19:00"),
        at("Ночью (~01:00)", "01:00"),
    ]


class RulesValidator:
    def __init__(
        self,
        today: Callable[[], date] = date.today,
        location_of: Callable[[str], LocationPack] | None = None,
        multi_location: bool = False,
    ):
        """location_of — откуда взять локацию по id (паспорт/виза, если её не передали явно);
        multi_location — направлений несколько, значит не названный пользователем город надо подтвердить."""
        self._today = today
        self._location_of = location_of
        self.multi_location = multi_location

    def _location(self, req: Requirements, location: LocationPack | None) -> LocationPack | None:
        if location is not None:
            return location
        if self._location_of is None:
            return None
        try:
            return self._location_of(req.location_id)
        except KeyError:
            return None

    def pending(self, req: Requirements) -> list[tuple[str, str]]:
        loc = self._location(req, None) if self.multi_location else None
        return pending_confirmations(req, loc.name if loc else None)

    @property
    def today(self) -> date:
        return self._today()

    def validate_variant(self, v: Variant, req: Requirements, location: LocationPack | None = None) -> list[Issue]:
        issues: list[Issue] = []
        o = v.offer
        loc = self._location(req, location)
        if loc is not None and loc.entry is not None:
            e = loc.entry
            if e.visa_free_days and o.nights + 1 > e.visa_free_days:
                issues.append(
                    Issue(
                        code="visa_stay_limit",
                        severity=Severity.warning,
                        message=f"Поездка {o.nights + 1} дн., а без визы {loc.label_in} можно до {e.visa_free_days} дн. — "
                        "сократите поездку или заранее разберитесь с продлением/визой.",
                    )
                )
        if o.check_in < self.today:
            issues.append(Issue(code="dates_in_past", severity=Severity.blocker, message="Дата заезда уже прошла."))
        if o.adults != (req.guests.adults or o.adults) or len(o.children_ages) != len(req.guests.children_ages):
            issues.append(
                Issue(code="guests_mismatch", severity=Severity.blocker, message="Состав гостей в предложении не совпадает с вашим.")
            )

        # бюджет
        b = req.budget.amount
        if b and req.budget.scope != BudgetScope.per_night and v.total_price > b * (1 + req.budget.flex_pct / 100):
            issues.append(Issue(code="over_budget", severity=Severity.warning, message=f"Выше бюджета на {fmt_rub(v.total_price - b)}."))

        if req.guests.pets and v.hotel.pets_max_weight_kg:
            issues.append(
                Issue(
                    code="pet_weight_limit",
                    severity=Severity.info,
                    message=f"Отель принимает животных до {v.hotel.pets_max_weight_kg} кг — проверьте вес питомца.",
                )
            )

        # прибытие → заселение
        ci = _t(v.hotel.check_in_time)
        co = _t(v.hotel.check_out_time)
        # время прибытия: из подобранного билета (если есть), иначе — со слов пользователя
        arr = v.outbound.arrive_at.time() if v.outbound else req.transport.arrival_time
        if arr is None:
            issues.append(
                Issue(
                    code="arrival_unknown",
                    severity=Severity.info,
                    message="Во сколько вы прибываете? Проверю, успеваете ли к заселению и нужен ли ранний заезд.",
                )
            )
        else:
            t_in = v.transfer_in.minutes if v.transfer_in else 0
            at_hotel = _minutes(arr) + t_in
            wait = _minutes(ci) - at_hotel
            if at_hotel >= 24 * 60 - 60 or _minutes(arr) < 5 * 60:
                issues.append(
                    Issue(
                        code="late_arrival",
                        severity=Severity.warning,
                        message=f"Будете в отеле около {_hhmm(at_hotel)} — предупредим отель о позднем заезде.",
                    )
                )
            elif wait >= 90:
                price = early_check_in_price(v)
                msg = (
                    f"Будете в отеле около {_hhmm(at_hotel)}, заселение с {v.hotel.check_in_time} — "
                    f"ждать {wait // 60} ч {wait % 60:02d} мин."
                )
                msg += (
                    f" Ранний заезд: {fmt_rub(price)}."
                    if price is not None
                    else " Ранний заезд отель не гарантирует — можно оставить багаж."
                )
                issues.append(
                    Issue(
                        code="early_arrival",
                        severity=Severity.warning,
                        message=msg,
                        fix=QuickReply(
                            label=f"Добавить ранний заезд (+{fmt_rub(price)})",
                            action=AddExtraAction(kind=CartItemKind.early_check_in),
                        )
                        if price is not None
                        else None,
                    )
                )

        # выезд → отъезд
        dep = v.inbound.depart_at.time() if v.inbound else req.transport.departure_time
        if dep is not None and v.transfer_out:
            train = (v.inbound.mode == "train") if v.inbound else req.transport.arrival_mode == ArrivalMode.train
            buffer = RAIL_BUFFER_MIN if train else AIRPORT_BUFFER_MIN
            leave_hotel = _minutes(dep) - buffer - v.transfer_out.minutes
            gap = leave_hotel - _minutes(co)
            if gap >= 180:
                issues.append(
                    Issue(
                        code="late_departure",
                        severity=Severity.info,
                        message=f"Выезд в {v.hotel.check_out_time}, а выходить можно в {_hhmm(leave_hotel)} — {gap // 60} ч без номера.",
                        fix=QuickReply(label="Добавить поздний выезд", action=AddExtraAction(kind=CartItemKind.late_check_out)),
                    )
                )
            if leave_hotel < 0:
                issues.append(
                    Issue(
                        code="departure_too_early",
                        severity=Severity.warning,
                        message="Отъезд очень ранний — выезжать придётся ночью, такси заказать заранее.",
                    )
                )
        return issues

    def validate_cart(self, cart: Cart, variant: Variant | None, req: Requirements) -> list[Issue]:
        issues: list[Issue] = []
        stays = [i for i in cart.items if i.kind == CartItemKind.hotel_stay]
        if len(stays) != 1:
            issues.append(Issue(code="cart_no_stay", severity=Severity.blocker, message="В корзине должно быть проживание."))
        if sum(i.price for i in cart.items) != cart.total:
            issues.append(Issue(code="cart_total_mismatch", severity=Severity.blocker, message="Сумма корзины не сходится."))
        pending = self.pending(req)
        if pending:
            issues.append(
                Issue(
                    code="unconfirmed_assumptions",
                    severity=Severity.blocker,
                    message="Перед оформлением подтвердите то, что я предположил: " + "; ".join(v for _, v in pending) + ".",
                    fix=QuickReply(
                        label="Всё верно — оформить",
                        action=ConfirmAssumptionsAction(paths=[g for g, _ in pending], then_checkout=True),
                    ),
                )
            )
        loc = self._location(req, None)
        if loc is not None and loc.entry is not None:
            # перед покупкой — полный список документов (в карточках вариантов его нет, чтобы не повторять трижды)
            e = loc.entry
            msg = f"{loc.name}, {loc.country}: {e.passport} {e.visa}" + (f" {e.insurance}" if e.insurance else "")
            issues.append(Issue(code="abroad_documents", severity=Severity.info, message=msg))
            if e.money:
                issues.append(Issue(code="abroad_money", severity=Severity.info, message=e.money))
        if variant is not None:
            for x in self.validate_variant(variant, req):
                if x.code == "early_arrival" and any(i.kind == CartItemKind.early_check_in for i in cart.items):
                    continue
                if x.code == "late_departure" and any(i.kind == CartItemKind.late_check_out for i in cart.items):
                    continue
                issues.append(x)
        return issues


def early_check_in_price(v: Variant) -> int | None:
    """Цена раннего заезда = % от средней ночи. None — отель не продаёт ранний заезд."""
    pct = v.hotel.early_check_in_fee_pct
    if pct is None:
        return None
    return int(round(v.offer.price_per_night_avg * pct / 100, -1))


def late_check_out_price(v: Variant) -> int:
    return int(round(v.offer.price_per_night_avg * 0.5, -1))
