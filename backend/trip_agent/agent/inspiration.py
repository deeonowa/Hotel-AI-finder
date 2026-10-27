"""«Не знаю куда» → карточки районов или городов: почему подходит, цена от, климат, дорога, документы. Владелец: ML1.

Важно: числа (цены, минуты, температура) берутся из данных и тех же провайдеров, что и поиск.
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import TYPE_CHECKING

from ..ports import CatalogProvider, PricingProvider, TransferProvider
from ..requirements_ops import PREF_LABELS
from ..schemas import (
    ApplyPatchAction,
    ClimateMonth,
    DestinationCard,
    DistrictKind,
    LocationPack,
    PatchOp,
    PrefKey,
    QuickReply,
    Requirements,
    VibeTag,
)
from ..transfer.schedule import from_city

if TYPE_CHECKING:
    from ..locations import LocationRegistry

PREF_TO_VIBE = {
    PrefKey.quiet: VibeTag.quiet,
    PrefKey.nightlife: VibeTag.party,
    PrefKey.family_friendly: VibeTag.family,
    PrefKey.romantic: VibeTag.romantic,
    PrefKey.budget_value: VibeTag.budget,
    PrefKey.luxury: VibeTag.luxury,
    PrefKey.nature: VibeTag.nature,
    PrefKey.wellness: VibeTag.wellness,
    PrefKey.ski_access: VibeTag.sport,
}


def suggest_districts(
    req: Requirements,
    location: LocationPack,
    catalog: CatalogProvider,
    pricing: PricingProvider,
    transfer: TransferProvider,
    check_in: date | None,
    check_out: date | None,
    limit: int = 4,
) -> list[DestinationCard]:
    month = check_in.month if check_in else req.dates.month
    hotels = catalog.list_hotels(location.id)
    wants = {p.key for p in req.preferences}
    cards: list[tuple[float, DestinationCard]] = []
    for d in location.districts:
        dh = [h for h in hotels if h.district_id == d.id]
        if not dh:
            continue
        score, why = 0.0, []
        for k in wants:
            if (v := PREF_TO_VIBE.get(k)) and v in d.vibe_tags:
                score += 1
                why.append(f"подходит под «{PREF_LABELS[k].lower()}»")
        if PrefKey.near_sea in wants or PrefKey.beach_first_line in wants:
            score += 1.5 if d.kind == DistrictKind.sea else -2
        if PrefKey.mountains in wants or PrefKey.ski_access in wants:
            score += 2 if d.kind == DistrictKind.mountain else -2
        if req.guests.children_ages and VibeTag.family in d.vibe_tags:
            score += 1
            why.append("удобно с детьми")
        climate = catalog.climate(location.id, d.climate_zone, month) if month else None
        if climate and month in (12, 1, 2, 3) and d.kind == DistrictKind.mountain and PrefKey.ski_access in wants:
            why.append("сезон катания")
        prices = []
        if check_in and check_out:
            for h in dh[:40]:
                offers = pricing.quote(h, req, check_in, check_out)
                if offers:
                    prices.append(min(o.price_per_night_avg for o in offers))
        airport = next((h for h in location.hubs if h.kind == "airport"), None)
        minutes = (
            int(statistics.median(transfer.estimate(location, airport.id, h, "to_hotel").minutes for h in dh[:15])) if airport else None
        )
        if minutes is not None and minutes <= 25:
            why.append("рядом с аэропортом")
        why += d.highlights[:2]
        cards.append(
            (
                score,
                DestinationCard(
                    district_id=d.id,
                    name=d.name,
                    kind=d.kind,
                    why=list(dict.fromkeys(why))[:4],
                    price_from_per_night=min(prices) if prices else None,
                    hotels_count=len(dh),
                    transfer_minutes_from_airport=minutes,
                    climate=climate,
                    choose=QuickReply(
                        label=f"Смотреть {d.name}",
                        action=ApplyPatchAction(ops=[PatchOp(op="set", path="district_ids", value=[d.id])], label=d.name),
                    ),
                ),
            )
        )
    cards.sort(key=lambda x: -x[0])
    return [c for _, c in cards[:limit]]


# ============================================================ города и страны
MONTHS_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]
WARM_SEA_C = 22.0  # с какой температуры воды купаться комфортно большинству


def wants_beach(req: Requirements) -> bool:
    return any(req.has_pref(k) for k in (PrefKey.near_sea, PrefKey.beach_first_line, PrefKey.sea_view))


def beach_climate(location: LocationPack, catalog: CatalogProvider, month: int) -> ClimateMonth | None:
    """Климат приморской зоны локации в месяц (или любой зоны, если моря нет)."""
    zone = "coast" if "coast" in location.climate else next(iter(location.climate), None)
    return catalog.climate(location.id, zone, month) if zone else None


def rainy_month(location: LocationPack, catalog: CatalogProvider, c: ClimateMonth) -> bool:
    """Дождливый месяц ДЛЯ ЭТОГО места: заметно выше его средней и не меньше 14 дней с осадками.

    Сравниваем с годовым ходом самой локации, а не с абсолютным порогом: реанализ (Open-Meteo/ERA5)
    в тропиках завышает число дней с мелкими осадками, но сезонность (сухо зимой на Пхукете) передаёт верно.
    """
    if c.precip_days is None or c.precip_days < 14:
        return False
    zone = "coast" if "coast" in location.climate else next(iter(location.climate), None)
    months = [m.precip_days for m in location.climate.get(zone or "", []) if m.precip_days is not None]
    if len(months) < 12:
        return False
    mean = sum(months) / 12
    return c.precip_days >= mean + 0.25 * (max(months) - min(months))


def suggest_locations(
    req: Requirements,
    locations: LocationRegistry,
    catalog: CatalogProvider,
    check_in: date | None,
    check_out: date | None,
    *,
    exclude: set[str] | None = None,
    limit: int = 4,
) -> list[DestinationCard]:
    """Сравнить ГОРОДА под запрос: климат в месяц поездки, цены на те же даты, перелёт, документы.

    Все числа — из тех же данных и провайдеров, что и поиск (климат Open-Meteo, DEMO-цены, DEMO-расписание).
    """
    month = check_in.month if check_in else req.dates.month
    origin = req.transport.origin_city or "Москва"
    wants = {p.key for p in req.preferences}
    beach = wants_beach(req)
    ski = PrefKey.ski_access in wants
    nights = (check_out - check_in).days if check_in and check_out else None
    pax = (req.guests.adults or 2) + sum(0.75 if a >= 2 else 0.1 for a in req.guests.children_ages)
    cards: list[tuple[float, DestinationCard]] = []
    for lid in locations.ids:
        if exclude and lid in exclude:
            continue
        svc = locations.get(lid)
        loc = svc.pack
        hotels = list(catalog.list_hotels(lid))
        if not hotels:
            continue
        score, why, cons = 0.0, [], []
        has_sea = any(d.kind == DistrictKind.sea for d in loc.districts)
        c = beach_climate(loc, catalog, month) if month else None
        when = f"в {MONTHS_PREP[month - 1]}" if month else ""
        if beach:
            if not has_sea:
                score -= 3
                cons.append("моря нет")
            elif c and c.sea_temp_c is not None:
                t = c.sea_temp_c
                score += 3 if t >= 25 else 2 if t >= WARM_SEA_C else 0.5 if t >= 20 else -2
                (why if t >= WARM_SEA_C else cons).append(
                    f"море ~{t:.0f}° {when}".strip() + (" — купаться комфортно" if t >= WARM_SEA_C else " — прохладно для купания")
                )
        if c and c.air_temp_day_c is not None:
            if c.air_temp_day_c >= 37:
                score -= 1
                cons.append(f"жара: днём ~{c.air_temp_day_c:.0f}°")
            elif not beach:
                why.append(f"днём ~{c.air_temp_day_c:.0f}° {when}".strip())
        if c and rainy_month(loc, catalog, c):
            score -= 1
            cons.append("самый дождливый период года")
        if ski:
            season = month in (12, 1, 2, 3) if month else True
            mountain = any(d.kind == DistrictKind.mountain for d in loc.districts)
            score += 3 if mountain and season else -3
            if mountain and season:
                why.append("сезон катания")
        for k in wants:
            if (v := PREF_TO_VIBE.get(k)) and any(v in d.vibe_tags for d in loc.districts):
                score += 0.4
        if PrefKey.city_center in wants and any(d.kind == DistrictKind.city for d in loc.districts):
            score += 1
        if loc.is_abroad:
            score -= 0.3  # нужен загранпаспорт — небольшой минус при прочих равных
        flight = svc.schedule.direct_minutes(origin)
        if flight is None:
            score -= 2
            cons.append(f"нет рейсов {from_city(origin)} в нашем расписании")
        else:
            h, m = divmod(flight, 60)
            why.append(f"перелёт {from_city(origin)} ~{h} ч" + (f" {m:02d} мин" if m else ""))
            if flight > 480:
                score -= 1

        # цены на те же даты: выборка отелей, 10-й перцентиль «за ночь» и 25-й «за проживание»
        per_night: list[int] = []
        totals: list[int] = []
        if check_in and check_out:
            step = max(1, len(hotels) // 60)
            for h in sorted(hotels, key=lambda x: x.id)[::step]:
                offers = svc.pricing.quote(h, req, check_in, check_out)
                if offers:
                    best = min(offers, key=lambda o: o.price_total)
                    per_night.append(best.price_per_night_avg)
                    totals.append(best.price_total)
        per_night.sort()
        totals.sort()
        price_from = per_night[len(per_night) // 10] if per_night else None
        if req.budget.amount and totals and nights:
            typical = totals[len(totals) // 4]
            route = next((r for r in loc.routes.get(origin, []) if r.mode == "plane"), None)
            tickets = int(route.price * 2 * pax) if route and req.transport.origin_city else 0
            budget = req.budget.amount if req.budget.scope.value != "per_night" else req.budget.amount * nights
            if typical + tickets > budget * 1.1:
                score -= 2
                cons.append("дороже вашего бюджета")
            else:
                score += 0.5
        if loc.entry:
            note = loc.entry.visa.split(";")[0]
        else:
            note = "без загранпаспорта"
            why.append("без загранпаспорта")
        cards.append(
            (
                score,
                DestinationCard(
                    district_id=lid,
                    scope="location",
                    location_id=lid,
                    country=loc.country,
                    name=loc.name,
                    kind=DistrictKind.sea if has_sea else DistrictKind.city,
                    why=list(dict.fromkeys(why))[:4],
                    cons=cons[:3],
                    price_from_per_night=price_from,
                    hotels_count=len(hotels),
                    flight_minutes=flight,
                    entry_note=note,
                    climate=c,
                    choose=QuickReply(
                        label=f"Смотреть {loc.name}",
                        action=ApplyPatchAction(ops=[PatchOp(op="set", path="location_id", value=lid)], label=loc.name),
                    ),
                ),
            )
        )
    cards.sort(key=lambda x: (-x[0], x[1].name))
    return [c for _, c in cards[:limit]]
