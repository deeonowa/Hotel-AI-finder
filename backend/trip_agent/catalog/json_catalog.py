"""CatalogProvider поверх Location Pack (JSON-файлы в data/locations/<id>/). Владелец: ML2.

Новая локация = новая папка с location.json + hotels.json + pois.json. Код не меняется.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path

from ..schemas import POI, ClimateMonth, Hotel, LocationPack


class JsonCatalog:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._locations: dict[str, LocationPack] = {}
        self._hotels: dict[str, list[Hotel]] = {}
        self._pois: dict[str, list[POI]] = {}
        self._meta: dict[str, dict] = {}

    # --- загрузка ---------------------------------------------------------
    def _dir(self, location_id: str) -> Path:
        d = self.data_dir / "locations" / location_id
        if not d.is_dir():
            raise KeyError(f"location pack not found: {d}")
        return d

    def _load(self, location_id: str) -> None:
        if location_id in self._locations:
            return
        d = self._dir(location_id)
        self._locations[location_id] = LocationPack.model_validate_json((d / "location.json").read_text("utf-8"))
        raw = json.loads((d / "hotels.json").read_text("utf-8"))
        self._meta[location_id] = raw.get("meta", {})
        self._hotels[location_id] = [Hotel.model_validate(h) for h in raw["hotels"]]
        pois_path = d / "pois.json"
        self._pois[location_id] = (
            [POI.model_validate(p) for p in json.loads(pois_path.read_text("utf-8"))["pois"]] if pois_path.exists() else []
        )

    @cached_property
    def _by_id(self) -> dict[str, Hotel]:
        return {h.id: h for hs in self._hotels.values() for h in hs}

    # --- порт CatalogProvider ----------------------------------------------
    def get_location(self, location_id: str) -> LocationPack:
        self._load(location_id)
        return self._locations[location_id]

    def list_hotels(self, location_id: str) -> Sequence[Hotel]:
        self._load(location_id)
        return self._hotels[location_id]

    def get_hotel(self, hotel_id: str) -> Hotel | None:
        hit = self._by_id.get(hotel_id)
        if hit is not None:
            return hit
        self.__dict__.pop("_by_id", None)  # могли загрузить новую локацию
        return self._by_id.get(hotel_id)

    def list_pois(self, location_id: str, kinds: Sequence[str] | None = None) -> Sequence[POI]:
        self._load(location_id)
        pois = self._pois[location_id]
        return [p for p in pois if kinds is None or p.kind in kinds]

    def climate(self, location_id: str, climate_zone: str, month: int) -> ClimateMonth | None:
        months = self.get_location(location_id).climate.get(climate_zone) or []
        return next((m for m in months if m.month == month), None)

    def data_source(self, location_id: str) -> str:
        self._load(location_id)
        return self._meta[location_id].get("source", "unknown")
