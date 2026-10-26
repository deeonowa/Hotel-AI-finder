"""Структурированные действия из UI. Клики НЕ идут через LLM: быстрее, дешевле, детерминированно.

Текст пользователя → NLU → (intent, patch). Клик → Action → тот же обработчик в оркестраторе.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .common import Model
from .requirements import PatchOp
from .vocab import CartItemKind, PostPurchaseKind


class SelectVariantAction(Model):
    type: Literal["select_variant"] = "select_variant"
    variant_id: str


class ApplyPatchAction(Model):
    """Правка чипса-предположения, кнопки «Дешевле», «+2 дня», «С собакой», ослабление ограничения."""

    type: Literal["apply_patch"] = "apply_patch"
    ops: list[PatchOp]
    label: str | None = None


class CompareAction(Model):
    type: Literal["compare"] = "compare"
    variant_ids: list[str] = Field(default_factory=list, description="пусто = все текущие")


class AddToCartAction(Model):
    type: Literal["add_to_cart"] = "add_to_cart"
    variant_id: str
    with_transfer: bool = True


class AddExtraAction(Model):
    type: Literal["add_extra"] = "add_extra"
    kind: CartItemKind


class RemoveCartItemAction(Model):
    type: Literal["remove_cart_item"] = "remove_cart_item"
    item_id: str


class CheckoutAction(Model):
    type: Literal["checkout"] = "checkout"
    guest_name: str | None = None


class PayAction(Model):
    type: Literal["pay"] = "pay"
    order_id: str


class PostPurchaseAction(Model):
    type: Literal["post_purchase"] = "post_purchase"
    order_id: str
    kind: PostPurchaseKind
    details: dict[str, Any] = Field(default_factory=dict)


class ConfirmAssumptionsAction(Model):
    """Пользователь подтвердил то, что агент предположил (даты, состав). Без этого заказ не оформляется."""

    type: Literal["confirm_assumptions"] = "confirm_assumptions"
    paths: list[Literal["dates", "guests"]] = Field(default_factory=lambda: ["dates", "guests"])
    then_checkout: bool = True


class DismissFixAction(Model):
    """Пользователь отказался от предложенного агентом исправления — больше не предлагать."""

    type: Literal["dismiss_fix"] = "dismiss_fix"
    code: str
    hotel_id: str


class ResetAction(Model):
    type: Literal["reset"] = "reset"


Action = Annotated[
    SelectVariantAction
    | ApplyPatchAction
    | CompareAction
    | AddToCartAction
    | AddExtraAction
    | RemoveCartItemAction
    | CheckoutAction
    | PayAction
    | PostPurchaseAction
    | ConfirmAssumptionsAction
    | DismissFixAction
    | ResetAction,
    Field(discriminator="type"),
]


class QuickReply(Model):
    """Кнопка в чате: либо отправляет текст (как будто напечатал пользователь), либо действие."""

    label: str
    text: str | None = None
    action: Action | None = None
