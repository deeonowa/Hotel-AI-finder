"""Корзина → заказ → имитация оплаты → запросы после покупки. Владелец: FS.

Модель данных как у настоящего заказа (позиции, статусы, номер, ссылка на оплату),
поэтому замена имитации на реальный booking/payment API — это замена методов checkout/pay.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from ..ports import BookingRepository, Validator
from ..requirements_ops import fmt_rub
from ..schemas import (
    Cart,
    CartItem,
    CartItemKind,
    Order,
    OrderStatus,
    PostPurchaseKind,
    PostPurchaseRequest,
    Requirements,
    Variant,
)
from ..validation.rules import early_check_in_price, late_check_out_price

MEALS_RU = {"RO": "без питания", "BB": "завтраки", "HB": "полупансион", "FB": "полный пансион", "AI": "всё включено"}


def _now() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class BookingError(ValueError):
    pass


class BookingService:
    def __init__(self, repo: BookingRepository, validator: Validator):
        self.repo = repo
        self.validator = validator

    # ---------------------------------------------------------------- корзина
    def put_variant(self, session_id: str, cart_id: str | None, v: Variant, req: Requirements, with_transfer: bool) -> Cart:
        o = v.offer
        refund = f"бесплатная отмена до {o.cancellation.free_until:%d.%m}" if o.cancellation.refundable else "невозвратный тариф"
        items = [
            CartItem(
                id=_id("ci"),
                kind=CartItemKind.hotel_stay,
                title=f"{v.hotel.name} — {o.room_name}",
                details=f"{o.check_in:%d.%m}–{o.check_out:%d.%m}, {o.nights} ноч., {MEALS_RU[o.meal_plan.value]}, {refund}",
                price=o.price_total,
                ref_id=o.offer_id,
                removable=False,
            )
        ]
        if with_transfer and v.transfer_in:
            for t in (v.transfer_in, v.transfer_out):
                if t is None:
                    continue
                direction = f"{t.hub_name} → отель" if t.direction == "to_hotel" else f"отель → {t.hub_name}"
                items.append(
                    CartItem(
                        id=_id("ci"),
                        kind=CartItemKind.transfer,
                        title=f"Трансфер {direction}",
                        details=f"~{t.minutes} мин, такси (оценка)",
                        price=t.price,
                        ref_id=t.hub_id,
                    )
                )
        cart = Cart(
            id=cart_id or _id("cart"),
            session_id=session_id,
            variant_id=v.id,
            items=items,
            total=sum(i.price for i in items),
            updated_at=_now(),
        )
        cart.issues = self.validator.validate_cart(cart, v, req)
        self.repo.save_cart(cart)
        return cart

    def add_extra(self, cart: Cart, v: Variant, req: Requirements, kind: CartItemKind) -> Cart:
        if any(i.kind == kind for i in cart.items):
            return cart
        if kind == CartItemKind.early_check_in:
            price = early_check_in_price(v)
            if price is None:
                raise BookingError("Отель не продаёт ранний заезд")
            item = CartItem(
                id=_id("ci"),
                kind=kind,
                title="Ранний заезд",
                details=f"гарантированное заселение утром ({v.hotel.early_check_in_fee_pct}% от ночи)",
                price=price,
            )
        elif kind == CartItemKind.late_check_out:
            item = CartItem(
                id=_id("ci"), kind=kind, title="Поздний выезд", details="до 18:00 (50% от ночи)", price=late_check_out_price(v)
            )
        else:
            raise BookingError(f"Нельзя добавить {kind}")
        cart.items.append(item)
        return self._recalc(cart, v, req)

    def remove_item(self, cart: Cart, item_id: str, v: Variant | None, req: Requirements) -> Cart:
        cart.items = [i for i in cart.items if not (i.id == item_id and i.removable)]
        return self._recalc(cart, v, req)

    def _recalc(self, cart: Cart, v: Variant | None, req: Requirements) -> Cart:
        cart.total = sum(i.price for i in cart.items)
        cart.updated_at = _now()
        cart.issues = self.validator.validate_cart(cart, v, req)
        self.repo.save_cart(cart)
        return cart

    # ---------------------------------------------------------------- заказ
    def checkout(self, cart: Cart, guest_name: str | None) -> Order:
        if any(i.severity.value == "blocker" for i in cart.issues):
            raise BookingError("В корзине есть блокирующие проблемы: " + "; ".join(i.message for i in cart.issues))
        now = _now()
        order = Order(
            id=_id("ord"),
            number=f"OST-{now:%d%m}-{secrets.randbelow(9000) + 1000}",
            session_id=cart.session_id,
            variant_id=cart.variant_id,
            items=list(cart.items),
            total=cart.total,
            guest_name=guest_name,
            created_at=now,
        )
        self.repo.save_order(order)
        return order

    def pay(self, order: Order) -> Order:
        """ИМИТАЦИЯ оплаты. Реальная интеграция: создать платёж у провайдера → вебхук → paid."""
        if order.status == OrderStatus.paid:
            return order
        order.status = OrderStatus.paid
        order.paid_at = _now()
        order.payment_ref = f"SIM-{secrets.token_hex(4).upper()}"
        self.repo.save_order(order)
        return order

    def post_purchase(self, order: Order, kind: PostPurchaseKind, details: dict) -> tuple[Order, str]:
        req = PostPurchaseRequest(id=_id("ppr"), kind=kind, details=details, created_at=_now())
        msg = {
            PostPurchaseKind.early_check_in: "Запросил у отеля ранний заезд — подтверждение придёт в течение дня (имитация).",
            PostPurchaseKind.late_check_out: "Запросил поздний выезд (имитация).",
            PostPurchaseKind.change_dates: "Создал запрос на изменение дат: проверю наличие и разницу в цене (имитация).",
            PostPurchaseKind.add_transfer: "Добавил запрос на трансфер к заказу (имитация).",
            PostPurchaseKind.reminder: "Напомню о поездке за 3 дня и в день заезда (имитация).",
            PostPurchaseKind.cancel: "Создал запрос на отмену. Условия возврата — по тарифу.",
        }[kind]
        if kind in (PostPurchaseKind.early_check_in, PostPurchaseKind.reminder, PostPurchaseKind.add_transfer):
            req.status = "confirmed"
        if kind in (PostPurchaseKind.change_dates, PostPurchaseKind.cancel):
            order.status = OrderStatus.change_requested
        order.requests.append(req)
        self.repo.save_order(order)
        return order, msg


def cart_summary(cart: Cart) -> str:
    return f"В корзине {len(cart.items)} поз. на {fmt_rub(cart.total)}."
