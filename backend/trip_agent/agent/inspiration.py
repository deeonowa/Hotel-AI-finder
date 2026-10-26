"""«Не знаю куда» → карточки районов: почему подходит, цена от, климат в месяц поездки, дорога. Владелец: ML1.

Важно: числа (цены, минуты, температура) берутся из данных и тех же провайдеров, что и поиск.
"""

from __future__ import annotations

import statistics
from datetime import date

from ..ports import CatalogProvider, PricingProvider, TransferProvider
from ..requirements_ops import PREF_LABELS
from ..schemas import (
    ApplyPatchAction,
    DestinationCard,
    DistrictKind,
    LocationPack,
    PatchOp,
    PrefKey,
    QuickReply,
    Requirements,
    VibeTag,
)

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
            int(statistics.median(transfer.estimate(location, airport.id, h, "to_hotel").minutes for h in dh[:15]))
            if airport
            else None
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
