"""DEMO-расписание рейсов и поездов до локации и выбор пары билетов. Владелец: ML2.

Островок продаёт поездку целиком, поэтому агент умеет добавить к отелю билеты, если известен город
отправления. Реального API билетов у нас нет (на Q&A перелёты — «неплохое дополнение»), поэтому:
  * расписание и цены генерируются ДЕТЕРМИНИРОВАННО (хэш от маршрута и даты), помечены is_demo;
  * длительности и наличие прямых рейсов правдоподобны для направлений в Сочи;
  * замена на реальный API = новая реализация с тем же методом pick().

Выбор пары — не «самый дешёвый», а с учётом удобства: время в пути, пересадки, ночной прилёт
(с детьми штраф больше), ранний вылет обратно (теряется последний день).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..schemas import LocationPack, Requirements, TransportOffer

# город → маршруты: (вид, хаб, длительность прямого, базовая цена/чел, время отправлений, доля прямых)
ROUTES: dict[str, list[tuple[str, str, int, int, list[str], float]]] = {
    "Москва": [("plane", "AER", 145, 6500, ["06:10", "08:40", "11:20", "14:05", "17:30", "21:45"], 0.65),
               ("train", "rail_adler", 1440, 5200, ["12:30", "20:10"], 1.0)],
    "Санкт-Петербург": [("plane", "AER", 200, 8000, ["07:05", "13:40", "19:55"], 0.6)],
    "Екатеринбург": [("plane", "AER", 220, 9000, ["06:50", "15:20"], 0.5)],
    "Казань": [("plane", "AER", 165, 7500, ["09:15", "18:40"], 0.5)],
    "Новосибирск": [("plane", "AER", 290, 12500, ["05:40", "13:10"], 0.35)],
    "Ростов-на-Дону": [("plane", "AER", 80, 5000, ["10:30", "19:10"], 0.8),
                       ("train", "rail_sochi", 540, 2200, ["07:20", "22:50"], 1.0)],
    "Краснодар": [("train", "rail_sochi", 270, 1300, ["06:40", "10:15", "15:30", "18:45"], 1.0)],
    "Самара": [("plane", "AER", 175, 8000, ["08:00", "20:30"], 0.4)],
    "Нижний Новгород": [("plane", "AER", 165, 7800, ["07:30"], 0.4)],
    "Минск": [("plane", "AER", 190, 9500, ["11:45"], 0.5)],
}  # fmt: skip
STOP_CITY = {"plane": ["Москва", "Ростов-на-Дону", "Минеральные Воды", "Краснодар"]}
CARRIERS = {"plane": ["Демо-Авиа", "ДемоЛёт", "Южный Демо"], "train": ["Демо-РЖД"]}


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
        return bool(city) and city in ROUTES

    def _hub_name(self, hub_id: str) -> str:
        h = self.location.hub(hub_id)
        return h.name if h else hub_id

    def offers(self, city: str, day: date, direction: str, mode: str | None, req: Requirements) -> list[TransportOffer]:
        out: list[TransportOffer] = []
        season = self.location.seasonality.get("coast", [1.0] * 12)[day.month - 1] ** 0.7
        pax_factor = (req.guests.adults or 2) + sum(0.75 if a >= 2 else 0.1 for a in req.guests.children_ages)
        for kind, hub, base_min, base_price, times, p_direct in ROUTES.get(city, []):
            if mode and mode != kind:
                continue
            for i, hhmm in enumerate(times):
                key = (self.seed, city, kind, direction, day, i)
                direct = _u(*key, "direct") < p_direct
                stops = 0 if direct else 1
                dur = base_min if direct else base_min + 90 + int(150 * _u(*key, "layover"))
                h, m = map(int, hhmm.split(":"))
                dep = datetime.combine(day, time(h, m)) + timedelta(minutes=int(20 * _u(*key, "shift")))
                arr = dep + timedelta(minutes=dur)
                tod = 1.1 if 7 <= h <= 11 else 0.85 if h >= 21 or h < 6 else 1.0
                price = base_price * season * tod * (1.25 if direct and kind == "plane" else 1.0) * (0.85 + 0.4 * _u(*key, "p"))
                price = int(round(price, -1))
                carrier = CARRIERS[kind][int(_u(*key, "c") * len(CARRIERS[kind]))]
                num = f"{'DA' if kind == 'plane' else '№'}{100 + int(_u(*key, 'n') * 899)}"
                stop_city = None if direct else STOP_CITY["plane"][int(_u(*key, "sc") * 4)] if kind == "plane" else None
                if stop_city == city:
                    stop_city = "Краснодар"
                hub_name = self._hub_name(hub)
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
                        passengers_price=int(round(price * pax_factor, -1)),
                    )
                )
        return out

    def pick(self, req: Requirements, check_in: date, check_out: date) -> TicketSearch:
        """Лучшая пара «туда-обратно» под требования. Ограничение max_stops — жёсткое."""
        city = req.transport.origin_city
        if not self.known_city(city):
            return TicketSearch(None, reason=f"нет расписания из {city}" if city else None)
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
            return TicketSearch(None, reason=f"нет {what} {'туда' if not outs else 'обратно'} на {day:%d.%m}")
        kids = bool(req.guests.children_ages)

        def pen_out(o: TransportOffer) -> float:
            p = o.passengers_price + o.duration_min * 15 + o.stops * 2500
            if o.arrive_at.hour < 6 or o.arrive_at.hour >= 23:
                p += 8000 if kids else 4000
            if o.depart_at.hour < 6:
                p += 2000
            if o.arrive_at.date() > check_in and o.mode == "plane":
                p += 10_000
            return p

        def pen_in(o: TransportOffer) -> float:
            p = o.passengers_price + o.duration_min * 15 + o.stops * 2500
            if o.depart_at.hour < 11:
                p += 1500  # теряется последний день
            if o.depart_at.hour >= 22:
                p += 1000
            return p

        out = min(outs, key=pen_out)
        inb = min(ins, key=pen_in)
        return TicketSearch(TicketPair(out, inb, out.passengers_price + inb.passengers_price), options_out=len(outs))


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
    return (
        f"{kind} {o.from_name} → {o.to_name}, {o.depart_at:%d.%m %H:%M}–{o.arrive_at:%H:%M}, {dur}, {stops} ({o.carrier} {o.number}, демо)"
    )
