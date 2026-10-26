"""Корзина и заказ. Оплата имитируется, но модель данных — как у настоящего заказа."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .common import Model
from .trip import Issue
from .vocab import CartItemKind, OrderStatus, PostPurchaseKind


class CartItem(Model):
    id: str
    kind: CartItemKind
    title: str
    details: str = ""
    price: int
    ref_id: str | None = Field(default=None, description="offer_id / variant_id / hub_id")
    removable: bool = True


class Cart(Model):
    id: str
    session_id: str
    variant_id: str | None = None
    items: list[CartItem] = Field(default_factory=list)
    total: int = 0
    issues: list[Issue] = Field(default_factory=list)
    updated_at: datetime


class PostPurchaseRequest(Model):
    id: str
    kind: PostPurchaseKind
    status: str = "requested"  # requested | confirmed | declined
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Order(Model):
    id: str
    number: str = Field(description="человекочитаемый номер, напр. OST-2611-4821")
    session_id: str
    variant_id: str | None = None
    items: list[CartItem]
    total: int
    status: OrderStatus = OrderStatus.awaiting_payment
    guest_name: str | None = None
    created_at: datetime
    paid_at: datetime | None = None
    payment_ref: str | None = Field(default=None, description="имитация: SIM-…")
    requests: list[PostPurchaseRequest] = Field(default_factory=list)
    is_simulated: bool = True
