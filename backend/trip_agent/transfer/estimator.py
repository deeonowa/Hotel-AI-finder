"""Оценка трансфера «хаб → отель». Владелец: ML2.

Baseline: расстояние по прямой × коэффициент извилистости дороги / средняя скорость.
Улучшение (ML2): предрасчитать реальную матрицу хаб×отель через OSRM в build-скрипте
и класть в hotels.json — порт TransferProvider не изменится.
"""

from __future__ import annotations

from ..geo import haversine_m
from ..schemas import Hotel, LocationPack, TransferQuote

ROAD_FACTOR = {"sea": 1.35, "mountain": 1.55}
SPEED_KMH = {"sea": 32.0, "mountain": 48.0}  # прибрежная трасса с пробками vs. горная дорога без светофоров
TAXI_BASE, TAXI_PER_KM = 350, 32


class HaversineTransfer:
    def estimate(self, location: LocationPack, hub_id: str, hotel: Hotel, direction: str) -> TransferQuote:
        hub = location.hub(hub_id)
        if hub is None:
            raise KeyError(f"unknown hub {hub_id}")
        district = location.district(hotel.district_id)
        kind = district.kind.value if district else "sea"
        km = haversine_m(hub.point, hotel.point) / 1000 * ROAD_FACTOR[kind]
        minutes = int(round(km / SPEED_KMH[kind] * 60 + 8))  # +8 мин на посадку/подъезд
        price = int(round(TAXI_BASE + TAXI_PER_KM * km, -1))
        return TransferQuote(
            hub_id=hub.id,
            hub_name=hub.name,
            hotel_id=hotel.id,
            direction=direction,
            minutes=minutes,
            distance_km=round(km, 1),
            price=price,
            mode="taxi",
            is_estimate=True,
        )


def default_hub(location: LocationPack, hotel: Hotel, arrival_mode: str) -> str:
    """Самолёт → аэропорт; поезд → ближайшая к отелю ж/д станция; иначе — аэропорт."""
    if arrival_mode == "train":
        rails = [h for h in location.hubs if h.kind == "rail"]
        if rails:
            return min(rails, key=lambda h: haversine_m(h.point, hotel.point)).id
    airports = [h for h in location.hubs if h.kind == "airport"]
    return airports[0].id if airports else location.hubs[0].id
