"""Baseline-ранжирование: жёсткие фильтры → предложения → бюджет → скоринг → 3 варианта-компромисса.

Владелец: ML2. Принципы (не ломать при доработке):
  1. Нарушение жёсткого ограничения = вариант отсекается, а не «штрафуется».
  2. Неизвестное (None) не считается выполненным (pets.allowed=None ≠ «можно с собакой»).
  3. Веса критериев выводятся из требований ЭТОГО пользователя, а не захардкожены одним набором.
  4. Каждый вариант несёт разбивку скора по критериям → из неё строится объяснение.
  5. Пусто → считаем «ослабления» реальным перезапуском фильтров (а не выдумываем).
  6. Стабильность: при почти равном скоре оставляем отель из прошлой выдачи (пересборка не «скачет»).
  7. Выбранный пользователем отель (pinned) сохраняется, пока он проходит все жёсткие условия;
     иначе — заменяется с ЯВНОЙ причиной (PinnedOutcome.reason).
"""

from __future__ import annotations

import hashlib
import math
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from ..ports import PricingProvider, TransferProvider
from ..pricing.demo_pricing import room_split
from ..requirements_ops import PREF_LABELS, PatchError, apply_patch, fmt_rub
from ..schemas import (
    Archetype,
    BudgetScope,
    DistrictKind,
    Hotel,
    HotelCard,
    Issue,
    LocationPack,
    Offer,
    PatchOp,
    PinnedOutcome,
    PrefKey,
    Priority,
    QuickReply,
    RankingResult,
    Relaxation,
    ScoreComponent,
    SearchStats,
    Severity,
    Source,
    TransferQuote,
    Variant,
    VibeTag,
)
from ..schemas.actions import ApplyPatchAction
from ..transfer.estimator import default_hub
from ..transfer.schedule import DemoSchedule, TicketPair, TicketSearch, wants_tickets
from .prefs import HARD_AT_HOTEL_LEVEL, HARD_AT_OFFER_LEVEL, SCORERS, Ctx

PRIORITY_WEIGHT = {Priority.must: 3.0, Priority.important: 2.0, Priority.nice: 1.0}
ARCHETYPE_TITLES = {
    Archetype.balanced: "Лучший баланс",
    Archetype.best_value: "Выгоднее всего",
    Archetype.comfort: "Максимум комфорта",
    Archetype.alternative: "Ещё вариант",
}
REJECT_LABELS = {
    "district": "не в выбранном районе",
    "capacity": "нет номера под ваш состав",
    "pets": "не принимают животных",
    "pets_unknown": "правило для животных неизвестно",
    "adults_only": "только для взрослых",
    "no_availability": "нет мест на эти даты",
    "over_budget": "дороже бюджета",
    "transfer_too_long": "слишком долгая дорога",
    "custom": "не решает проблему",
    "no_tickets": "нет подходящих билетов",
}


def reject_label(code: str) -> str:
    if code.startswith("custom:"):
        return code.split(":", 1)[1]
    if code.startswith("must:") or code.startswith("offer_must:"):
        key = code.split(":", 1)[1]
        return f"нет «{PREF_LABELS.get(PrefKey(key), key)}»"
    return REJECT_LABELS.get(code, code)


@dataclass
class _Cand:
    hotel: Hotel
    offer: Offer
    t_in: TransferQuote | None
    t_out: TransferQuote | None
    total: int
    tickets: TicketPair | None = None
    components: list[ScoreComponent] = field(default_factory=list)
    score: float = 0.0
    comfort: float = 0.0


