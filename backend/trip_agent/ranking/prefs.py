"""Как каждое предпочтение (PrefKey) проверяется жёстко и оценивается мягко. Владелец: ML2.

Добавили PrefKey в vocab → ОБЯЗАТЕЛЬНО добавьте его сюда (тест test_every_pref_has_handler упадёт).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..schemas import Amenity, District, DistrictKind, Hotel, MealPlan, Offer, PrefKey, VibeTag


@dataclass
class Ctx:
    hotel: Hotel
    district: District | None
    offer: Offer | None = None
    transfer_minutes: int | None = None
    has_kids: bool = False


def _has(h: Hotel, *a: Amenity) -> bool:
    return any(x in h.amenities for x in a)


def _sea(ctx: Ctx, value: float | None) -> float:
    d = ctx.hotel.distance_to_sea_m
    if d is None:
        return 0.0 if ctx.district and ctx.district.kind == DistrictKind.mountain else 0.3
    limit = float(value) if value else 600.0
    return 1.0 if d <= limit else max(0.0, 1.0 - (d - limit) / 2000)


def _near(h: Hotel, key: str) -> int | None:
    """Реальное расстояние до ближайшего POI (OSM, build_location.py) или None."""
    return h.nearest_m.get(key)


def _climb(h: Hotel) -> int:
    """Подъём от пляжа до отеля, м (реальные высоты). Нет данных → по флагу hilly_walk."""
    if h.climb_to_hotel_m is not None:
        return h.climb_to_hotel_m
    return 30 if h.hilly_walk else 0


def _ski(c: Ctx) -> float:
    if _has(c.hotel, Amenity.ski_in_out):
        return 1.0
    lift = _near(c.hotel, "ski_lift")
    mountain = bool(c.district and c.district.kind == DistrictKind.mountain)
    if lift is not None and mountain:
        return 0.95 if lift <= 400 else 0.8 if lift <= 1200 else 0.6
    return 0.5 if mountain else 0.0


def _family(c: Ctx) -> float:
    h = c.hotel
    if h.adults_only:
        return 0.0
    s = 0.2
    s += 0.35 * _has(h, Amenity.kids_club) + 0.15 * _has(h, Amenity.kids_menu)
    pg = _near(h, "playground")
    s += 0.15 if _has(h, Amenity.playground) or (pg is not None and pg <= 400) else 0.0
    wp = _near(h, "water_park")
    s += 0.1 if wp is not None and wp <= 2500 else 0.0
    s += 0.1 * (VibeTag.family in h.vibe_tags)
    s -= min(0.35, _climb(h) / 120)  # подъём с ребёнком/коляской
    s -= 0.25 * (VibeTag.party in h.vibe_tags)
    return max(0.0, min(1.0, s))


def _meal_score(offer: Offer | None, wanted: set[MealPlan]) -> float:
    if offer is None:
        return 0.5
    return 1.0 if offer.meal_plan in wanted else 0.0


# PrefKey → (мягкая оценка 0..1, жёсткая проверка или None если «must» невозможен на уровне отеля)
Scorer = Callable[[Ctx, float | int | str | None], float]

SCORERS: dict[PrefKey, Scorer] = {
    PrefKey.near_sea: lambda c, v: _sea(c, v),
    PrefKey.beach_first_line: lambda c, v: _sea(c, 150),
    PrefKey.sea_view: lambda c, v: 1.0 if any(r.view == "sea" for r in c.hotel.room_types) else 0.0,
    PrefKey.mountains: lambda c, v: 1.0 if c.district and c.district.kind == DistrictKind.mountain else 0.0,
    PrefKey.ski_access: lambda c, v: _ski(c),
    PrefKey.quiet: lambda c, v: 1.0 if VibeTag.quiet in c.hotel.vibe_tags else 0.15 if VibeTag.party in c.hotel.vibe_tags else 0.5,
    PrefKey.nightlife: lambda c, v: 1.0 if VibeTag.party in c.hotel.vibe_tags or (c.district and c.district.id == "central") else 0.25,
    PrefKey.city_center: lambda c, v: max(0.0, 1.0 - (c.hotel.distance_to_center_m or 10_000) / 5000),
    PrefKey.family_friendly: lambda c, v: _family(c),
    PrefKey.kids_club: lambda c, v: 1.0 if _has(c.hotel, Amenity.kids_club) else 0.0,
    PrefKey.pool: lambda c, v: 1.0 if _has(c.hotel, Amenity.pool, Amenity.heated_pool, Amenity.indoor_pool) else 0.0,
    PrefKey.heated_pool: lambda c, v: (
        1.0 if _has(c.hotel, Amenity.heated_pool, Amenity.indoor_pool) else 0.3 if _has(c.hotel, Amenity.pool) else 0.0
    ),
    PrefKey.spa: lambda c, v: 1.0 if _has(c.hotel, Amenity.spa) else 0.5 if _has(c.hotel, Amenity.sauna) else 0.0,
    PrefKey.romantic: lambda c, v: (
        1.0 if c.hotel.adults_only or VibeTag.romantic in c.hotel.vibe_tags else 0.6 if VibeTag.luxury in c.hotel.vibe_tags else 0.2
    ),
    PrefKey.breakfast_included: lambda c, v: _meal_score(c.offer, {MealPlan.BB, MealPlan.HB, MealPlan.FB, MealPlan.AI}),
    PrefKey.all_inclusive: lambda c, v: (
        1.0 if c.offer and c.offer.meal_plan == MealPlan.AI else 0.6 if c.offer and c.offer.meal_plan == MealPlan.FB else 0.0
    ),
    PrefKey.free_cancellation: lambda c, v: 1.0 if c.offer and c.offer.cancellation.refundable else 0.0,
    PrefKey.pet_friendly: lambda c, v: 1.0 if c.hotel.pets.allowed else 0.0,
    PrefKey.parking: lambda c, v: 1.0 if _has(c.hotel, Amenity.parking) else 0.0,
    PrefKey.accessible: lambda c, v: (
        0.0
        if _climb(c.hotel) >= 25
        else 1.0
        if _has(c.hotel, Amenity.wheelchair_accessible)
        else 0.5
        if _has(c.hotel, Amenity.elevator)
        else 0.1
    ),
    PrefKey.short_transfer: lambda c, v: (
        0.5 if c.transfer_minutes is None else max(0.0, 1.0 - c.transfer_minutes / (float(v) * 2 if v else 120))
    ),
    PrefKey.high_rating: lambda c, v: max(0.0, min(1.0, ((c.hotel.rating or 6) - 6) / 4)),
    PrefKey.min_stars: lambda c, v: 1.0 if (c.hotel.stars or 0) >= float(v or 4) else (c.hotel.stars or 0) / float(v or 4),
    PrefKey.budget_value: lambda c, v: 0.5,  # учитывается весом цены, а не здесь
    PrefKey.luxury: lambda c, v: (
        1.0 if (c.hotel.stars or 0) >= 5 else 0.6 if VibeTag.luxury in c.hotel.vibe_tags or (c.hotel.stars or 0) == 4 else 0.1
    ),
    PrefKey.work_friendly: lambda c, v: 1.0 if _has(c.hotel, Amenity.workspace) else 0.5 if _has(c.hotel, Amenity.wifi) else 0.0,
    PrefKey.nature: lambda c, v: (
        1.0 if VibeTag.nature in c.hotel.vibe_tags or (c.district and VibeTag.nature in c.district.vibe_tags) else 0.3
    ),
    PrefKey.wellness: lambda c, v: 1.0 if _has(c.hotel, Amenity.medical_treatments) else 0.6 if _has(c.hotel, Amenity.spa) else 0.0,
}

# Для «must» на уровне ОТЕЛЯ (до запроса цен): порог мягкой оценки.
# Не входят: предпочтения уровня предложения (питание, отмена) и «настроение» (quiet, romantic…) —
# «настроение» не бывает жёстким: при must оно просто получает максимальный вес.
HARD_AT_HOTEL_LEVEL: dict[PrefKey, float] = {
    PrefKey.near_sea: 1.0,
    PrefKey.beach_first_line: 1.0,
    PrefKey.sea_view: 1.0,
    PrefKey.mountains: 1.0,
    PrefKey.ski_access: 0.6,
    PrefKey.kids_club: 1.0,
    PrefKey.pool: 1.0,
    PrefKey.heated_pool: 1.0,
    PrefKey.spa: 1.0,
    PrefKey.pet_friendly: 1.0,
    PrefKey.parking: 1.0,
    PrefKey.accessible: 1.0,
    PrefKey.min_stars: 1.0,
    PrefKey.high_rating: 0.5,
    PrefKey.wellness: 0.6,
}
HARD_AT_OFFER_LEVEL: dict[PrefKey, float] = {
    PrefKey.breakfast_included: 1.0,
    PrefKey.all_inclusive: 1.0,
    PrefKey.free_cancellation: 1.0,
}
