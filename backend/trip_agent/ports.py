"""Порты (интерфейсы) между модулями. Это ГЛАВНЫЙ файл для параллельной работы трёх человек.

Агент (ML1) зависит только от этих протоколов. Реализации (ML2, FS) подключаются в
container.py. Чтобы встроить агента в реальный бизнес, достаточно написать новые реализации
CatalogProvider/PricingProvider поверх партнёрского API — агент не меняется.

Владельцы:
  LLMClient, Nlu, Nlg ................ ML1
  CatalogProvider, PricingProvider,
  TransferProvider, Ranker, Validator  ML2
  SessionStore, BookingRepository ..... FS
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from .schemas import (
    POI,
    Cart,
    ClimateMonth,
    Hotel,
    Issue,
    LocationPack,
    NluResult,
    Offer,
    Order,
    RankingResult,
    Requirements,
    TransferQuote,
    TripState,
    Variant,
)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------- ML1 -------
@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete_json(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        """Вернуть объект schema. Внутри: JSON-режим + валидация Pydantic + 1 повтор с ошибкой."""
        ...

    def complete_text(self, system: str, user: str, *, temperature: float = 0.3, max_tokens: int = 600) -> str: ...


class Nlu(Protocol):
    def parse(self, text: str, state: TripState, today: date) -> NluResult:
        """Текст пользователя → intent + патч к требованиям. Никогда не бросает исключений наружу."""
        ...


class Nlg(Protocol):
    def summarize_turn(self, state: TripState, facts: dict) -> str:
        """Короткий текст ответа по ФАКТАМ (facts). Никаких чисел, которых нет в facts."""
        ...

    def answer_question(self, question: str, state: TripState, context: dict) -> str:
        """Ответ на вопрос («есть ли там бассейн?») строго по context; иначе честно «не знаю»."""
        ...


# ---------------------------------------------------------------- ML2 -------
class CatalogProvider(Protocol):
    """Статический контент (как hotel-content API у поставщиков)."""

    def get_location(self, location_id: str) -> LocationPack: ...

    def list_hotels(self, location_id: str) -> Sequence[Hotel]: ...

    def get_hotel(self, hotel_id: str) -> Hotel | None: ...

    def list_pois(self, location_id: str, kinds: Sequence[str] | None = None) -> Sequence[POI]: ...

    def climate(self, location_id: str, climate_zone: str, month: int) -> ClimateMonth | None: ...


class PricingProvider(Protocol):
    """Динамика: цены и наличие на даты (как search/rates API у поставщиков)."""

    def quote(self, hotel: Hotel, req: Requirements, check_in: date, check_out: date) -> list[Offer]:
        """Все подходящие по вместимости предложения отеля на даты (разные номера/питание/отмена)."""
        ...


class TransferProvider(Protocol):
    def estimate(self, location: LocationPack, hub_id: str, hotel: Hotel, direction: str) -> TransferQuote: ...


class Ranker(Protocol):
    def rank(
        self,
        req: Requirements,
        hotels: Sequence[Hotel],
        check_in: date,
        check_out: date,
        *,
        previous: Sequence[Variant] = (),
    ) -> RankingResult:
        """Жёсткие фильтры → скоринг → 3 варианта-компромисса (+ ослабления, если пусто).

        previous — прошлые варианты: при равенстве предпочитаем их (стабильность пересборки).
        """
        ...


class Validator(Protocol):
    def validate_variant(self, variant: Variant, req: Requirements, location: LocationPack) -> list[Issue]: ...

    def validate_cart(self, cart: Cart, variant: Variant | None, req: Requirements) -> list[Issue]: ...


# ---------------------------------------------------------------- FS --------
class SessionStore(Protocol):
    def load(self, session_id: str) -> TripState | None: ...

    def save(self, state: TripState) -> None: ...


class BookingRepository(Protocol):
    def get_cart(self, cart_id: str) -> Cart | None: ...

    def save_cart(self, cart: Cart) -> None: ...

    def get_order(self, order_id: str) -> Order | None: ...

    def save_order(self, order: Order) -> None: ...
