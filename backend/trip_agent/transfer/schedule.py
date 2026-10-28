"""DEMO-расписание рейсов и поездов до локации и выбор пары билетов. Владелец: ML2.

Островок продаёт поездку целиком, поэтому агент умеет добавить к отелю билеты, если известен город
отправления. Реального API билетов у нас нет (на Q&A перелёты — «неплохое дополнение»), поэтому:
  * расписание и цены генерируются ДЕТЕРМИНИРОВАННО (хэш от маршрута и даты), помечены is_demo;
  * маршруты, длительности и доля прямых рейсов — в location.json каждого города (правдоподобные, DEMO);
  * время местное: вылет — по часам города отправления, прилёт — по часам локации (разница поясов учтена);
  * замена на реальный API = новая реализация с тем же методом pick().

Выбор пары — не «самый дешёвый», а с учётом удобства: время в пути, пересадки, ночной прилёт
(с детьми штраф больше), ранний вылет обратно (теряется последний день).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..schemas import LocationPack, Requirements, TransportOffer

# Маршруты «город отправления → хаб» лежат в location.json (routes, stop_cities) — у каждого города свои.
CARRIERS = {"plane": ["Демо-Авиа", "ДемоЛёт", "Южный Демо"], "train": ["Демо-РЖД"]}
# Смещение от UTC. Во всех этих поясах нет перехода на летнее время, поэтому хватает константы
# (и не нужна база tzdata в Docker-образе). Время в билетах — местное в точке вылета и прилёта.
UTC_OFFSET_H = {
    "Europe/Moscow": 3, "Europe/Minsk": 3, "Europe/Istanbul": 3, "Europe/Samara": 4, "Asia/Dubai": 4,
    "Asia/Tbilisi": 4, "Asia/Yekaterinburg": 5, "Asia/Bangkok": 7, "Asia/Novosibirsk": 7, "Asia/Krasnoyarsk": 7,
    "Asia/Vladivostok": 10,
}  # fmt: skip
ORIGIN_TZ = {
    "Москва": "Europe/Moscow", "Санкт-Петербург": "Europe/Moscow", "Казань": "Europe/Moscow",
    "Нижний Новгород": "Europe/Moscow", "Ростов-на-Дону": "Europe/Moscow", "Краснодар": "Europe/Moscow",
    "Минск": "Europe/Minsk", "Самара": "Europe/Samara", "Екатеринбург": "Asia/Yekaterinburg",
    "Новосибирск": "Asia/Novosibirsk", "Уфа": "Asia/Yekaterinburg", "Пермь": "Asia/Yekaterinburg",
    "Красноярск": "Asia/Krasnoyarsk", "Владивосток": "Asia/Vladivostok",
}  # fmt: skip

FROM_CITY = {
    "Москва": "Москвы", "Санкт-Петербург": "Петербурга", "Казань": "Казани", "Нижний Новгород": "Нижнего Новгорода",
    "Ростов-на-Дону": "Ростова-на-Дону", "Краснодар": "Краснодара", "Минск": "Минска", "Самара": "Самары",
    "Екатеринбург": "Екатеринбурга", "Новосибирск": "Новосибирска", "Уфа": "Уфы", "Пермь": "Перми",
    "Красноярск": "Красноярска", "Владивосток": "Владивостока",
}  # fmt: skip


def from_city(city: str) -> str:
    """«из Москвы»: родительный падеж для известных городов, иначе «из г. X»."""
    return f"из {FROM_CITY[city]}" if city in FROM_CITY else f"из г. {city}"


def _u(*parts: object) -> float:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


@dataclass
class TicketPair:
    outbound: TransportOffer
    inbound: TransportOffer
    total: int  # за всех пассажиров, туда и обратно


@dataclass
class TicketSearch:
    pair: TicketPair | None
    reason: str | None = None  # почему не нашлось
    options_out: int = 0


class DemoSchedule:
    def __init__(self, location: LocationPack, seed: str = "sched-v1"):
        self.location = location
        self.seed = seed

    def known_city(self, city: str | None) -> bool:
        return bool(city) and city in self.location.routes

    def direct_minutes(self, city: str | None) -> int | None:
        """Длительность прямого рейса из города (для карточек направлений)."""
        routes = self.location.routes.get(city or "", [])
        planes = [r.minutes for r in routes if r.mode == "plane"] or [r.minutes for r in routes]
        return min(planes) if planes else None

    def _tz_shift(self, city: str) -> int:
        """На сколько часов местное время локации впереди времени города отправления."""
        dest = UTC_OFFSET_H.get(self.location.timezone, 3)
        return dest - UTC_OFFSET_H.get(ORIGIN_TZ.get(city, "Europe/Moscow"), 3)

    def _hub_name(self, hub_id: str) -> str:
        h = self.location.hub(hub_id)
        return h.name if h else hub_id

    def offers(self, city: str, day: date, direction: str, mode: str | None, req: Requirements) -> list[TransportOffer]:
        out: list[TransportOffer] = []
        zones = self.location.seasonality
        season = (zones.get("coast") or next(iter(zones.values()), None) or [1.0] * 12)[day.month - 1] ** 0.7
        pax_factor = (req.guests.adults or 2) + sum(0.75 if a >= 2 else 0.1 for a in req.guests.children_ages)
        seats = (req.guests.adults or 2) + sum(1 for a in req.guests.children_ages if a >= 2)
        bags = bool(req.transport.baggage)
        # туда: вылет по времени города, прилёт по местному времени локации; обратно — наоборот
        shift = timedelta(hours=self._tz_shift(city) * (1 if direction == "outbound" else -1))
        stops_pool = self.location.stop_cities or ["Москва"]
        for r in self.location.routes.get(city, []):
            kind, hub, base_min, base_price, times, p_direct = r.mode, r.hub, r.minutes, r.price, r.times, r.p_direct
            if mode and mode != kind:
                continue
            for i, hhmm in enumerate(times):
                key = (self.seed, city, kind, direction, day, i)
                direct = _u(*key, "direct") < p_direct
                stops = 0 if direct else 1
                dur = base_min if direct else base_min + 90 + int(150 * _u(*key, "layover"))
                h, m = map(int, hhmm.split(":"))
                dep = datetime.combine(day, time(h, m)) + timedelta(minutes=int(20 * _u(*key, "shift")))
                arr = dep + timedelta(minutes=dur) + shift
                tod = 1.1 if 7 <= h <= 11 else 0.85 if h >= 21 or h < 6 else 1.0
                # на коротких рейсах за «прямой» доплачивают; на дальних прямой у нас не дороже стыковки
                premium = 1.25 if direct and kind == "plane" and base_min < 300 else 1.0
                price = base_price * season * tod * premium * (0.85 + 0.4 * _u(*key, "p"))
                price = int(round(price, -1))
                carrier = CARRIERS[kind][int(_u(*key, "c") * len(CARRIERS[kind]))]
                num = f"{'DA' if kind == 'plane' else '№'}{100 + int(_u(*key, 'n') * 899)}"
                stop_city = None if direct or kind != "plane" else stops_pool[int(_u(*key, "sc") * len(stops_pool))]
                if stop_city == city:  # пересадка не в городе вылета
                    stop_city = stops_pool[-1] if stops_pool[-1] != city else stops_pool[0]
                hub_name = self._hub_name(hub)
                # тариф «Лайт» — только ручная кладь; багаж 23 кг — доплата за человека (у поезда багаж включён)
                bag_fee = 0 if kind == "train" else int(round(2000 + base_min * 8, -2))
                total = price * pax_factor + (bag_fee * seats if bags else 0)
                out.append(
                    TransportOffer(
                        id=f"tk-{direction[0]}-{kind}-" + hashlib.sha1("|".join(map(str, key)).encode()).hexdigest()[:8],
                        direction=direction,
                        mode=kind,
                        carrier=carrier,
                        number=num,
                        origin_city=city,
                        from_name=city if direction == "outbound" else hub_name,
                        to_name=hub_name if direction == "outbound" else city,
                        hub_id=hub,
                        depart_at=dep,
                        arrive_at=arr,
                        duration_min=dur,
                        stops=stops,
                        stop_city=stop_city,
                        price_per_person=price,
                        passengers_price=int(round(total, -1)),
                        baggage_included=kind == "train" or bags,
                        baggage_fee_per_person=bag_fee,
                    )
                )
        return out

    def options(self, req: Requirements, check_in: date, check_out: date) -> tuple[list[TransportOffer], list[TransportOffer], str | None]:
        """Все рейсы туда и обратно под требования, от удобного к неудобному; третий элемент — почему пусто."""
        city = req.transport.origin_city
        if not self.known_city(city):
            return [], [], (f"нет расписания {from_city(city)}" if city else None)
        mode = {"plane": "plane", "train": "train"}.get(req.transport.arrival_mode.value)
        outs = self.offers(city, check_in, "outbound", mode, req)  # type: ignore[arg-type]
        ins = self.offers(city, check_out, "inbound", mode, req)  # type: ignore[arg-type]
        ms = req.transport.max_stops
        if ms is not None:
            outs = [o for o in outs if o.stops <= ms]
            ins = [o for o in ins if o.stops <= ms]
        if not outs or not ins:
            what = "прямых рейсов" if ms == 0 else "билетов"
            day = check_in if not outs else check_out
            return outs, ins, f"нет {what} {'туда' if not outs else 'обратно'} на {day:%d.%m}"
        kids = bool(req.guests.children_ages)
        dw, rw = req.transport.depart_window, req.transport.return_window

        def pen_out(o: TransportOffer) -> float:
            p = o.passengers_price + o.duration_min * 15 + o.stops * 2500
            if o.arrive_at.hour < 6 or o.arrive_at.hour >= 23:
                p += 8000 if kids else 4000
            if o.depart_at.hour < 6:
                p += 2000
            if o.arrive_at.date() > check_in and o.mode == "plane":
                p += 20_000  # прилёт на следующий день: первая оплаченная ночь пропадает
            if dw and window(o.depart_at.hour) != dw:
                p += 30_000  # пользователь просил вылет утром/вечером — это почти жёсткое условие
            return p

        def pen_in(o: TransportOffer) -> float:
            p = o.passengers_price + o.duration_min * 15 + o.stops * 2500
            if o.depart_at.hour < 11:
                p += 1500  # теряется последний день
            if o.depart_at.hour >= 22:
                p += 1000
            if rw and window(o.depart_at.hour) != rw:
                p += 30_000
            return p

        return sorted(outs, key=pen_out), sorted(ins, key=pen_in), None

    def pick(self, req: Requirements, check_in: date, check_out: date) -> TicketSearch:
        """Лучшая пара «туда-обратно» под требования. Ограничение max_stops — жёсткое.
        Рейс, выбранный пользователем кнопкой, берём как есть (если он есть на эти даты)."""
        outs, ins, reason = self.options(req, check_in, check_out)
        if reason:
            return TicketSearch(None, reason=reason)
        t = req.transport
        out = next((o for o in outs if o.id == t.outbound_offer_id), outs[0])
        inb = next((o for o in ins if o.id == t.inbound_offer_id), ins[0])
        return TicketSearch(TicketPair(out, inb, out.passengers_price + inb.passengers_price), options_out=len(outs))


def window(hour: int) -> str:
    """Часть суток по часу вылета: утро 5–12, день 12–17, вечер 17–23, ночь 23–5."""
    return "morning" if 5 <= hour < 12 else "day" if 12 <= hour < 17 else "evening" if 17 <= hour < 23 else "night"


def wants_tickets(req: Requirements) -> bool:
    """Билеты подбираем, если известен город отправления, едут не на машине и свой рейс не назван."""
    t = req.transport
    if not t.origin_city or t.arrival_mode.value == "car":
        return False
    return not (t.arrival_time is not None and req.provenance.get("transport.arrival_time") == "user")


def fmt_leg(o: TransportOffer) -> str:
    h, m = divmod(o.duration_min, 60)
    dur = f"{h} ч {m:02d} мин" if h else f"{m} мин"
    stops = "прямой" if o.stops == 0 else f"с пересадкой{(' (' + o.stop_city + ')') if o.stop_city else ''}"
    kind = "Рейс" if o.mode == "plane" else "Поезд"
    bag = "" if o.mode == "train" else (", с багажом" if o.baggage_included else ", ручная кладь")
    return (
        f"{kind} {o.from_name} → {o.to_name}, {o.depart_at:%d.%m %H:%M}–{o.arrive_at:%H:%M}, {dur}, {stops}{bag} "
        f"({o.carrier} {o.number}, демо)"
    )