class BaselineRanker:
    def __init__(
        self,
        location: LocationPack,
        pricing: PricingProvider,
        transfer: TransferProvider,
        top_k: int = 3,
        schedule: DemoSchedule | None = None,
    ):
        self.location = location
        self.pricing = pricing
        self.transfer = transfer
        self.top_k = top_k
        self.schedule = schedule

    # ------------------------------------------------------------------ API --
    def rank(
        self,
        req,
        hotels: Sequence[Hotel],
        check_in: date,
        check_out: date,
        *,
        previous=(),
        pinned_hotel_id: str | None = None,
        hotel_filter: Callable[[Hotel], str | None] | None = None,
    ) -> RankingResult:
        """hotel_filter(h) → None (подходит) или текст причины отказа. Для внутренних задач агента
        (автоисправление: «только отели с ранним заездом»), в требования пользователя не попадает."""
        t0 = time.perf_counter()
        tickets = self._tickets(req, check_in, check_out)
        if tickets is not None and tickets.pair is None:
            # билеты нужны (известен город), но под условия их нет — честно говорим и считаем ослабления
            stats = SearchStats(hotels_total=len(hotels), rejected={"no_tickets": len(hotels)})
            relax = self._relaxations(req, hotels, check_in, check_out)
            return RankingResult(variants=[], stats=stats, relaxations=relax, ticket_issue=tickets.reason)
        cands, rejected, after_hard, reasons = self._candidates(req, hotels, check_in, check_out, hotel_filter)
        stats = SearchStats(
            hotels_total=len(hotels),
            after_hard_filters=after_hard,
            with_offers=len(cands),
            rejected=dict(rejected),
        )
        pinned_out: PinnedOutcome | None = None
        pinned_cand: _Cand | None = None
        if pinned_hotel_id:
            pinned_cand = next((c for c in cands if c.hotel.id == pinned_hotel_id), None)
            ph = next((h for h in hotels if h.id == pinned_hotel_id), None)
            if ph is not None:
                pinned_out = PinnedOutcome(
                    hotel_id=ph.id,
                    hotel_name=ph.name,
                    kept=pinned_cand is not None,
                    reason=None if pinned_cand else reject_label(reasons.get(ph.id, "no_availability")),
                )
        if not cands:
            relax = self._relaxations(req, hotels, check_in, check_out)
            stats.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return RankingResult(variants=[], stats=stats, relaxations=relax, pinned=pinned_out)

        self._score_all(req, cands)
        picked = self._pick(cands, previous, pinned_cand)
        variants = [self._to_variant(req, c, arch) for c, arch in picked]
        self._cross_explain(variants)
        stats.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return RankingResult(variants=variants, recommended_id=variants[0].id, stats=stats, pinned=pinned_out)

    # ------------------------------------------------------- кандидаты ------
    def _hard_reject(self, req, h: Hotel, hotel_filter) -> str | None:
        """Причина отказа по жёстким условиям уровня отеля или None."""
        d = self.location.district(h.district_id)
        kids = req.guests.children_ages
        a_room, k_room = room_split(req.guests.adults or 2, len(kids), max(1, req.guests.rooms or 1))
        if req.district_ids and h.district_id not in req.district_ids:
            return "district"
        if not any(a_room <= r.max_adults and k_room <= r.max_children and a_room + k_room <= r.max_occupancy for r in h.room_types):
            return "capacity"
        if req.guests.pets:
            if h.pets.allowed is None:
                return "pets_unknown"
            if not h.pets.allowed:
                return "pets"
        if kids and h.adults_only:
            return "adults_only"
        for p in req.preferences:
            if (
                p.priority == Priority.must
                and p.key in HARD_AT_HOTEL_LEVEL
                and SCORERS[p.key](Ctx(h, d, has_kids=bool(kids)), p.value) < HARD_AT_HOTEL_LEVEL[p.key]
            ):
                return f"must:{p.key.value}"
        if hotel_filter is not None and (why := hotel_filter(h)):
            return f"custom:{why}"
        return None

    def _tickets(self, req, check_in: date, check_out: date) -> TicketSearch | None:
        if self.schedule is None or not wants_tickets(req) or not self.schedule.known_city(req.transport.origin_city):
            return None
        return self.schedule.pick(req, check_in, check_out)

    def _candidates(self, req, hotels, check_in, check_out, hotel_filter=None):
        ts = self._tickets(req, check_in, check_out)
        pair = ts.pair if ts else None
        if ts is not None and pair is None:
            return [], Counter({"no_tickets": len(hotels)}), 0, {}
        rejected: Counter[str] = Counter()
        reasons: dict[str, str] = {}
        survivors: list[Hotel] = []
        for h in hotels:
            code = self._hard_reject(req, h, hotel_filter)
            if code:
                rejected[code] += 1
                reasons[h.id] = code
                continue
            survivors.append(h)

        cands: list[_Cand] = []
        offer_musts = [p for p in req.preferences if p.priority == Priority.must and p.key in HARD_AT_OFFER_LEVEL]
        transfer_must = next(
            (p for p in req.preferences if p.key == PrefKey.short_transfer and p.priority == Priority.must and p.value),
            None,
        )
        for h in survivors:
            d = self.location.district(h.district_id)
            offers = self.pricing.quote(h, req, check_in, check_out)
            code = None
            if not offers:
                code = "no_availability"
            ok = [o for o in offers if all(SCORERS[p.key](Ctx(h, d, o), p.value) >= 1.0 for p in offer_musts)]
            if offers and not ok:
                code = f"offer_must:{offer_musts[0].key.value}"
            if code:
                rejected[code] += 1
                reasons[h.id] = code
                continue
            hub = (
                pair.outbound.hub_id
                if pair
                else req.transport.arrival_hub_id or default_hub(self.location, h, req.transport.arrival_mode.value)
            )
            t_in = self.transfer.estimate(self.location, hub, h, "to_hotel")
            t_out = self.transfer.estimate(self.location, hub, h, "from_hotel")
            if transfer_must is not None and t_in.minutes > float(transfer_must.value):
                rejected["transfer_too_long"] += 1
                reasons[h.id] = "transfer_too_long"
                continue
            best = min(ok, key=lambda o: self._offer_penalty(req, o, h, d))
            total = self._trip_total(req, best, t_in, t_out) + (pair.total if pair and req.budget.scope == BudgetScope.total_trip else 0)
            if not self._within_budget(req, best, total):
                rejected["over_budget"] += 1
                reasons[h.id] = "over_budget"
                continue
            cands.append(_Cand(h, best, t_in, t_out, total, pair))
        return cands, rejected, len(survivors), reasons

    @staticmethod
    def _offer_penalty(req, o: Offer, h: Hotel, d) -> float:
        factor = 1.0
        for p in req.preferences:
            if p.key in HARD_AT_OFFER_LEVEL:
                bonus = {Priority.must: 0.0, Priority.important: 0.15, Priority.nice: 0.07}[p.priority]
                factor -= bonus * SCORERS[p.key](Ctx(h, d, o), p.value)
        return o.price_total * factor

    @staticmethod
    def _trip_total(req, o: Offer, t_in, t_out) -> int:
        if req.budget.scope == BudgetScope.total_trip:
            return o.price_total + (t_in.price if t_in else 0) + (t_out.price if t_out else 0)
        return o.price_total

    @staticmethod
    def _within_budget(req, o: Offer, total: int) -> bool:
        b = req.budget.amount
        if not b:
            return True
        cap = b * (1 + req.budget.flex_pct / 100)
        if req.budget.scope == BudgetScope.per_night:
            return o.price_per_night_avg <= cap
        return total <= cap

    # ----------------------------------------------------------- скоринг ----
    def _score_all(self, req, cands: list[_Cand]) -> None:
        b = req.budget.amount
        has_kids = bool(req.guests.children_ages)
        pref_keys = {p.key for p in req.preferences}

        w_price = 2.0
        if (p := req.pref(PrefKey.budget_value)) is not None:
            w_price += PRIORITY_WEIGHT[p.priority]
        if req.pref(PrefKey.luxury) is not None:
            w_price = max(0.5, w_price - 1.0)

        for c in cands:
            d = self.location.district(c.hotel.district_id)
            ctx = Ctx(c.hotel, d, c.offer, c.t_in.minutes if c.t_in else None, has_kids)
            comps: list[ScoreComponent] = []

            if PrefKey.min_stars not in pref_keys and PrefKey.luxury not in pref_keys:
                q = _quality(c.hotel)
                comps.append(
                    ScoreComponent(
                        key="quality",
                        label="Класс отеля",
                        score=_c(q),
                        weight=1.5 if pref_keys & {PrefKey.romantic, PrefKey.wellness} else 1.0,
                        contribution=0,
                        detail=_quality_label(c.hotel),
                    )
                )
            if PrefKey.high_rating not in pref_keys and c.hotel.rating is not None:
                comps.append(
                    ScoreComponent(
                        key="rating",
                        label="Рейтинг",
                        score=_c((c.hotel.rating - 6) / 4),
                        weight=1.0,
                        contribution=0,
                        detail=f"{c.hotel.rating} из 10, {c.hotel.review_count} отзывов (демо)",
                    )
                )
            if not pref_keys & {PrefKey.near_sea, PrefKey.beach_first_line, PrefKey.city_center, PrefKey.mountains}:
                s_loc = SCORERS[PrefKey.near_sea](ctx, None) if d and d.kind == DistrictKind.sea else 0.7
                comps.append(
                    ScoreComponent(
                        key="location",
                        label="Расположение",
                        score=_c(s_loc),
                        weight=0.6,
                        contribution=0,
                        detail=_loc_detail(c.hotel, d),
                    )
                )
            if c.t_in:
                comps.append(
                    ScoreComponent(
                        key="transfer",
                        label="Дорога",
                        score=_c(1 - c.t_in.minutes / 120),
                        weight=0.4 + (0.6 if has_kids else 0.0),
                        contribution=0,
                        detail=f"{c.t_in.minutes} мин от «{c.t_in.hub_name}» (по дорогам, с учётом пробок)",
                    )
                )
            if PrefKey.free_cancellation not in pref_keys:
                refundable = c.offer.cancellation.refundable
                comps.append(
                    ScoreComponent(
                        key="flexibility",
                        label="Условия отмены",
                        score=1.0 if refundable else 0.3,
                        weight=0.3,
                        contribution=0,
                        detail=f"бесплатная отмена до {c.offer.cancellation.free_until:%d.%m}" if refundable else "невозвратный тариф",
                    )
                )
            for p in req.preferences:
                if p.key == PrefKey.budget_value:
                    continue
                s = SCORERS[p.key](ctx, p.value)
                comps.append(
                    ScoreComponent(
                        key=f"pref:{p.key.value}",
                        label=PREF_LABELS.get(p.key, p.key.value),
                        score=_c(s),
                        weight=PRIORITY_WEIGHT[p.priority],
                        contribution=0,
                        detail=_pref_detail(p.key, s, c, d),
                    )
                )

            c.components = comps
            nw = sum(x.weight for x in comps) or 1.0
            c.comfort = sum(x.weight * x.score for x in comps) / nw

        # Типичная цена = медиана среди ~20% вариантов, лучше всего подходящих под пожелания.
        # Сравнение со всеми (в т.ч. хостелами) делало любой подходящий отель «в 5 раз дороже».
        by_fit = sorted(cands, key=lambda x: x.comfort, reverse=True)
        # «Подешевле» → сравниваем со всеми вариантами, а не с лучшими по пожеланиям.
        ref = cands if req.pref(PrefKey.budget_value) else by_fit[: max(10, len(by_fit) // 5)]
        median = statistics.median(x.total for x in ref) or 1

        for c in cands:
            ratio = c.total / median
            rel = 0.65 - 0.55 * math.log2(max(ratio, 1e-6))  # типичная → 0.65, вдвое дешевле → 1, вдвое дороже → 0.1
            if b:
                cap = b if req.budget.scope != BudgetScope.per_night else b * c.offer.nights
                spent = c.total / cap
                # Бюджет назван → тратить его можно: до 85% бюджета цена не штрафуется;
                # дешевизна даёт лишь небольшой бонус (иначе семье с 120к советуем 2★ за 29к).
                bud = 1.0 if spent <= 0.85 else max(0.0, 1.0 - (spent - 0.85) / 0.15 * 0.6)
                s_price = 0.25 * min(1.0, max(0.0, rel)) + 0.75 * bud
                detail = f"{fmt_rub(c.total)} за всё, " + (
                    f"запас {fmt_rub(int(cap - c.total))}" if c.total <= cap else f"выше бюджета на {fmt_rub(int(c.total - cap))}"
                )
                weight = w_price
            else:
                s_price = rel
                detail = f"{fmt_rub(c.total)} за всё" + (
                    f", дороже типичного для таких отелей в {ratio:.1f} раза"
                    if ratio >= 1.4
                    else ", дешевле типичного для таких отелей"
                    if ratio <= 0.8
                    else ""
                )
                # Без бюджета цена весит ~треть решения, сколько бы пожеланий ни было.
                others = sum(x.weight for x in c.components)
                weight = max(w_price, round(0.5 * others * (0.5 if req.pref(PrefKey.luxury) else 1.0), 2))
            comps = [
                ScoreComponent(key="price", label="Цена", score=_c(s_price), weight=weight, contribution=0, detail=detail),
                *c.components,
            ]
            wsum = sum(x.weight for x in comps) or 1.0
            for x in comps:
                x.contribution = round(x.weight * x.score / wsum, 4)
            c.components = comps
            c.score = round(sum(x.contribution for x in comps), 4)

    # ------------------------------------------------------ архетипы --------
    def _pick(self, cands: list[_Cand], previous, pinned: _Cand | None = None) -> list[tuple[_Cand, Archetype]]:
        prev_hotels = {v.hotel.id for v in previous}
        by_score = sorted(cands, key=lambda c: c.score, reverse=True)
        top = by_score[0]
        sticky = next((c for c in by_score if c.hotel.id in prev_hotels and c.score >= top.score - 0.03), None)
        balanced = pinned or sticky or top
        picked = [(balanced, Archetype.balanced)]
        same = lambda c: c.hotel.district_id == balanced.hotel.district_id  # noqa: E731
        rest = [c for c in by_score if c.hotel.id != balanced.hotel.id]
        if rest:
            good = (
                [c for c in rest if c.score >= 0.85 * balanced.score and c.comfort >= 0.8 * balanced.comfort]
                or [c for c in rest if c.score >= 0.85 * balanced.score]
                or rest[: max(3, len(rest) // 4)]
            )
            # разнообразие: при близкой цене предпочитаем другой район
            cheap = min(good, key=lambda c: c.total * (1.06 if same(c) else 1.0))
            arch = Archetype.best_value if cheap.total < balanced.total * 0.97 else Archetype.alternative
            picked.append((cheap, arch))
            rest = [c for c in rest if c.hotel.id != cheap.hotel.id]
        if rest and len(picked) < self.top_k:
            pool = [c for c in rest if c.score >= 0.75 * balanced.score] or rest
            comfy = max(pool, key=lambda c: c.comfort - (0.03 if same(c) else 0.0))
            picked.append((comfy, Archetype.comfort if comfy.comfort > balanced.comfort else Archetype.alternative))
        if pinned is not None and top.hotel.id != pinned.hotel.id and all(c.hotel.id != top.hotel.id for c, _ in picked):
            picked[-1] = (top, Archetype.alternative)  # выбор пользователя сохранили, но лучший по скору тоже покажем
        return picked

    # ------------------------------------------------------- вывод ----------
    def _to_variant(self, req, c: _Cand, arch: Archetype) -> Variant:
        d = self.location.district(c.hotel.district_id)
        comps_sorted = sorted(c.components, key=lambda x: x.contribution, reverse=True)
        why = [f"{x.label}: {x.detail}" for x in comps_sorted if x.score >= 0.65][:3]
        tradeoffs = [f"{x.label}: {x.detail}" for x in c.components if x.weight >= 1.0 and x.score < 0.5]
        why += [f for f in _nearby_facts(req, c.hotel, d) if f not in why][: max(0, 4 - len(why))]
        if c.tickets:
            o = c.tickets.outbound
            leg = f"{'Прямой рейс' if o.mode == 'plane' else 'Поезд'}" if o.stops == 0 else "Рейс с пересадкой"
            line = f"{leg} {o.from_name} → {o.to_name}: прибытие {o.arrive_at:%H:%M}, билеты {fmt_rub(c.tickets.total)} (демо)"
            (why if o.stops == 0 else tradeoffs).append(line)
        pref_scores = {x.key.removeprefix("pref:"): x.score for x in c.components if x.key.startswith("pref:")}
        h = c.hotel
        vid = "v-" + hashlib.sha1(f"{h.id}|{c.offer.offer_id}".encode()).hexdigest()[:10]
        b = req.budget.amount
        return Variant(
            id=vid,
            archetype=arch,
            title=ARCHETYPE_TITLES[arch],
            hotel=HotelCard(
                id=h.id,
                name=h.name,
                kind=h.kind,
                district_id=h.district_id,
                district_name=d.name if d else h.district_id,
                stars=h.stars,
                rating=h.rating,
                review_count=h.review_count,
                distance_to_sea_m=h.distance_to_sea_m,
                beach_walk_minutes=h.beach_walk_minutes,
                amenities=h.amenities,
                vibe_tags=h.vibe_tags,
                check_in_time=h.check_in_time.strftime("%H:%M"),
                check_out_time=h.check_out_time.strftime("%H:%M"),
                early_check_in_fee_pct=h.early_check_in_fee_pct,
                pets_allowed=h.pets.allowed,
                pets_max_weight_kg=h.pets.max_weight_kg,
                climb_to_hotel_m=h.climb_to_hotel_m,
                nearest_m=h.nearest_m,
                lat=h.point.lat,
                lon=h.point.lon,
                data_source=h.provenance.source,
            ),
            offer=c.offer,
            transfer_in=c.t_in,
            transfer_out=c.t_out,
            outbound=c.tickets.outbound if c.tickets else None,
            inbound=c.tickets.inbound if c.tickets else None,
            total_price=c.total,
            budget_delta=(c.total - b) if b and req.budget.scope != BudgetScope.per_night else None,
            score=c.score,
            breakdown=comps_sorted,
            why=why,
            tradeoffs=tradeoffs,
            fit_warnings=self._fit_warnings(req, c, d),
            matched_prefs=[PrefKey(k) for k, s in pref_scores.items() if s >= 0.65],
            missed_prefs=[PrefKey(k) for k, s in pref_scores.items() if s < 0.5],
        )

    def _fit_warnings(self, req, c: _Cand, d) -> list[Issue]:
        """«Кому этот отель НЕ подойдёт» — честно, даже если вариант рекомендован."""
        h, out = c.hotel, []
        kids = req.guests.children_ages
        if h.hilly_walk:
            sev = Severity.warning if (kids or req.pref(PrefKey.accessible)) else Severity.info
            climb = f": подъём ~{h.climb_to_hotel_m} м от пляжа" if h.climb_to_hotel_m else ""
            out.append(
                Issue(
                    code="hilly_walk",
                    severity=sev,
                    message=f"Обратно с моря — в гору{climb}. Тяжело с детьми, коляской и пожилыми.",
                )
            )
        if kids and VibeTag.party in h.vibe_tags:
            out.append(
                Issue(
                    code="party_with_kids",
                    severity=Severity.warning,
                    message="Шумное место с вечеринками — с детьми может быть некомфортно.",
                )
            )
        pg = h.nearest_m.get("playground")
        if kids and not ({"kids_club", "playground"} & {a.value for a in h.amenities}) and (pg is None or pg > 600):
            out.append(Issue(code="no_kids_infra", severity=Severity.info, message="Нет детского клуба и площадки."))
        if (
            d
            and d.kind == DistrictKind.mountain
            and req.dates.month in (5, 6, 7, 8, 9)
            and (req.pref(PrefKey.near_sea) or req.pref(PrefKey.beach_first_line))
        ):
            out.append(Issue(code="no_sea_in_mountains", severity=Severity.warning, message="Это горы: до моря около часа езды."))
        if h.distance_to_sea_m and h.distance_to_sea_m > 1500 and d and d.kind == DistrictKind.sea:
            out.append(
                Issue(
                    code="far_from_sea",
                    severity=Severity.info,
                    message=f"До моря {h.distance_to_sea_m / 1000:.1f} км — нужен транспорт.",
                )
            )
        if not c.offer.cancellation.refundable:
            out.append(
                Issue(
                    code="non_refundable",
                    severity=Severity.info,
                    message="Невозвратный тариф: при отмене деньги не вернут.",
                    fix=QuickReply(
                        label="Хочу с бесплатной отменой",
                        action=ApplyPatchAction(
                            ops=[PatchOp(op="add", path="preferences", value={"key": "free_cancellation", "priority": "important"})],
                            label="С бесплатной отменой",
                        ),
                    ),
                )
            )
        if c.offer.pet_fee_total:
            out.append(
                Issue(
                    code="pet_fee",
                    severity=Severity.info,
                    message=f"Доплата за животное {fmt_rub(c.offer.pet_fee_total)} уже в цене.",
                )
            )
        if h.adults_only:
            out.append(Issue(code="adults_only", severity=Severity.info, message="Отель только для взрослых."))
        return out

    @staticmethod
    def _cross_explain(variants: list[Variant]) -> None:
        if not variants:
            return
        base = variants[0]
        for v in variants[1:]:
            delta = v.total_price - base.total_price
            if delta < 0:
                v.why.insert(0, f"Дешевле рекомендованного на {fmt_rub(-delta)}")
            elif delta > 0:
                v.tradeoffs.insert(0, f"Дороже рекомендованного на {fmt_rub(delta)}")

    # --------------------------------------------------- ослабления ---------
    def _relaxations(self, req, hotels, check_in, check_out) -> list[Relaxation]:
        tries: list[tuple[str, list[PatchOp]]] = []
        for p in req.preferences:
            if p.priority == Priority.must:
                tries.append(
                    (
                        f"Сделать «{PREF_LABELS.get(p.key, p.key.value)}» желательным",
                        [PatchOp(op="add", path="preferences", value={"key": p.key.value, "priority": "important"})],
                    )
                )
        if req.budget.amount:
            tries.append(("Поднять бюджет на 20%", [PatchOp(op="scale", path="budget.amount", value=1.2)]))
            tries.append(("Без ограничения бюджета", [PatchOp(op="unset", path="budget.amount")]))
        if req.district_ids:
            tries.append(("Искать во всех районах", [PatchOp(op="unset", path="district_ids")]))
        if req.transport.max_stops is not None:
            # сначала ищем ближайшие даты, где есть прямые рейсы туда и обратно (условие пользователя сохраняется)
            if self.schedule is not None and req.dates.is_exact:
                for delta in (1, -1, 2, -2, 3, -3):
                    r2, _ = apply_patch(req, [PatchOp(op="shift_days", path="dates", value=delta)], source=Source.user)
                    if self.schedule.pick(r2, r2.dates.check_in, r2.dates.check_out).pair is not None:
                        label = f"Сдвинуть на {abs(delta)} дн. {'позже' if delta > 0 else 'раньше'} — есть прямые рейсы"
                        tries.insert(0, (label, [PatchOp(op="shift_days", path="dates", value=delta)]))
                        break
            tries.append(("Рейсы с пересадкой", [PatchOp(op="unset", path="transport.max_stops")]))
        if wants_tickets(req):
            tries.append(("Без билетов — только отель", [PatchOp(op="unset", path="transport.origin_city")]))
        adults = req.guests.adults or 2
        if adults + len(req.guests.children_ages) >= 3 and not req.guests.rooms:
            rooms = 2 if adults + len(req.guests.children_ages) <= 6 else 3
            tries.append((f"Разместиться в {rooms} номерах", [PatchOp(op="set", path="guests.rooms", value=rooms)]))
        if req.dates.is_exact:
            tries.append(("Сдвинуть на неделю позже", [PatchOp(op="shift_days", path="dates", value=7)]))

        def count(ops: list[PatchOp]) -> int:
            try:
                r2, _ = apply_patch(req, ops, source=Source.user)
            except PatchError:
                return 0
            ci, co = (r2.dates.check_in, r2.dates.check_out) if r2.dates.is_exact else (check_in, check_out)
            return len(self._candidates(r2, hotels, ci, co)[0])

        out: list[Relaxation] = []
        for label, ops in tries:
            if n := count(ops):
                out.append(Relaxation(label=label, ops=ops, results_count=n))
        if not out:
            # Ни одно ослабление по отдельности не помогает → пробуем пары (проверено реальным поиском).
            for i, (l1, o1) in enumerate(tries):
                for l2, o2 in tries[i + 1 :]:
                    if n := count([*o1, *o2]):
                        out.append(Relaxation(label=f"{l1} и {l2[0].lower()}{l2[1:]}", ops=[*o1, *o2], results_count=n))
        return sorted(out, key=lambda r: ("прямые" not in r.label, -r.results_count))[:4]


KIND_QUALITY = {
    "hostel": 0.15,
    "motel": 0.3,
    "guest_house": 0.35,
    "apartment": 0.45,
    "hotel": 0.5,
    "sanatorium": 0.55,
    "chalet": 0.6,
    "resort": 0.8,
}
STARS_QUALITY = {1: 0.3, 2: 0.4, 3: 0.6, 4: 0.8, 5: 1.0}
KIND_RU = {
    "hostel": "хостел",
    "motel": "мотель",
    "guest_house": "гостевой дом",
    "apartment": "апартаменты",
    "hotel": "отель",
    "sanatorium": "санаторий",
    "chalet": "шале",
    "resort": "курорт",
}


def _quality(h: Hotel) -> float:
    return STARS_QUALITY.get(h.stars or 0) or KIND_QUALITY.get(h.kind.value, 0.5)


def _quality_label(h: Hotel) -> str:
    return KIND_RU.get(h.kind.value, h.kind.value) + (f", {h.stars}★" if h.stars else "")


def _nearby_facts(req, h: Hotel, d) -> list[str]:
    """Реальные факты о соседстве (OSM): что рядом — только то, что важно этому путешественнику."""
    out: list[str] = []
    near = h.nearest_m
    kids = bool(req.guests.children_ages)
    if d and d.kind == DistrictKind.sea and h.beach_walk_minutes is not None and h.beach_walk_minutes <= 10:
        out.append(f"Пляж в ~{h.beach_walk_minutes} мин пешком")
    if (req.pref(PrefKey.ski_access) or (d and d.kind == DistrictKind.mountain)) and near.get("ski_lift", 99999) <= 1200:
        out.append(f"Канатная дорога в {near['ski_lift']} м")
    if kids and near.get("playground", 99999) <= 400:
        out.append(f"Детская площадка в {near['playground']} м")
    if kids and near.get("water_park", 99999) <= 3000:
        out.append(f"Аквапарк в {near['water_park'] / 1000:.1f} км")
    if (req.pref(PrefKey.nature) or req.pref(PrefKey.quiet)) and near.get("park", 99999) <= 600:
        out.append(f"Парк в {near['park']} м")
    return out


def _c(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 3)


def _loc_detail(h: Hotel, d) -> str:
    if d and d.kind == DistrictKind.mountain:
        return f"горы, {d.name}"
    if h.distance_to_sea_m is not None:
        hill = ", в гору" if h.hilly_walk else ""
        return f"до моря {h.distance_to_sea_m} м (~{h.beach_walk_minutes} мин пешком{hill})"
    return d.name if d else ""


def _pref_detail(key: PrefKey, s: float, c: _Cand, d) -> str:
    h = c.hotel
    if key in (PrefKey.near_sea, PrefKey.beach_first_line):
        return _loc_detail(h, d)
    if key == PrefKey.short_transfer and c.t_in:
        return f"{c.t_in.minutes} мин"
    if key in (PrefKey.breakfast_included, PrefKey.all_inclusive):
        return {"RO": "без питания", "BB": "завтраки", "HB": "полупансион", "FB": "полный пансион", "AI": "всё включено"}[
            c.offer.meal_plan.value
        ]
    if key == PrefKey.free_cancellation:
        return "есть" if c.offer.cancellation.refundable else "нет"
    if key == PrefKey.min_stars:
        return f"{h.stars or 0}★"
    if key == PrefKey.high_rating:
        return f"рейтинг {h.rating}"
    return "да" if s >= 0.8 else "частично" if s >= 0.4 else "нет"
