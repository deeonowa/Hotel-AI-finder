"""Каталог: локация, районы, отели, предложения (цены), POI.

Как в реальных B2B-API отелей, СТАТИЧЕСКИЙ контент (Hotel) отделён от ДИНАМИКИ (Offer:
цена/наличие на даты). Поэтому замена нашего demo-источника на реальный API — это новая
реализация портов CatalogProvider и PricingProvider, без изменений агента.
"""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import Field

from .common import BBox, GeoPoint, Model
from .vocab import Amenity, DistrictKind, HotelKind, MealPlan, VibeTag


class ClimateMonth(Model):
    month: int = Field(ge=1, le=12)
    air_temp_day_c: float | None = None
    air_temp_night_c: float | None = None
    sea_temp_c: float | None = None
    precip_days: float | None = None
    source: str = "unknown"


class District(Model):
    id: str
    name: str
    kind: DistrictKind
    center: GeoPoint
    radius_km: float = 3.0
    climate_zone: Literal["coast", "mountain", "city"] = "coast"
    vibe_tags: list[VibeTag] = Field(default_factory=list)
    description: str = ""
    highlights: list[str] = Field(default_factory=list)
    nlu_patterns: list[str] = Field(default_factory=list, description="регулярки (по нормализованному тексту) для NLU на правилах")
    price_factor: float = Field(default=1.0, gt=0, description="для генерации DEMO-цен: район дороже/дешевле среднего")


class TransportHub(Model):
    id: str
    name: str
    kind: Literal["airport", "rail"]
    point: GeoPoint


class PeakPeriod(Model):
    start_mmdd: str = Field(pattern=r"^\d{2}-\d{2}$")
    end_mmdd: str = Field(pattern=r"^\d{2}-\d{2}$")
    factor: float
    label: str


class DataSource(Model):
    name: str
    url: str | None = None
    license: str | None = None
    used_for: str


class EntryRules(Model):
    """Что нужно знать до покупки, если направление за границей. Тексты проверены по датированным источникам."""

    passport: str
    visa: str
    insurance: str | None = None
    money: str | None = None
    visa_free_days: int | None = Field(default=None, description="сколько дней можно без визы за поездку; None — не ограничено")
    sources: list[str] = Field(default_factory=list)
    checked_at: date


class Route(Model):
    """DEMO-маршрут «город отправления → хаб локации» для расписания билетов (transfer/schedule.py)."""

    mode: Literal["plane", "train"]
    hub: str
    minutes: int = Field(gt=0, description="длительность прямого рейса/поезда")
    price: int = Field(gt=0, description="базовая цена за человека в одну сторону, ₽")
    times: list[str] = Field(description="время отправления, HH:MM по часовому поясу города отправления")
    p_direct: float = Field(ge=0, le=1, description="доля прямых рейсов")


class LocationPack(Model):
    """Всё, что нужно агенту о локации. Новая локация = новый каталог data/locations/<id>/."""

    id: str
    name: str
    name_in: str | None = Field(default=None, description="«в Анталье», «в Дубае» — для текстов")
    country: str = "Россия"
    country_code: str = Field(default="RU", pattern=r"^[A-Z]{2}$")
    nlu_patterns: list[str] = Field(default_factory=list, description="регулярки названия города/страны для NLU на правилах")
    tagline: str = ""
    timezone: str
    currency: Literal["RUB"] = "RUB"
    bbox: BBox
    center: GeoPoint
    districts: list[District]
    hubs: list[TransportHub]
    seasonality: dict[str, list[float]] = Field(description="climate_zone → 12 множителей цены")
    peak_periods: list[PeakPeriod] = Field(default_factory=list)
    climate: dict[str, list[ClimateMonth]] = Field(default_factory=dict, description="climate_zone → 12 мес.")
    climate_points: dict[str, dict[str, GeoPoint]] = Field(
        default_factory=dict, description="для scripts/build_climate.py: zone → {air, sea}"
    )
    data_sources: list[DataSource] = Field(default_factory=list)
    entry: EntryRules | None = Field(default=None, description="None — внутренняя поездка, загранпаспорт не нужен")
    routes: dict[str, list[Route]] = Field(default_factory=dict, description="город отправления → DEMO-маршруты")
    stop_cities: list[str] = Field(default_factory=list, description="где пересадка у непрямых рейсов (DEMO)")
    price_index: float = Field(default=1.0, gt=0, description="уровень цен относительно Сочи (для генерации DEMO-цен)")
    meal_price_index: float = Field(
        default=1.0, gt=0, description="стоимость питания относительно Сочи (в Турции «всё включено» заметно дешевле)"
    )
    flat_terrain: bool = Field(
        default=False,
        description="плоский рельеф: «дорогу в гору» не считаем (модель высот включает здания — небоскрёбы дают ложный подъём)",
    )
    meal_plans: dict[str, list[MealPlan]] = Field(
        default_factory=dict, description="тип объекта → типичные планы питания (для генерации), напр. AI в Анталье"
    )

    @property
    def is_abroad(self) -> bool:
        return self.entry is not None

    @property
    def label_in(self) -> str:
        return self.name_in or f"в локации «{self.name}»"

    def district(self, district_id: str) -> District | None:
        return next((d for d in self.districts if d.id == district_id), None)

    def hub(self, hub_id: str) -> TransportHub | None:
        return next((h for h in self.hubs if h.id == hub_id), None)


