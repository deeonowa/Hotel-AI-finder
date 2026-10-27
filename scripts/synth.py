"""Детерминированное синтетическое обогащение отелей (общая логика для фикстур и OSM).

Реальное (из OSM): название, тип, координаты, звёзды и часть удобств — если есть в тегах.
Синтетика (seed по id отеля → одинаково при каждом запуске): типы номеров, базовая цена,
рейтинг, правила для животных, ранний заезд, питание, «в гору ли идти к морю».
Список синтетических полей пишется в provenance.synthetic_fields — честно для демо.
"""

from __future__ import annotations

import math
import random
from typing import Any

KIND_PRICE = {  # (min, max) базовой цены за ночь, стандарт DBL, низкий сезон, ₽
    "hostel": (1200, 2200),
    "guest_house": (2500, 4800),
    "apartment": (3500, 7500),
    "motel": (2500, 4000),
    "sanatorium": (5500, 10000),
    "chalet": (9000, 18000),
}
STARS_PRICE = {0: (3000, 5000), 1: (2500, 4000), 2: (3200, 5000), 3: (4800, 7500), 4: (7500, 13000), 5: (14000, 32000)}

MEALS = {
    "hostel": ["RO"],
    "guest_house": ["RO"],
    "apartment": ["RO"],
    "motel": ["RO"],
    "hotel": ["RO", "BB", "HB"],
    "resort": ["BB", "HB", "AI"],
    "sanatorium": ["FB"],
    "chalet": ["RO", "BB"],
}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _rooms(kind: str, rng: random.Random, near_sea: bool, mountain: bool) -> list[dict[str, Any]]:
    view = "sea" if near_sea else ("mountain" if mountain else "city")
    if kind == "hostel":
        return [
            {
                "id": "dorm",
                "name": "Место в общем номере",
                "max_adults": 1,
                "max_children": 0,
                "max_occupancy": 1,
                "area_m2": None,
                "view": "none",
                "price_factor": 0.55,
            },
            {
                "id": "private",
                "name": "Двухместный номер",
                "max_adults": 2,
                "max_children": 0,
                "max_occupancy": 2,
                "area_m2": 12,
                "view": "none",
                "price_factor": 1.0,
            },
        ]
    if kind == "apartment":
        return [
            {
                "id": "studio",
                "name": "Студия",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "area_m2": rng.choice([24, 28, 32]),
                "view": view if rng.random() < 0.4 else "none",
                "price_factor": 1.0,
            },
            {
                "id": "two_room",
                "name": "Апартаменты с 2 спальнями",
                "max_adults": 4,
                "max_children": 2,
                "max_occupancy": 6,
                "area_m2": rng.choice([48, 55, 62]),
                "view": "none",
                "price_factor": 1.65,
            },
        ]
    if kind in ("guest_house", "motel"):
        return [
            {
                "id": "dbl",
                "name": "Двухместный номер",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "area_m2": rng.choice([14, 16, 18]),
                "view": "none",
                "price_factor": 1.0,
            },
            {
                "id": "trpl",
                "name": "Трёхместный номер",
                "max_adults": 3,
                "max_children": 1,
                "max_occupancy": 4,
                "area_m2": rng.choice([20, 22]),
                "view": "none",
                "price_factor": 1.3,
            },
        ]
    if kind == "chalet":
        return [
            {
                "id": "chalet",
                "name": "Шале целиком",
                "max_adults": 6,
                "max_children": 3,
                "max_occupancy": 8,
                "area_m2": rng.choice([80, 110, 140]),
                "view": "mountain",
                "price_factor": 2.2,
            },
            {
                "id": "dbl",
                "name": "Номер в шале",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "area_m2": 22,
                "view": "mountain",
                "price_factor": 1.0,
            },
        ]
    rooms = [
        {
            "id": "std",
            "name": "Стандарт",
            "max_adults": 2,
            "max_children": 1,
            "max_occupancy": 3,
            "area_m2": rng.choice([18, 20, 22, 25]),
            "view": "city" if not mountain else "mountain",
            "price_factor": 1.0,
        },
        {
            "id": "family",
            "name": "Семейный номер",
            "max_adults": 2,
            "max_children": 2,
            "max_occupancy": 4,
            "area_m2": rng.choice([30, 34, 38]),
            "view": "none",
            "price_factor": 1.4,
        },
    ]
    if near_sea or mountain:
        rooms.append(
            {
                "id": "view",
                "name": "Номер с видом",
                "max_adults": 2,
                "max_children": 1,
                "max_occupancy": 3,
                "area_m2": rng.choice([22, 25, 28]),
                "view": view,
                "price_factor": 1.25,
            }
        )
    if rng.random() < 0.6:
        rooms.append(
            {
                "id": "suite",
                "name": "Люкс",
                "max_adults": 2,
                "max_children": 2,
                "max_occupancy": 4,
                "area_m2": rng.choice([40, 50, 60]),
                "view": view,
                "price_factor": 2.0,
            }
        )
    return rooms


