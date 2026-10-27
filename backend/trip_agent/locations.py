"""Реестр локаций: для каждого города — свои цены, расписание и ранжирование. Владелец: ML2.

Город — это данные (data/locations/<id>/), а не код: сезонность, районы, хабы, маршруты и правила
въезда лежат в location.json. Реестр лениво поднимает сервисы под локацию из требований пользователя,
поэтому агент один, а направлений сколько угодно («Сочи», «Анталья», «Дубай», …).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .ports import CatalogProvider, PricingProvider, Ranker, TransferProvider
from .requirements_ops import DISTRICT_LABELS, LOCATION_LABELS
from .schemas import Hotel, LocationPack
from .transfer.schedule import DemoSchedule


@dataclass
class LocationServices:
    pack: LocationPack
    pricing: PricingProvider
    ranker: Ranker
    schedule: DemoSchedule

    @property
    def id(self) -> str:
        return self.pack.id


RankerFactory = Callable[[LocationPack, PricingProvider, TransferProvider, DemoSchedule], Ranker]
PricingFactory = Callable[[LocationPack], PricingProvider]


class LocationRegistry:
    def __init__(
        self,
        catalog: CatalogProvider,
        transfer: TransferProvider,
        *,
        default_id: str,
        pricing_factory: PricingFactory,
        ranker_factory: RankerFactory,
        ids: list[str] | None = None,
    ):
        self.catalog = catalog
        self.transfer = transfer
        self.ids = ids or catalog.location_ids()
        if default_id not in self.ids:
            raise KeyError(f"локация по умолчанию {default_id!r} не найдена среди {self.ids}")
        self.default_id = default_id
        self._pricing_factory = pricing_factory
        self._ranker_factory = ranker_factory
        self._svc: dict[str, LocationServices] = {}
        for lid in self.ids:
            pack = self.catalog.get_location(lid)
            LOCATION_LABELS[lid] = pack.name
            DISTRICT_LABELS.update({d.id: d.name for d in pack.districts})

    def __contains__(self, location_id: object) -> bool:
        return location_id in self.ids

    def get(self, location_id: str | None) -> LocationServices:
        lid = location_id if location_id in self.ids else self.default_id
        if lid not in self._svc:
            pack = self.catalog.get_location(lid)
            pricing = self._pricing_factory(pack)
            schedule = DemoSchedule(pack)
            self._svc[lid] = LocationServices(pack, pricing, self._ranker_factory(pack, pricing, self.transfer, schedule), schedule)
        return self._svc[lid]

    def packs(self) -> list[LocationPack]:
        return [self.catalog.get_location(lid) for lid in self.ids]

    def hotels(self, location_id: str) -> list[Hotel]:
        return list(self.catalog.list_hotels(self.get(location_id).id))
