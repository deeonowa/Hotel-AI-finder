"""Требования к поездке и операции их изменения.

Ключевая идея: ЛЮБОЕ сообщение пользователя (первое или «а если на 2 дня позже?»)
превращается в intent + список PatchOp над текущими Requirements.
Первое сообщение — это просто патч к пустым требованиям. Отсюда пересборка «бесплатно».
"""

from __future__ import annotations

from datetime import date, time
from typing import Any, Literal

from pydantic import Field, model_validator

from .common import Model
from .vocab import (
    ArrivalMode,
    BudgetScope,
    Intent,
    PetKind,
    PostPurchaseKind,
    PrefKey,
    Priority,
    Source,
)


class DateWindow(Model):
    check_in: date | None = None
    check_out: date | None = None
    month: int | None = Field(default=None, ge=1, le=12, description="Если точных дат нет")
    year: int | None = None
    nights: int | None = Field(default=None, ge=1, le=60)
    flexibility_days: int = Field(default=0, ge=0, le=14)

    @model_validator(mode="after")
    def _order(self) -> DateWindow:
        if self.check_in and self.check_out and self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self

    @property
    def is_exact(self) -> bool:
        return self.check_in is not None and self.check_out is not None

    @property
    def nights_count(self) -> int | None:
        if self.is_exact:
            return (self.check_out - self.check_in).days  # type: ignore[operator]
        return self.nights


class Guests(Model):
    adults: int | None = Field(default=None, ge=1, le=10)
    children_ages: list[int] = Field(default_factory=list)
    pets: list[PetKind] = Field(default_factory=list)
    rooms: int | None = Field(default=None, ge=1, le=5)

    @property
    def total_people(self) -> int:
        return (self.adults or 0) + len(self.children_ages)


class Budget(Model):
    amount: int | None = Field(default=None, ge=0, description="RUB")
    scope: BudgetScope = BudgetScope.total_trip
    flex_pct: int = Field(default=0, ge=0, le=50, description="«около 100к» → 10")


class Preference(Model):
    key: PrefKey
    priority: Priority = Priority.important
    value: float | int | str | None = None
    source: Source = Source.user


class TransportPlan(Model):
    origin_city: str | None = None
    arrival_mode: ArrivalMode = ArrivalMode.unknown
    arrival_hub_id: str | None = None
    arrival_time: time | None = None
    departure_time: time | None = None
    max_stops: int | None = Field(default=None, ge=0, le=2, description="0 = только прямые рейсы")


class Requirements(Model):
    location_id: str = Field(default="sochi", description="куда едем (data/locations/<id>); не названо — локация по умолчанию")
    geo_scope: Literal["domestic", "abroad"] | None = Field(default=None, description="«по России» / «за границу»; None — не сказано")
    excluded_location_ids: list[str] = Field(default_factory=list, description="«только не Дубай», «не в Турцию» — не предлагать")
    district_ids: list[str] = Field(default_factory=list, description="Пусто = любой район")
    dates: DateWindow = Field(default_factory=DateWindow)
    guests: Guests = Field(default_factory=Guests)
    budget: Budget = Field(default_factory=Budget)
    preferences: list[Preference] = Field(default_factory=list)
    transport: TransportPlan = Field(default_factory=TransportPlan)
    notes: list[str] = Field(default_factory=list, description="Факты без поля: «годовщина свадьбы»")
    provenance: dict[str, Source] = Field(default_factory=dict, description="путь поля → откуда значение, напр. 'dates.nights': assumed")

    def pref(self, key: PrefKey) -> Preference | None:
        return next((p for p in self.preferences if p.key == key), None)

    def has_pref(self, key: PrefKey, *, at_least: Priority = Priority.nice) -> bool:
        order = {Priority.nice: 0, Priority.important: 1, Priority.must: 2}
        p = self.pref(key)
        return p is not None and order[p.priority] >= order[at_least]


# ---- Патчи ------------------------------------------------------------------

PatchPath = Literal[
    "location_id",  # смена города/страны; районы и хаб прибытия прежнего города сбрасываются
    "geo_scope",  # domestic | abroad
    "excluded_location_ids",  # add/remove id направления, которое пользователь отверг
    "district_ids",
    "dates",  # только для shift_days: сдвинуть заезд и выезд
    "dates.check_in",
    "dates.check_out",
    "dates.month",
    "dates.year",
    "dates.nights",
    "dates.flexibility_days",
    "guests.adults",
    "guests.children_ages",
    "guests.pets",
    "guests.rooms",
    "budget.amount",
    "budget.scope",
    "budget.flex_pct",
    "preferences",
    "transport.origin_city",
    "transport.arrival_mode",
    "transport.arrival_hub_id",
    "transport.arrival_time",
    "transport.departure_time",
    "transport.max_stops",
    "notes",
]
"""Белый список путей. NLU не может изменить ничего за его пределами — это и защита от инъекций."""

PatchOpName = Literal["set", "unset", "add", "remove", "shift_days", "scale"]


class PatchOp(Model):
    """Атомарное изменение требований.

    Примеры:
      {"op":"set","path":"guests.adults","value":2}
      {"op":"add","path":"guests.pets","value":"dog"}
      {"op":"add","path":"preferences","value":{"key":"near_sea","priority":"must"}}
      {"op":"remove","path":"preferences","value":"pool"}          # по ключу
      {"op":"shift_days","path":"dates","value":2}                 # «на 2 дня позже»
      {"op":"scale","path":"budget.amount","value":0.85}           # «подешевле»
    """

    op: PatchOpName
    path: PatchPath
    value: Any = None


class NluResult(Model):
    """Выход NLU (LLM или правила) — единственное, что «понимание» отдаёт оркестратору."""

    intent: Intent
    patch: list[PatchOp] = Field(default_factory=list)
    assumed_paths: list[str] = Field(default_factory=list, description="какие пути патча — догадка NLU (покажем чипсом как «предположил»)")
    target_variant_ids: list[str] = Field(default_factory=list)
    variant_ordinal: int | None = Field(default=None, description="«второй вариант» → 2")
    info_question: str | None = None
    more_options: bool = Field(default=False, description="«предложи другие отели» — показать НОВЫЕ, без уже показанных")
    inspire_level: Literal["location", "district"] | None = Field(
        default=None, description="inspire: сравнить города (location) или районы выбранного города (district)"
    )
    candidate_location_ids: list[str] = Field(
        default_factory=list, description="«Сочи или Анталья?» — сравнить только эти направления (id из каталога)"
    )
    unknown_destination: str | None = Field(
        default=None,
        description="пользователь назвал город/страну, которых нет в каталоге («Батуми») — агент честно скажет и предложит есть",
    )
    post_purchase_kind: PostPurchaseKind | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    parser: Literal["llm", "rules"] = "rules"