def _amenities(kind: str, stars: int, district: dict, rng: random.Random, near_sea: bool, known: set[str]) -> list[str]:
    a = set(known) | {"wifi"}
    mountain = district["kind"] == "mountain"
    fam = "family" in district.get("vibe_tags", [])
    p = lambda base: base + 0.08 * max(stars - 3, 0)  # noqa: E731

    if kind in ("hotel", "resort", "sanatorium"):
        a |= {"restaurant"}
        if rng.random() < p(0.45):
            a.add("pool")
        if "pool" in a and rng.random() < 0.4:
            a.add("heated_pool")
        if rng.random() < p(0.25) or kind == "resort":
            a.add("spa")
        if rng.random() < p(0.35):
            a.add("gym")
        if rng.random() < (0.45 if fam else 0.25) or kind == "resort":
            a |= {"kids_club", "kids_menu"}
        if rng.random() < 0.6:
            a.add("playground")
        if rng.random() < 0.7:
            a.add("elevator")
        if rng.random() < 0.5:
            a.add("bar")
        if rng.random() < 0.25:
            a.add("airport_shuttle")
        if near_sea and rng.random() < (0.5 if stars >= 4 else 0.15):
            a.add("private_beach")
        if not near_sea and not mountain and rng.random() < 0.3:
            a.add("beach_shuttle")
        if kind == "sanatorium":
            a |= {"medical_treatments", "pool", "spa"}
        if "elevator" in a and rng.random() < 0.35:
            a.add("wheelchair_accessible")
        if rng.random() < 0.3:
            a.add("workspace")
    else:
        if rng.random() < 0.45:
            a.add("kitchen")
        if rng.random() < 0.3:
            a.add("playground")
        if kind == "guest_house" and rng.random() < 0.2:
            a.add("pool")
    if rng.random() < (0.75 if kind in ("guest_house", "chalet", "apartment") else 0.5):
        a.add("parking")
    if mountain:
        if rng.random() < 0.7:
            a.add("ski_storage")
        if stars >= 4 and rng.random() < 0.3:
            a.add("ski_in_out")
    if rng.random() < 0.3:
        a.add("laundry")
    return sorted(a)


def _vibes(kind: str, stars: int, district: dict, amenities: list[str], adults_only: bool) -> list[str]:
    v = set()
    dv = set(district.get("vibe_tags", []))
    if "kids_club" in amenities or "playground" in amenities:
        v.add("family")
    if stars >= 5 or kind in ("resort", "chalet"):
        v.add("luxury")
    if kind in ("hostel", "guest_house") or stars <= 2:
        v.add("budget")
    if adults_only or ("romantic" in dv and stars >= 4):
        v.add("romantic")
    if kind == "sanatorium" or "spa" in amenities:
        v.add("wellness")
    if "quiet" in dv and kind != "hostel":
        v.add("quiet")
    if "party" in dv and kind in ("hostel", "hotel"):
        v.add("party")
    if district["kind"] == "mountain":
        v |= {"nature", "sport"}
    if "workspace" in amenities:
        v.add("business")
    if "family" in v:
        v.discard("party")
    return sorted(v)


