"""Baseline-ранжирование: жёсткие фильтры → предложения → бюджет → скоринг → 3 варианта-компромисса.

Владелец: ML2. Принципы (не ломать при доработке):
  1. Нарушение жёсткого ограничения = вариант отсекается, а не «штрафуется».
  2. Неизвестное (None) не считается выполненным (pets.allowed=None ≠ «можно с собакой»).
  3. Веса критериев выводятся из требований ЭТОГО пользователя, а не захардкожены одним набором.
  4. Каждый вариант несёт разбивку скора по критериям → из неё строится объяснение.
  5. Пусто → считаем «ослабления» реальным перезапуском фильтров (а не выдумываем).
  6. Стабильность: при почти равном скоре оставляем отель из прошлой выдачи (пересборка не «скачет»).
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..ports import PricingProvider, TransferProvider
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
}


def reject_label(code: str) -> str:
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
    components: list[ScoreComponent] = field(default_factory=list)
    score: float = 0.0
    comfort: float = 0.0


class BaselineRanker:
    def __init__(self, location: LocationPack, pricing: PricingProvider, transfer: TransferProvider, top_k: int = 3):
        self.location = location
        self.pricing = pricing
        self.transfer = transfer
        self.top_k = top_k

    # ------------------------------------------------------------------ API --
    def rank(self, req, hotels: Sequence[Hotel], check_in: date, check_out: date, *, previous=()) -> RankingResult:
        t0 = time.perf_counter()
        cands, rejected, after_hard = self._candidates(req, hotels, check_in, check_out)
        stats = SearchStats(
            hotels_total=len(hotels),
            after_hard_filters=after_hard,
            with_offers=len(cands),
            rejected=dict(rejected),
        )
        if not cands:
            relax = self._relaxations(req, hotels, check_in, check_out)
            stats.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return RankingResult(variants=[], stats=stats, relaxations=relax)

        self._score_all(req, cands)
        picked = self._pick(cands, previous)
        variants = [self._to_variant(req, c, arch) for c, arch in picked]
        self._cross_explain(variants)
        stats.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return RankingResult(variants=variants, recommended_id=variants[0].id, stats=stats)

    # ------------------------------------------------------- кандидаты ------
    def _candidates(self, req, hotels, check_in, check_out):
        rejected: Counter[str] = Counter()
        survivors: list[Hotel] = []
        adults = req.guests.adults or 2
        kids = req.guests.children_ages
        for h in hotels:
            d = self.location.district(h.district_id)
            if req.district_ids and h.district_id not in req.district_ids:
                rejected["district"] += 1
                continue
            if not any(
                adults <= r.max_adults and len(kids) <= r.max_children and adults + len(kids) <= r.max_occupancy
                for r in h.room_types
            ):
                rejected["capacity"] += 1
                continue
            if req.guests.pets:
                if h.pets.allowed is None:
                    rejected["pets_unknown"] += 1
                    continue
                if not h.pets.allowed:
                    rejected["pets"] += 1
                    continue
            if kids and h.adults_only:
                rejected["adults_only"] += 1
                continue
            failed = next(
                (
                    p.key
                    for p in req.preferences
                    if p.priority == Priority.must
                    and p.key in HARD_AT_HOTEL_LEVEL
                    and SCORERS[p.key](Ctx(h, d, has_kids=bool(kids)), p.value) < HARD_AT_HOTEL_LEVEL[p.key]
                ),
                None,
            )
            if failed:
                rejected[f"must:{failed.value}"] += 1
                continue
            survivors.append(h)

        cands: list[_Cand] = []
        offer_musts = [p for p in req.preferences if p.priority == Priority.must and p.key in HARD_AT_OFFER_LEVEL]
        for h in survivors:
            d = self.location.district(h.district_id)
            offers = self.pricing.quote(h, req, check_in, check_out)
            if not offers:
                rejected["no_availability"] += 1
                continue
            ok = [o for o in offers if all(SCORERS[p.key](Ctx(h, d, o), p.value) >= 1.0 for p in offer_musts)]
            if not ok:
                rejected[f"offer_must:{offer_musts[0].key.value}"] += 1
                continue
            hub = req.transport.arrival_hub_id or default_hub(self.location, h, req.transport.arrival_mode.value)
            t_in = self.transfer.estimate(self.location, hub, h, "to_hotel")
            t_out = self.transfer.estimate(self.location, hub, h, "from_hotel")
            best = min(ok, key=lambda o: self._offer_penalty(req, o, h, d))
            total = self._trip_total(req, best, t_in, t_out)
            if not self._within_budget(req, best, total):
                rejected["over_budget"] += 1
                continue
            cands.append(_Cand(h, best, t_in, t_out, total))
        return cands, rejected, len(survivors)

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
        totals = [c.total for c in cands]
        tmin, tmax = min(totals), max(totals)
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

            rel = 1.0 - (c.total - tmin) / (tmax - tmin + 1)
            if b:
                cap = b if req.budget.scope != BudgetScope.per_night else b * c.offer.nights
                spent = c.total / cap
                bud = 1.0 if spent <= 0.7 else max(0.0, 1.0 - (spent - 0.7) / 0.3 * 0.6)
                s_price = 0.5 * rel + 0.5 * bud
                detail = f"{fmt_rub(c.total)} за всё, " + (
                    f"запас {fmt_rub(int(cap - c.total))}" if c.total <= cap else f"выше бюджета на {fmt_rub(int(c.total - cap))}"
                )
            else:
                s_price = rel
                detail = f"{fmt_rub(c.total)} за всё"
            comps.append(
                ScoreComponent(key="price", label="Цена", score=_c(s_price), weight=w_price, contribution=0, detail=detail)
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
                        detail=f"{c.t_in.minutes} мин от «{c.t_in.hub_name}» (оценка)",
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
                        detail=f"бесплатная отмена до {c.offer.cancellation.free_until:%d.%m}"
                        if refundable
                        else "невозвратный тариф",
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

            wsum = sum(x.weight for x in comps) or 1.0
            for x in comps:
                x.contribution = round(x.weight * x.score / wsum, 4)
            c.components = comps
            c.score = round(sum(x.contribution for x in comps), 4)
            non_price = [x for x in comps if x.key != "price"]
            nw = sum(x.weight for x in non_price) or 1.0
            c.comfort = sum(x.weight * x.score for x in non_price) / nw

    # ------------------------------------------------------ архетипы --------
    def _pick(self, cands: list[_Cand], previous) -> list[tuple[_Cand, Archetype]]:
        prev_hotels = {v.hotel.id for v in previous}
        by_score = sorted(cands, key=lambda c: c.score, reverse=True)
        top = by_score[0]
        sticky = next((c for c in by_score if c.hotel.id in prev_hotels and c.score >= top.score - 0.03), None)
        balanced = sticky or top
        picked = [(balanced, Archetype.balanced)]
        rest = [c for c in by_score if c.hotel.id != balanced.hotel.id]
        if rest:
            good = [c for c in rest if c.score >= 0.8 * balanced.score] or rest
            cheap = min(good, key=lambda c: c.total)
            picked.append((cheap, Archetype.best_value if cheap.total < balanced.total else Archetype.alternative))
            rest = [c for c in rest if c.hotel.id != cheap.hotel.id]
        if rest and len(picked) < self.top_k:
            comfy = max(rest, key=lambda c: c.comfort)
            picked.append((comfy, Archetype.comfort if comfy.comfort > balanced.comfort else Archetype.alternative))
        return picked

    # ------------------------------------------------------- вывод ----------
    def _to_variant(self, req, c: _Cand, arch: Archetype) -> Variant:
        d = self.location.district(c.hotel.district_id)
        comps_sorted = sorted(c.components, key=lambda x: x.contribution, reverse=True)
        why = [f"{x.label}: {x.detail}" for x in comps_sorted if x.score >= 0.65][:3]
        tradeoffs = [f"{x.label}: {x.detail}" for x in c.components if x.weight >= 1.0 and x.score < 0.5]
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
                lat=h.point.lat,
                lon=h.point.lon,
                data_source=h.provenance.source,
            ),
            offer=c.offer,
            transfer_in=c.t_in,
            transfer_out=c.t_out,
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
            out.append(
                Issue(
                    code="hilly_walk", severity=sev, message="Дорога к морю идёт в гору — тяжело с детьми, коляской и пожилыми."
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
        if kids and not ({"kids_club", "playground"} & {a.value for a in h.amenities}):
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
                            ops=[
                                PatchOp(op="add", path="preferences", value={"key": "free_cancellation", "priority": "important"})
                            ],
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
        if req.dates.is_exact:
            tries.append(("Сдвинуть на неделю позже", [PatchOp(op="shift_days", path="dates", value=7)]))
        out: list[Relaxation] = []
        for label, ops in tries:
            try:
                r2, _ = apply_patch(req, ops, source=Source.user)
            except PatchError:
                continue
            ci, co = (r2.dates.check_in, r2.dates.check_out) if r2.dates.is_exact else (check_in, check_out)
            n = len(self._candidates(r2, hotels, ci, co)[0])
            if n:
                out.append(Relaxation(label=label, ops=ops, results_count=n))
        return sorted(out, key=lambda r: -r.results_count)[:4]


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
