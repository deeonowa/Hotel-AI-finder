"""DEMO-цены и наличие. Владелец: ML2.

Цена считается на лету и ДЕТЕРМИНИРОВАННО (хэш от отеля и даты), поэтому:
  * работают любые даты, без хранения календаря на год вперёд;
  * один и тот же запрос всегда даёт одну и ту же цену (тесты, eval, демо стабильны).

Модель: base * сезон(климатическая зона, месяц) * праздники * выходные * номер * размещение
        + питание на человека + шум ±7%. Возвратный тариф дороже невозвратного.
Всё помечается is_demo_price=True — честно для демо.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from ..schemas import CancellationPolicy, Hotel, LocationPack, MealPlan, Offer, Requirements, RoomType

MEAL_PER_PERSON = {MealPlan.RO: 0, MealPlan.BB: 700, MealPlan.HB: 1800, MealPlan.FB: 2800, MealPlan.AI: 4500}
CHILD_MEAL_FACTOR = 0.5
REFUNDABLE_MARKUP = 1.10


def _u(*parts: object) -> float:
    """Детерминированное «случайное» число в [0, 1)."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def room_split(adults: int, kids: int, rooms: int) -> tuple[int, int]:
    """Сколько взрослых и детей приходится на самый «плотный» номер при размещении в rooms номерах."""
    return -(-adults // rooms), -(-kids // rooms)


def _in_peak(d: date, start_mmdd: str, end_mmdd: str) -> bool:
    md = d.strftime("%m-%d")
    return start_mmdd <= md <= end_mmdd if start_mmdd <= end_mmdd else (md >= start_mmdd or md <= end_mmdd)


class DemoPricing:
    def __init__(self, location: LocationPack, seed: str = "demo-v1"):
        self.location = location
        self.seed = seed
        self._night_cache: dict[tuple[str, str, date], int | None] = {}

    def _zone(self, hotel: Hotel) -> str:
        d = self.location.district(hotel.district_id)
        return d.climate_zone if d else "coast"

    def night_price(self, hotel: Hotel, room: RoomType, night: date) -> int | None:
        """Цена ночи за номер (без питания) или None, если продано. Детерминирована → кэшируем."""
        key = (hotel.id, room.id, night)
        if key not in self._night_cache:
            if len(self._night_cache) > 500_000:
                self._night_cache.clear()
            self._night_cache[key] = self._night_price(hotel, room, night)
        return self._night_cache[key]

    def _night_price(self, hotel: Hotel, room: RoomType, night: date) -> int | None:
        season = self.location.seasonality.get(self._zone(hotel), [1.0] * 12)[night.month - 1]
        peak = max((p.factor for p in self.location.peak_periods if _in_peak(night, p.start_mmdd, p.end_mmdd)), default=1.0)
        weekend = 1.15 if night.weekday() in (4, 5) else 1.0
        # в высокий сезон чаще продано
        soldout_p = min(0.35, 0.04 + 0.12 * max(season * peak - 1.0, 0))
        if _u(self.seed, "avail", hotel.id, room.id, night) < soldout_p:
            return None
        noise = 0.93 + 0.14 * _u(self.seed, "noise", hotel.id, night)
        return int(hotel.base_price_per_night * room.price_factor * season * peak * weekend * noise)

    def quote(self, hotel: Hotel, req: Requirements, check_in: date, check_out: date) -> list[Offer]:
        adults = req.guests.adults or 2
        kids = req.guests.children_ages
        nights = (check_out - check_in).days
        if nights <= 0:
            return []
        rooms = max(1, req.guests.rooms or 1)
        a_room, k_room = room_split(adults, len(kids), rooms)
        offers: list[Offer] = []
        for room in hotel.room_types:
            if a_room > room.max_adults or k_room > room.max_children or a_room + k_room > room.max_occupancy:
                continue
            per_night: list[int] = []
            for i in range(nights):
                p = self.night_price(hotel, room, check_in + timedelta(days=i))
                if p is None:
                    break
                per_night.append(p)
            if len(per_night) != nights:
                continue  # нет наличия на все ночи
            extra_people = max(0, a_room + k_room - 2)
            room_total = int(sum(per_night) * (1 + 0.18 * extra_people)) * rooms
            pet_fee = (hotel.pets.fee_per_night or 0) * nights if req.guests.pets and hotel.pets.allowed else 0
            for meal in hotel.meal_plans:
                persons = adults + CHILD_MEAL_FACTOR * sum(1 for a in kids if a >= 3)
                meal_total = int(
                    MEAL_PER_PERSON[meal] * self.location.meal_price_index * persons * nights * (1 + 0.15 * max((hotel.stars or 3) - 3, 0))
                )
                for refundable in (False, True):
                    total = int((room_total + meal_total) * (REFUNDABLE_MARKUP if refundable else 1.0)) + pet_fee
                    total = int(round(total, -2))
                    offers.append(
                        Offer(
                            offer_id=f"of-{hotel.id}-{room.id}-{meal.value}-{'r' if refundable else 'n'}-{check_in:%Y%m%d}-{nights}",
                            hotel_id=hotel.id,
                            room_type_id=room.id,
                            room_name=room.name if rooms == 1 else f"{room.name} × {rooms}",
                            check_in=check_in,
                            check_out=check_out,
                            nights=nights,
                            adults=adults,
                            children_ages=list(kids),
                            meal_plan=meal,
                            cancellation=CancellationPolicy(
                                refundable=refundable,
                                free_until=(check_in - timedelta(days=3)) if refundable else None,
                                penalty_pct_after=100,
                            ),
                            price_total=total,
                            price_per_night_avg=total // nights,
                            pet_fee_total=pet_fee,
                        )
                    )
        return offers
