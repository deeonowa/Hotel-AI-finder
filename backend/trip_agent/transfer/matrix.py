"""Трансфер по реальной матрице «хаб → отель» (OSRM, собрана scripts/build_location.py). Владелец: ML2.

OSRM считает время без пробок, поэтому умножаем на коэффициент загруженности дорог
(побережье Сочи — заметные пробки, горная дорога — почти нет) и добавляем время на подачу.
Нет записи в матрице → откат на оценку по прямой (HaversineTransfer).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..schemas import Hotel, LocationPack, TransferQuote
from .estimator import TAXI_BASE, TAXI_PER_KM, HaversineTransfer

TRAFFIC_FACTOR = {"sea": 1.35, "mountain": 1.1}
PICKUP_MIN = 8


class MatrixTransfer:
    def __init__(self, data_dir: Path, fallback: HaversineTransfer | None = None):
        self.data_dir = Path(data_dir)
        self.fallback = fallback or HaversineTransfer()
        self._m: dict[str, dict[str, dict[str, dict]]] = {}

    def _matrix(self, location_id: str) -> dict[str, dict[str, dict]]:
        if location_id not in self._m:
            p = self.data_dir / "locations" / location_id / "transfers.json"
            self._m[location_id] = json.loads(p.read_text("utf-8"))["transfers"] if p.exists() else {}
        return self._m[location_id]

    def estimate(self, location: LocationPack, hub_id: str, hotel: Hotel, direction: str) -> TransferQuote:
        row = self._matrix(location.id).get(hotel.id, {}).get(hub_id)
        hub = location.hub(hub_id)
        if row is None or hub is None:
            return self.fallback.estimate(location, hub_id, hotel, direction)
        district = location.district(hotel.district_id)
        kind = district.kind.value if district else "sea"
        minutes = int(round(row["min"] * TRAFFIC_FACTOR[kind] + PICKUP_MIN))
        km = float(row["km"])
        return TransferQuote(
            hub_id=hub.id,
            hub_name=hub.name,
            hotel_id=hotel.id,
            direction=direction,
            minutes=minutes,
            distance_km=round(km, 1),
            price=int(round(TAXI_BASE + TAXI_PER_KM * km, -1)),
            mode="taxi",
            is_estimate=True,
        )
