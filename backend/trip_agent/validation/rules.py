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
    Variant,
)
from ..schemas.actions import AddExtraAction, ApplyPatchAction

AIRPORT_BUFFER_MIN = 120
RAIL_BUFFER_MIN = 30


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
    def __init__(self, today: Callable[[], date] = date.today):
        self._today = today

    @property
    def today(self) -> date:
        return self._today()

    def validate_variant(self, v: Variant, req: Requirements, location: LocationPack | None = None) -> list[Issue]:
        issues: list[Issue] = []
        o = v.offer
        if o.check_in < self.today:
            issues.append(Issue(code="dates_in_past", severity=Severity.blocker, message="Дата заезда уже прошла."))
        if o.adults != (req.guests.adults or o.adults) or len(o.children_ages) != len(req.guests.children_ages):
            issues.append(
                Issue(
                    code="guests_mismatch", severity=Severity.blocker, message="Состав гостей в предложении не совпадает с вашим."
                )
            )

        # бюджет
        b = req.budget.amount
        if b and req.budget.scope != BudgetScope.per_night and v.total_price > b * (1 + req.budget.flex_pct / 100):
            issues.append(
                Issue(code="over_budget", severity=Severity.warning, message=f"Выше бюджета на {fmt_rub(v.total_price - b)}.")
            )

        # прибытие → заселение
        ci = _t(v.hotel.check_in_time)
        co = _t(v.hotel.check_out_time)
        arr = req.transport.arrival_time
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
        dep = req.transport.departure_time
        if dep is not None and v.transfer_out:
            buffer = AIRPORT_BUFFER_MIN if req.transport.arrival_mode != ArrivalMode.train else RAIL_BUFFER_MIN
            leave_hotel = _minutes(dep) - buffer - v.transfer_out.minutes
            gap = leave_hotel - _minutes(co)
            if gap >= 180:
                issues.append(
                    Issue(
                        code="late_departure",
                        severity=Severity.info,
                        message=f"Выезд в {v.hotel.check_out_time}, а выходить можно в {_hhmm(leave_hotel)} — "
                        f"{gap // 60} ч без номера.",
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
