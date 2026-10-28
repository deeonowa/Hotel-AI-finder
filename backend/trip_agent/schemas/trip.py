"""Результаты подбора и состояние поездки (сессии)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .actions import QuickReply
from .catalog import Offer
from .common import Model
from .requirements import PatchOp, Requirements
from .vocab import Amenity, Archetype, HotelKind, PrefKey, Severity, TripStage, VibeTag


class TransportOffer(Model):
    """Билет туда или обратно (самолёт/поезд). DEMO-расписание и цены — помечено is_demo."""

    id: str
    direction: str = Field(description="outbound | inbound")
    mode: str = Field(description="plane | train")
    carrier: str
    number: str
    origin_city: str
    from_name: str
    to_name: str
    hub_id: str = Field(description="хаб в локации (прилёт для outbound, вылет для inbound)")
    depart_at: datetime
    arrive_at: datetime
    duration_min: int
    stops: int = 0
    stop_city: str | None = None
    price_per_person: int
    passengers_price: int = Field(description="за всех пассажиров (с багажом, если он выбран)")
    baggage_included: bool = Field(default=False, description="в цене есть багаж 23 кг (поезд — всегда)")
    baggage_fee_per_person: int = Field(default=0, description="доплата за багаж 23 кг за человека в одну сторону")
    is_demo: bool = True


class TransferQuote(Model):
    hub_id: str
    hub_name: str
    hotel_id: str
    direction: str = Field(description="to_hotel | from_hotel")
    minutes: int
    distance_km: float
    price: int
    mode: str = "taxi"
    is_estimate: bool = True


class ScoreComponent(Model):
    """Вклад одного критерия в итоговый скор. Это и есть «объяснение выбора» в цифрах."""

    key: str
    label: str
    score: float = Field(ge=0, le=1, description="насколько хорошо по критерию, 0..1")
    weight: float = Field(ge=0, description="важность критерия для ЭТОГО пользователя")
    contribution: float
    detail: str = ""


class Issue(Model):
    """Проблема/риск. Используется и валидатором (состыковки), и fit-проверкой («кому не подойдёт»)."""

    code: str
    severity: Severity
    message: str
    fix: QuickReply | None = None


class HotelCard(Model):
    """Облегчённая карточка отеля для UI (полный Hotel — через GET /hotels/{id})."""

    id: str
    name: str
    kind: HotelKind
    district_id: str
    district_name: str
    stars: int | None = None
    rating: float | None = None
    review_count: int = 0
    distance_to_sea_m: int | None = None
    beach_walk_minutes: int | None = None
    amenities: list[Amenity] = Field(default_factory=list)
    vibe_tags: list[VibeTag] = Field(default_factory=list)
    check_in_time: str
    check_out_time: str
    early_check_in_fee_pct: int | None = Field(default=None, description="% от ночи; None = ранний заезд не продаётся")
    pets_allowed: bool | None = None
    pets_max_weight_kg: int | None = None
    climb_to_hotel_m: int | None = None
    nearest_m: dict[str, int] = Field(default_factory=dict)
    lat: float
    lon: float
    data_source: str = Field(description="osm | fixture | synthetic — показываем честно")


class Variant(Model):
    id: str
    archetype: Archetype
    title: str
    hotel: HotelCard
    offer: Offer
    transfer_in: TransferQuote | None = None
    transfer_out: TransferQuote | None = None
    outbound: TransportOffer | None = None
    inbound: TransportOffer | None = None
    total_price: int
    budget_delta: int | None = Field(default=None, description="total - budget; <0 значит в бюджете")
    score: float
    breakdown: list[ScoreComponent] = Field(default_factory=list)
    why: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    fit_warnings: list[Issue] = Field(default_factory=list, description="кому этот отель НЕ подойдёт")
    issues: list[Issue] = Field(default_factory=list, description="проблемы состыковок от валидатора")
    matched_prefs: list[PrefKey] = Field(default_factory=list)
    missed_prefs: list[PrefKey] = Field(default_factory=list)


class SearchStats(Model):
    hotels_total: int = 0
    after_hard_filters: int = 0
    with_offers: int = 0
    rejected: dict[str, int] = Field(default_factory=dict, description="причина → сколько отсеяно")
    elapsed_ms: int = 0


class Relaxation(Model):
    """«Если ослабить X — найдётся N вариантов». Считается реальным перезапуском фильтров."""

    label: str
    ops: list[PatchOp]
    results_count: int


class PinnedOutcome(Model):
    """Что стало с отелем, который пользователь уже выбрал, при пересборке."""

    hotel_id: str
    hotel_name: str
    kept: bool
    reason: str | None = Field(default=None, description="почему пришлось заменить (если kept=False)")


class RankingResult(Model):
    variants: list[Variant] = Field(default_factory=list)
    recommended_id: str | None = None
    stats: SearchStats = Field(default_factory=SearchStats)
    relaxations: list[Relaxation] = Field(default_factory=list)
    pinned: PinnedOutcome | None = None
    ticket_issue: str | None = Field(default=None, description="почему не нашлось билетов (если искали)")


class TripState(Model):
    """Состояние сессии. Хранится целиком (JSON) в SessionStore, версионируется на каждом ходу."""

    session_id: str
    user_id: str | None = None
    version: int = 0
    stage: TripStage = TripStage.collecting
    requirements: Requirements = Field(default_factory=Requirements)
    variants: list[Variant] = Field(default_factory=list)
    recommended_variant_id: str | None = None
    previous_variants: list[Variant] = Field(default_factory=list, description="для diff при пересборке")
    selected_variant_id: str | None = None
    last_stats: SearchStats | None = None
    clarifications_asked: int = 0
    asked_paths: list[str] = Field(default_factory=list, description="о чём уже спрашивали — не повторяться")
    dismissed_fixes: list[str] = Field(default_factory=list, description="'<hotel_id>:<code>' — от каких исправлений отказались")
    excluded_hotel_ids: list[str] = Field(
        default_factory=list, description="отели, которые пользователь попросил заменить («предложи другие») — больше не предлагаем"
    )
    cart_id: str | None = None
    order_ids: list[str] = Field(default_factory=list)
    turn_count: int = 0
    created_at: datetime
    updated_at: datetime

    def variant(self, variant_id: str) -> Variant | None:
        return next((v for v in self.variants if v.id == variant_id), None)