def enrich(base: dict[str, Any], district: dict, seed: str = "v1", location: dict | None = None) -> dict[str, Any]:
    """base: id, name, kind, location_id, district_id, point, [stars], [amenities_known], [distance_to_sea_m],
    [address], provenance{source, source_id}. Возвращает полный dict под схему Hotel.

    location (location.json) задаёт уровень цен страны (price_index) и типичное питание (meal_plans),
    district — коэффициент цены района (price_factor)."""
    location = location or {}
    rng = random.Random(f"{seed}:{base['id']}")
    kind = base["kind"]
    synthetic: list[str] = []
    mountain = district["kind"] == "mountain"

    stars = base.get("stars")
    if stars is None and kind in ("hotel", "resort", "sanatorium", "chalet"):
        stars = {"resort": rng.choice([4, 5]), "sanatorium": rng.choice([3, 4]), "chalet": rng.choice([3, 4])}.get(
            kind, rng.choices([2, 3, 4, 5], weights=[1, 4, 3, 1])[0]
        )
        synthetic.append("stars")
    s = stars or 0

    dist_sea = base.get("distance_to_sea_m")
    near_sea = dist_sea is not None and dist_sea <= 600
    amenities = _amenities(kind, s, district, rng, near_sea, set(base.get("amenities_known", [])))
    synthetic.append("amenities(частично)")

    lo, hi = KIND_PRICE.get(kind) or STARS_PRICE.get(s, (4000, 7000))
    if kind == "resort":
        lo, hi = STARS_PRICE[max(s, 4)]
        lo, hi = int(lo * 1.2), int(hi * 1.2)
    price = rng.randint(lo, hi)
    if dist_sea is not None:  # близость к морю дороже
        price = int(price * (1.25 if dist_sea < 200 else 1.1 if dist_sea < 600 else 1.0 if dist_sea < 1500 else 0.88))
    if district.get("price_factor", 1.0) != 1.0:  # центр дороже, окраины дешевле (location.json)
        price = int(price * district["price_factor"])
    if location.get("price_index", 1.0) != 1.0:  # уровень цен страны относительно Сочи
        price = int(price * location["price_index"])
    base_price = int(round(price, -2))
    synthetic.append("base_price_per_night")

    adults_only = kind in ("hotel", "resort") and s >= 4 and rng.random() < 0.08
    pets_roll = rng.random()
    pet_ok_p = 0.45 if kind in ("guest_house", "apartment", "chalet") else 0.25
    if pets_roll < pet_ok_p:
        pets = {"allowed": True, "fee_per_night": rng.choice([0, 500, 700, 1000]), "max_weight_kg": rng.choice([None, 10, 20])}
    elif pets_roll < pet_ok_p + 0.15:
        pets = {"allowed": None, "fee_per_night": None, "max_weight_kg": None}  # неизвестно
    else:
        pets = {"allowed": False, "fee_per_night": None, "max_weight_kg": None}
    synthetic.append("pets")

    check_in = (
        rng.choice(["14:00", "14:00", "15:00"]) if kind not in ("guest_house", "apartment") else rng.choice(["12:00", "13:00", "14:00"])
    )
    check_out = "12:00" if check_in != "15:00" else "11:00"
    early_fee = rng.choice([50, 50, 100, None]) if kind != "hostel" else None
    synthetic += ["check_in_time", "check_out_time", "early_check_in_fee_pct"]

    hilly = base.get("hilly_walk")  # реальное значение из высот (build_location.py), если есть
    beach_walk = base.get("beach_walk_minutes")
    if dist_sea is not None and beach_walk is None:
        beach_walk = max(1, round(dist_sea / 75))  # ~4.5 км/ч по ровному
    if dist_sea is not None and hilly is None:
        hilly = False if district["id"] == "sirius" else (dist_sea > 300 and rng.random() < 0.55)
        synthetic.append("hilly_walk")

    rating = round(min(9.8, max(6.2, rng.gauss(7.6 + 0.35 * (s - 3), 0.6))), 1)
    reviews = int(rng.lognormvariate(4.5, 1.0))
    synthetic += ["rating", "review_count", "room_types", "meal_plans"]

    hotel = {
        "id": base["id"],
        "name": base["name"],
        "kind": kind,
        "location_id": base["location_id"],
        "district_id": base["district_id"],
        "point": base["point"],
        "address": base.get("address"),
        "stars": stars,
        "rating": rating,
        "review_count": reviews,
        "amenities": amenities,
        "vibe_tags": _vibes(kind, s, district, amenities, adults_only),
        "check_in_time": check_in,
        "check_out_time": check_out,
        "early_check_in_fee_pct": early_fee,
        "adults_only": adults_only,
        "pets": pets,
        "distance_to_sea_m": dist_sea,
        "beach_walk_minutes": beach_walk,
        "hilly_walk": hilly,
        "distance_to_center_m": base.get("distance_to_center_m"),
        "meal_plans": (location.get("meal_plans") or {}).get(kind) or MEALS.get(kind, ["RO", "BB"]),
        "room_types": _rooms(kind, rng, near_sea, mountain),
        "base_price_per_night": base_price,
        "description": base.get("description"),
        "website": base.get("website"),
        "elevation_m": base.get("elevation_m"),
        "climb_to_hotel_m": base.get("climb_to_hotel_m"),
        "nearest_m": base.get("nearest_m") or {},
        "provenance": {
            "source": base["provenance"]["source"],
            "source_id": base["provenance"].get("source_id"),
            "synthetic_fields": synthetic,
        },
    }
    if adults_only:
        hotel["vibe_tags"] = sorted(set(hotel["vibe_tags"]) - {"family"})
        hotel["amenities"] = [x for x in hotel["amenities"] if x not in ("kids_club", "kids_menu", "playground")]
    return hotel