class RoomType(Model):
    id: str
    name: str
    max_adults: int = Field(ge=1)
    max_children: int = Field(ge=0, default=0)
    max_occupancy: int = Field(ge=1)
    area_m2: int | None = None
    view: Literal["sea", "mountain", "city", "garden", "none"] = "none"
    price_factor: float = Field(default=1.0, gt=0, description="относительно базовой цены отеля")


class PetPolicy(Model):
    allowed: bool | None = Field(default=None, description="None = неизвестно (НЕ считать разрешённым)")
    fee_per_night: int | None = None
    max_weight_kg: int | None = None


class Provenance(Model):
    source: Literal["osm", "fixture", "synthetic", "partner_api"]
    source_id: str | None = None
    synthetic_fields: list[str] = Field(default_factory=list, description="какие поля сгенерированы")


class Hotel(Model):
    id: str
    name: str
    kind: HotelKind
    location_id: str
    district_id: str
    point: GeoPoint
    address: str | None = None
    stars: int | None = Field(default=None, ge=0, le=5)
    rating: float | None = Field(default=None, ge=0, le=10)
    review_count: int = 0
    amenities: list[Amenity] = Field(default_factory=list)
    vibe_tags: list[VibeTag] = Field(default_factory=list)
    check_in_time: time = time(14, 0)
    check_out_time: time = time(12, 0)
    early_check_in_fee_pct: int | None = Field(default=50, description="% от цены ночи; None = нельзя")
    adults_only: bool = False
    pets: PetPolicy = Field(default_factory=PetPolicy)
    distance_to_sea_m: int | None = None
    beach_walk_minutes: int | None = None
    hilly_walk: bool | None = Field(default=None, description="дорога к морю в гору — важно с детьми/коляской")
    distance_to_center_m: int | None = None
    meal_plans: list[MealPlan] = Field(default_factory=lambda: [MealPlan.RO, MealPlan.BB])
    room_types: list[RoomType]
    base_price_per_night: int = Field(gt=0, description="ориентир для DEMO-цен, стандарт DBL, низкий сезон")
    description: str | None = None
    website: str | None = None
    elevation_m: int | None = Field(default=None, description="высота над морем, м (Open-Meteo, реальная)")
    climb_to_hotel_m: int | None = Field(default=None, description="подъём от ближайшего пляжа до отеля, м")
    nearest_m: dict[str, int] = Field(
        default_factory=dict,
        description="расстояние до ближайших POI (реальное, OSM): beach, water_park, park, ski_lift, playground, sight",
    )
    provenance: Provenance


class CancellationPolicy(Model):
    refundable: bool
    free_until: date | None = None
    penalty_pct_after: int = Field(default=100, ge=0, le=100)


class Offer(Model):
    """Конкретное предложение на даты. Цена всегда DEMO (is_demo_price=True), пока нет партнёрского API."""

    offer_id: str
    hotel_id: str
    room_type_id: str
    room_name: str
    check_in: date
    check_out: date
    nights: int
    adults: int
    children_ages: list[int] = Field(default_factory=list)
    meal_plan: MealPlan
    cancellation: CancellationPolicy
    price_total: int
    price_per_night_avg: int
    pet_fee_total: int = 0
    is_demo_price: bool = True


class POI(Model):
    id: str
    name: str
    kind: Literal["beach", "park", "water_park", "attraction", "ski_lift", "playground", "museum", "viewpoint", "zoo"]
    point: GeoPoint
    district_id: str | None = None
    tags: list[str] = Field(default_factory=list)
