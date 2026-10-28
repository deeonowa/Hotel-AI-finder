"""Оркестратор агента: один ход = вход (текст или действие) → решения → UI-блоки. Владелец: ML1.

Архитектура «LLM понимает и формулирует, код решает и считает»:
  текст ──NLU──▶ (intent, patch) ─┐
  клик ─────────▶ Action ─────────┴─▶ обработчик ─▶ patch к Requirements ─▶ политика
      ─▶ [вопрос | районы | поиск→ранжирование→валидация] ─▶ diff ─▶ NLG ─▶ blocks + trace

Почему не свободный ReAct-цикл/LangGraph: предсказуемое число вопросов, никакой выдуманной цены,
воспроизводимость для тестов/eval, ход ≤ 1–2 вызова LLM (быстро и дёшево). Гибкость — в NLU.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from ..booking.service import BookingError, BookingService
from ..locations import LocationRegistry, LocationServices
from ..ports import CatalogProvider, Nlg, Nlu, SessionStore, TransferProvider, Validator
from ..ranking.baseline import reject_label
from ..requirements_ops import PREF_LABELS, PatchError, apply_patch, fmt_rub
from ..schemas import (
    Action,
    AddExtraAction,
    AddToCartAction,
    ApplyPatchAction,
    AssumptionChip,
    AssumptionsBlock,
    CartBlock,
    CartItemKind,
    CheckoutAction,
    CompareAction,
    ComparisonBlock,
    ComparisonRow,
    ConfirmAssumptionsAction,
    DestinationsBlock,
    DiffBlock,
    DiffItem,
    DismissFixAction,
    Intent,
    LocationPack,
    LocationSummary,
    NluResult,
    NoResultsBlock,
    OrderBlock,
    OrderStatus,
    PatchOp,
    PayAction,
    PostPurchaseAction,
    PostPurchaseKind,
    PrefKey,
    QuestionBlock,
    QuickReply,
    RemoveCartItemAction,
    Requirements,
    ResetAction,
    SelectVariantAction,
    Source,
    TextBlock,
    TraceStep,
    TripStage,
    TripState,
    TurnRequest,
    TurnResponse,
    UIBlock,
    Variant,
    VariantsBlock,
)
from ..transfer.schedule import wants_tickets
from ..validation.rules import arrival_quick_replies, pending_confirmations
from .autofix import Fix, find_fix
from .inspiration import MONTHS_PREP, WARM_SEA_C, beach_climate, suggest_districts, suggest_locations, wants_beach
from .policy import (
    MAX_QUESTIONS,
    assume_rooms,
    assumption_chips,
    critical_question,
    dates_label,
    guests_label,
    implicit_preferences,
    resolve_dates,
)


def q(name: str) -> str:
    """Название в кавычках без двойных «« »»."""
    return name if "«" in name else f"«{name}»"


MEALS_RU = {"RO": "без питания", "BB": "завтраки", "HB": "полупансион", "FB": "полный пансион", "AI": "всё включено"}


class SessionNotFound(KeyError):
    pass


class VersionConflict(RuntimeError):
    pass


class Tracer:
    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    @contextmanager
    def step(self, name: str) -> Iterator[dict]:
        t0 = time.perf_counter()
        info: dict = {"detail": ""}
        try:
            yield info
        finally:
            self.steps.append(TraceStep(name=name, detail=info["detail"], ms=int((time.perf_counter() - t0) * 1000)))

    def note(self, name: str, detail: str) -> None:
        self.steps.append(TraceStep(name=name, detail=detail))


Out = tuple[list[UIBlock], list[QuickReply]]


class Agent:
    def __init__(
        self,
        *,
        catalog: CatalogProvider,
        transfer: TransferProvider,
        locations: LocationRegistry,
        validator: Validator,
        nlu: Nlu,
        nlg: Nlg,
        store: SessionStore,
        booking: BookingService,
        mode: str = "rules",
        today: Callable[[], date] = date.today,
    ):
        self.catalog, self.transfer, self.locations = catalog, transfer, locations
        self.validator, self.nlu, self.nlg = validator, nlu, nlg
        self.store, self.booking = store, booking
        self.location_id = locations.default_id  # куда едем, если пользователь не назвал город
        self.mode = mode
        self.today = today

    # локация по умолчанию — для обратной совместимости (тесты, скрипты)
    @property
    def location(self) -> LocationPack:
        return self.locations.get(self.location_id).pack

    @property
    def pricing(self):  # noqa: ANN201
        return self.locations.get(self.location_id).pricing

    @property
    def ranker(self):  # noqa: ANN201
        return self.locations.get(self.location_id).ranker

    def _svc(self, st: TripState) -> LocationServices:
        return self.locations.get(st.requirements.location_id)

    def _loc(self, st: TripState) -> LocationPack:
        return self._svc(st).pack

    @property
    def multi_location(self) -> bool:
        return len(self.locations.ids) > 1

    def _pending(self, st: TripState) -> list[tuple[str, str]]:
        name = self._loc(st).name if self.multi_location else None
        return pending_confirmations(st.requirements, name)

    # ============================================================ публичное API
    def new_session(self, user_id: str | None = None) -> TurnResponse:
        now = datetime.now(UTC)
        state = TripState(
            session_id=uuid.uuid4().hex,
            user_id=user_id,
            created_at=now,
            updated_at=now,
            requirements=Requirements(location_id=self.location_id),
        )
        blocks, sugg = self._greeting()
        self.store.save(state)
        return self._response(state, blocks, sugg, [], self.mode)

    def get_state(self, session_id: str) -> TripState:
        st = self.store.load(session_id)
        if st is None:
            raise SessionNotFound(session_id)
        return st

    def turn(self, session_id: str, req: TurnRequest) -> TurnResponse:
        state = self.get_state(session_id)
        if req.expected_version is not None and req.expected_version != state.version:
            raise VersionConflict(f"state version is {state.version}, client saw {req.expected_version}")
        tr = Tracer()
        mode = self.mode
        if req.action is not None:
            tr.note("input", f"действие {req.action.type}")
            blocks, sugg = self._on_action(state, req.action, tr)
        else:
            with tr.step("nlu") as s:
                nlu = self.nlu.parse(req.text or "", state, self.today())
                s["detail"] = f"{nlu.intent.value} · {len(nlu.patch)} изм. · {nlu.parser}"
            mode = nlu.parser
            if getattr(self.nlu, "last_error", None) and nlu.parser == "rules" and self.mode == "llm":
                tr.note("nlu_fallback", "LLM недоступна → правила")
            if notes := getattr(self.nlu, "guard_notes", None):
                tr.note("nlu_guard", "; ".join(notes))
            blocks, sugg = self._on_intent(state, nlu, tr)
        state.version += 1
        state.turn_count += 1
        state.updated_at = datetime.now(UTC)
        self.store.save(state)
        return self._response(state, blocks, sugg, tr.steps, mode)

    # ============================================================ маршрутизация
    def _on_intent(self, st: TripState, nlu: NluResult, tr: Tracer) -> Out:
        i = nlu.intent
        if i == Intent.reset:
            return self._reset(st)
        if i in (Intent.plan_trip, Intent.modify, Intent.inspire):
            if st.order_ids and i == Intent.modify and nlu.patch:
                return self._post_purchase(st, PostPurchaseKind.change_dates, {"patch": [o.model_dump(mode="json") for o in nlu.patch]})
            if not nlu.patch and i != Intent.inspire and not nlu.unknown_destination:
                return self._ask_for_details()
            changes = self._apply(st, nlu.patch, nlu.assumed_paths, tr)
            if changes is None:
                return [TextBlock(markdown="Не смог применить изменение — переформулируйте, пожалуйста.")], []
            if nlu.unknown_destination:
                return self._unknown_destination(st, nlu.unknown_destination, tr)
            if self._logistics_only(st, nlu.patch):
                return self._recheck(st, tr)
            only = {x for x in nlu.candidate_location_ids if x in self.locations} or None
            return self._plan(st, tr, changes, inspire=(i == Intent.inspire), only=only)
        if i == Intent.select_variant:
            v = self._resolve_variant(st, nlu.target_variant_ids, nlu.variant_ordinal)
            return self._select(st, v, tr) if v else ([TextBlock(markdown="Какой вариант выбрать? Нажмите «Выбрать» на карточке.")], [])
        if i == Intent.compare:
            return self._compare(st, nlu.target_variant_ids)
        if i == Intent.book:
            if st.cart_id and st.stage == TripStage.in_cart:
                if nlu.parser == "llm":
                    return self._confirm_checkout(st)
                return self._checkout(st, None)
            v = self._resolve_variant(st, nlu.target_variant_ids, nlu.variant_ordinal) or self._current_variant(st)
            return self._add_to_cart(st, v, True, tr) if v else self._ask_for_details()
        if i == Intent.post_purchase:
            return self._post_purchase(st, nlu.post_purchase_kind or PostPurchaseKind.reminder, {})
        if i == Intent.ask_info:
            return self._answer(st, nlu.info_question or "")
        if i == Intent.smalltalk:
            return [TextBlock(markdown="Рад помочь! Расскажите, какую поездку планируете — подберу варианты.")], self._examples()
        if i == Intent.off_topic:
            return [
                TextBlock(markdown="Я помогаю спланировать поездку и подобрать отель. Расскажите, куда и когда хотите поехать.")
            ], self._examples()
        return self._ask_for_details()

    def _on_action(self, st: TripState, a: Action, tr: Tracer) -> Out:
        if isinstance(a, ApplyPatchAction):
            changes = self._apply(st, a.ops, [], tr)
            if changes is None:
                return [TextBlock(markdown="Это изменение не применить к текущему плану.")], []
            if self._logistics_only(st, a.ops):
                return self._recheck(st, tr)
            return self._plan(st, tr, changes, inspire=False)
        if isinstance(a, SelectVariantAction):
            v = st.variant(a.variant_id)
            return self._select(st, v, tr) if v else ([TextBlock(markdown="Вариант устарел — обновил выдачу.")], [])
        if isinstance(a, CompareAction):
            return self._compare(st, a.variant_ids)
        if isinstance(a, AddToCartAction):
            v = st.variant(a.variant_id)
            return self._add_to_cart(st, v, a.with_transfer, tr) if v else ([TextBlock(markdown="Вариант устарел.")], [])
        if isinstance(a, AddExtraAction | RemoveCartItemAction):
            return self._cart_edit(st, a)
        if isinstance(a, CheckoutAction):
            return self._checkout(st, a.guest_name)
        if isinstance(a, PayAction):
            return self._pay(st, a.order_id)
        if isinstance(a, PostPurchaseAction):
            return self._post_purchase(st, a.kind, a.details, order_id=a.order_id)
        if isinstance(a, ConfirmAssumptionsAction):
            return self._confirm(st, a, tr)
        if isinstance(a, DismissFixAction):
            st.dismissed_fixes.append(f"{a.hotel_id}:{a.code}")
            tr.note("autofix", "пользователь оставил свой вариант")
            if st.stage == TripStage.in_cart and (cart := self.booking.repo.get_cart(st.cart_id or "")):
                return [TextBlock(markdown="Хорошо, оставляю ваш выбор."), CartBlock(cart=cart)], [
                    QuickReply(label="Оформить", action=CheckoutAction())
                ]
            v = self._current_variant(st)
            return self._select(st, v, tr) if v else self._ask_for_details()
        if isinstance(a, ResetAction):
            return self._reset(st)
        return [TextBlock(markdown="Неизвестное действие.")], []

    # ============================================================ требования
    def _apply(self, st: TripState, ops: list[PatchOp], assumed: list[str], tr: Tracer) -> list[str] | None:
        with tr.step("apply_patch") as s:
            if any(o.path == "location_id" and o.value not in self.locations for o in ops):
                s["detail"] = "ошибка: неизвестное направление"
                return None
            before = st.requirements.location_id
            try:
                new_req, changes = apply_patch(st.requirements, ops, source=Source.user, today=self.today())
            except PatchError as e:
                s["detail"] = f"ошибка: {e}"
                return None
            for p in assumed:
                new_req.provenance[p] = Source.assumed
            st.requirements = new_req
            if new_req.location_id != before:
                self._on_location_change(st, changes)
            s["detail"] = "; ".join(changes) or "без изменений"
        return changes

    def _on_location_change(self, st: TripState, changes: list[str]) -> None:
        """Другой город: отели прежнего города не закрепляем, собранную под них корзину не оформляем."""
        if st.cart_id and st.stage == TripStage.in_cart:
            changes.append("корзину с отелем прежнего направления очистил")
        st.cart_id = None
        st.selected_variant_id = None
        st.dismissed_fixes = []
        if st.stage in (TripStage.selected, TripStage.in_cart):
            st.stage = TripStage.proposing

    def _unknown_destination(self, st: TripState, name: str, tr: Tracer) -> Out:
        """Город не из каталога: не подменяем молча — говорим честно и сравниваем то, что есть."""
        req = st.requirements
        ops, _ = resolve_dates(req, self.today()) if (req.dates.check_in or req.dates.month) else ([], [])
        if ops:
            st.requirements, _ = apply_patch(req, ops, source=Source.assumed)
        ci, co = st.requirements.dates.check_in, st.requirements.dates.check_out
        with tr.step("compare_locations") as s:
            cards = suggest_locations(st.requirements, self.locations, self.catalog, ci, co)
            s["detail"] = f"«{name}» нет в каталоге → сравнил: " + ", ".join(c.name for c in cards)
        st.stage = TripStage.collecting
        names = ", ".join(lp.name for lp in self.locations.packs())
        text = f"«{name}» пока нет в каталоге — сейчас могу подобрать поездку: {names}. Сравнил их под ваш запрос:"
        return [TextBlock(markdown=text), DestinationsBlock(title="Куда могу предложить", items=cards)], []

    # ============================================================ планирование
    def _plan(self, st: TripState, tr: Tracer, changes: list[str], *, inspire: bool, only: set[str] | None = None) -> Out:
        req = st.requirements
        today = self.today()

        imp = implicit_preferences(req)
        if imp:
            req.preferences += imp
            tr.note("implicit_prefs", ", ".join(p.key.value for p in imp))

        question = critical_question(req, st.asked_paths, st.clarifications_asked)
        if question is not None and not st.variants:
            st.clarifications_asked += 1
            st.asked_paths += question.paths
            st.stage = TripStage.collecting
            tr.note("clarify", question.text)
            understood = self._understood_text(req)
            return [TextBlock(markdown=understood), question], []
        if not req.dates.check_in and not req.dates.month:  # лимит вопросов исчерпан — предполагаем
            st.requirements, _ = apply_patch(
                req, [PatchOp(op="set", path="dates.month", value=today.month % 12 + 1)], source=Source.assumed
            )
            req = st.requirements

        if rops := assume_rooms(req):
            st.requirements, _ = apply_patch(req, rops, source=Source.assumed)
            req = st.requirements
            tr.note("assume_rooms", f"{req.guests.rooms} номера по двое")

        ops, assumed_paths = resolve_dates(req, today)
        if ops:
            st.requirements, _ = apply_patch(req, ops, source=Source.assumed)
            for p in assumed_paths:
                st.requirements.provenance[p] = Source.assumed
            req = st.requirements
            tr.note("resolve_dates", dates_label(req))

        ci, co = req.dates.check_in, req.dates.check_out
        assert ci and co
        chips = AssumptionsBlock(items=assumption_chips(req))

        svc = self._svc(st)
        loc = svc.pack
        if self.multi_location:
            chips.items.insert(0, self._location_chip(st))
        choose_location = self.multi_location and req.provenance.get("location_id") != Source.user and (inspire or not st.variants)
        if choose_location:
            # Если города нет, не выдаём молча дефолтный Сочи: сначала даём выбрать направление.
            with tr.step("compare_locations") as s:
                exclude = {x for x in self.locations.ids if only and x not in only}
                cards = suggest_locations(req, self.locations, self.catalog, ci, co, exclude=exclude)
                s["detail"] = ", ".join(c.name for c in cards)
            st.stage = TripStage.collecting
            text = (
                f"Сравнил направления на {dates_label(req)}: климат, цены на эти даты, перелёт и документы. Выберите — подберу отели."
                if inspire
                else "Подобрал направления под ваш запрос. Выберите город — затем сравню конкретные отели."
            )
            chips.items = [c for c in chips.items if c.path != "location_id"]  # город как раз и выбираем
            title = "Куда поехать" if inspire else "Выберите направление"
            return [TextBlock(markdown=text), chips, DestinationsBlock(title=title, items=cards)], [c.choose for c in cards[:3]]
        if inspire and not req.district_ids:
            with tr.step("inspire") as s:
                cards = suggest_districts(req, loc, self.catalog, svc.pricing, self.transfer, ci, co)
                s["detail"] = ", ".join(c.name for c in cards)
            st.stage = TripStage.collecting
            text = f"Вот районы ({loc.name}), которые подходят под ваш запрос. Выберите — или покажу лучшие отели по всем сразу."
            return [TextBlock(markdown=text), chips, DestinationsBlock(items=cards)], [
                QuickReply(label="Показать лучшие везде", action=ApplyPatchAction(ops=[PatchOp(op="unset", path="district_ids")]))
            ]

        previous = list(st.variants)
        chosen = st.variant(st.selected_variant_id) if st.selected_variant_id else None
        if chosen is not None and st.stage not in (TripStage.selected, TripStage.in_cart):
            chosen = None
        prev_rec = chosen or (st.variant(st.recommended_variant_id) if st.recommended_variant_id else None)
        prev_stage = st.stage
        hotels = self.catalog.list_hotels(loc.id)
        with tr.step("search_rank") as s:
            rr = svc.ranker.rank(req, hotels, ci, co, previous=previous, pinned_hotel_id=chosen.hotel.id if chosen else None)
            st_ = rr.stats
            s["detail"] = (
                f"{loc.name}: {st_.hotels_total} отелей → после фильтров {st_.after_hard_filters} → "
                f"с ценами в бюджете {st_.with_offers} → показано {len(rr.variants)}"
            )
        with tr.step("validate") as s:
            for v in rr.variants:
                v.issues = self.validator.validate_variant(v, req, loc)
            s["detail"] = f"замечаний: {sum(len(v.issues) for v in rr.variants)}"
        st.last_stats = rr.stats

        keep_choice = st.stage in (TripStage.selected, TripStage.in_cart)  # выбор пользователя не теряем
        if not rr.variants and rr.ticket_issue:
            if not keep_choice:
                st.stage = TripStage.collecting
            text = (
                f"С билетами не получилось: {rr.ticket_issue}. Отели есть — можно ослабить условие по билетам или подобрать только отель."
            )
            return [
                TextBlock(markdown=text),
                NoResultsBlock(reason=f"Билеты: {rr.ticket_issue}", rejected={"нет подходящих билетов": 1}, relaxations=rr.relaxations),
                chips,
            ], [QuickReply(label=r.label, action=ApplyPatchAction(ops=r.ops, label=r.label)) for r in rr.relaxations]
        if not rr.variants:
            top = Counter(rr.stats.rejected).most_common(3)
            facts = {
                "kind": "no_results",
                "top_rejections": [(reject_label(k), n) for k, n in top],
                "relaxations": [r.label for r in rr.relaxations],
            }
            if not keep_choice:
                st.stage = TripStage.collecting
            return [
                TextBlock(markdown=self.nlg.summarize_turn(st, facts)),
                NoResultsBlock(
                    reason="Нет вариантов под все условия",
                    rejected={reject_label(k): n for k, n in rr.stats.rejected.items()},
                    relaxations=rr.relaxations,
                ),
                chips,
            ], [QuickReply(label=r.label, action=ApplyPatchAction(ops=r.ops, label=r.label)) for r in rr.relaxations]

        st.previous_variants = previous
        st.variants = rr.variants
        st.recommended_variant_id = rr.recommended_id
        st.selected_variant_id = None
        st.stage = TripStage.proposing
        new_rec = rr.variants[0]

        blocks: list[UIBlock] = []
        if rr.pinned is not None:
            if rr.pinned.kept:
                tr.note("replan", f"выбранный отель {q(rr.pinned.hotel_name)} сохранён")
                # выбор пользователя сохраняем: тот же этап, та же корзина (с новой ценой/датами)
                st.selected_variant_id = new_rec.id
                st.stage = TripStage.selected
                if prev_stage == TripStage.in_cart:
                    return self._refresh_cart(st, prev_rec, new_rec, changes, tr)  # type: ignore[arg-type]
            else:
                tr.note("replan", f"{q(rr.pinned.hotel_name)} больше не подходит: {rr.pinned.reason}")
                if st.cart_id:
                    st.cart_id = None  # корзина собрана под старый отель — не оформляем её по ошибке
                    changes = [*changes, "корзину с прежним отелем очистил"]
        if prev_rec is not None and changes:
            diff = self._diff(prev_rec, new_rec, changes, pinned=rr.pinned)
            blocks.append(TextBlock(markdown=self.nlg.summarize_turn(st, {"kind": "replanned", "summary": diff.summary})))
            blocks.append(diff)
        else:
            facts = {
                "kind": "proposals",
                "count": len(rr.variants),
                "considered": rr.stats.hotels_total,
                "dates": dates_label(req),
                "guests": guests_label(req),
            }
            blocks.append(TextBlock(markdown=self.nlg.summarize_turn(st, facts)))
        if any(c.startswith("Время прибытия") for c in changes):
            # «прилетаем в 7:30, успеем заселиться?» — отвечаем расчётом валидатора, а не догадкой
            arrival = [i.message for i in new_rec.issues if i.code in ("early_arrival", "late_arrival")]
            ok = f"Заселение в {q(new_rec.hotel.name)} с {new_rec.hotel.check_in_time} — успеваете."
            blocks.append(TextBlock(markdown=" ".join(arrival) or ok))
        if loc.entry and (prev_rec is None or prev_rec.hotel.id not in {h.id for h in hotels}):
            # впервые показываем варианты за границей — одной строкой про документы (подробно — в корзине и по вопросу)
            blocks.append(TextBlock(markdown=f"**{loc.name}, {loc.country}:** {loc.entry.visa} {loc.entry.passport}"))
        blocks += [chips, VariantsBlock(items=rr.variants, recommended_id=rr.recommended_id, stats=rr.stats)]
        if prev_rec is None or any(c.startswith(("Даты", "Заезд", "Выезд", "Месяц")) for c in changes):
            blocks += self._warmer_elsewhere(st, tr)  # подсказка про тёплое море — один раз, а не на каждую правку
        return blocks, self._proposal_suggestions(st)

    def _location_chip(self, st: TripState) -> AssumptionChip:
        req = st.requirements
        loc = self._loc(st)
        others = [lp for lp in self.locations.packs() if lp.id != loc.id][:4]
        return AssumptionChip(
            path="location_id",
            label="Направление",
            value_label=loc.name if not loc.is_abroad else f"{loc.name}, {loc.country}",
            source=req.provenance.get("location_id", Source.default),
            options=[
                QuickReply(label=lp.name, action=ApplyPatchAction(ops=[PatchOp(op="set", path="location_id", value=lp.id)], label=lp.name))
                for lp in others
            ],
        )

    def _warmer_elsewhere(self, st: TripState, tr: Tracer) -> list[UIBlock]:
        """Хотят купаться, город мы предположили сами, а море холодное → покажем, где в эти даты тепло."""
        req = st.requirements
        if not self.multi_location or req.provenance.get("location_id") == Source.user:
            return []
        if not any(
            p.source == Source.user and p.key in (PrefKey.near_sea, PrefKey.beach_first_line, PrefKey.sea_view) for p in req.preferences
        ):
            return []
        ci, co = req.dates.check_in, req.dates.check_out
        if not ci or not wants_beach(req):
            return []
        loc = self._loc(st)
        c = beach_climate(loc, self.catalog, ci.month)
        if c is None or c.sea_temp_c is None or c.sea_temp_c >= WARM_SEA_C:
            return []
        with tr.step("compare_locations") as s:
            cards = [
                x
                for x in suggest_locations(req, self.locations, self.catalog, ci, co, exclude={loc.id}, limit=3)
                if x.climate and x.climate.sea_temp_c is not None and x.climate.sea_temp_c >= WARM_SEA_C
            ]
            s["detail"] = f"море {loc.label_in} ~{c.sea_temp_c:.0f}° → теплее: " + (", ".join(x.name for x in cards) or "нет")
        if not cards:
            return []
        text = (
            f"Учтите: море {loc.label_in} в {MONTHS_PREP[ci.month - 1]} около {c.sea_temp_c:.0f}° — прохладно для купания. "
            "Если хочется именно купаться, вот где в эти даты тепло:"
        )
        return [TextBlock(markdown=text), DestinationsBlock(title="Где море теплее", items=cards)]

    @staticmethod
    def _logistics_only(st: TripState, ops: list[PatchOp]) -> bool:
        """Изменилось только время/способ прибытия, а вариант уже выбран → не ищем заново, а перепроверяем."""
        logistics = {"transport.arrival_time", "transport.departure_time", "transport.arrival_mode", "transport.arrival_hub_id"}
        return (
            bool(ops)
            and st.stage in (TripStage.selected, TripStage.in_cart)
            and all(o.path in logistics for o in ops)
            and not wants_tickets(st.requirements)  # билеты подбираем заново, а не только перепроверяем
        )

    def _recheck(self, st: TripState, tr: Tracer) -> Out:
        v = self._current_variant(st)
        if v is None:
            return self._ask_for_details()
        with tr.step("validate") as s:
            v.issues = self.validator.validate_variant(v, st.requirements, self._loc(st))
            s["detail"] = "перепроверил состыковки выбранного варианта"
        if st.stage == TripStage.in_cart and (cart := self.booking.repo.get_cart(st.cart_id or "")):
            cart.issues = self.validator.validate_cart(cart, v, st.requirements)
            self.booking.repo.save_cart(cart)
            fix = self._autofix(st, v, tr)
            if fix is not None:
                fb, fs = self._fix_blocks(st, v, fix)
                return [CartBlock(cart=cart), *fb], fs
            sugg = [i.fix for i in cart.issues if i.fix] + [QuickReply(label="Оформить", action=CheckoutAction())]
            problems = [i for i in cart.issues if i.severity.value != "info"]
            text = ("Проверил состыковки: " + " ".join(i.message for i in problems)) if problems else "Проверил состыковки — всё сходится."
            return [TextBlock(markdown=text), CartBlock(cart=cart)], sugg
        return self._select(st, v, tr)

    # ============================================================ выбор/сравнение
    def _resolve_variant(self, st: TripState, ids: list[str], ordinal: int | None) -> Variant | None:
        for vid in ids:
            if v := st.variant(vid):
                return v
        if ordinal and 1 <= ordinal <= len(st.variants):
            return st.variants[ordinal - 1]
        return None

    def _current_variant(self, st: TripState) -> Variant | None:
        return st.variant(st.selected_variant_id or "") or st.variant(st.recommended_variant_id or "")

    def _select(self, st: TripState, v: Variant, tr: Tracer | None = None) -> Out:
        if st.stage == TripStage.selected and st.selected_variant_id == v.id:
            # Старую карточку можно нажать повторно из истории диалога. Это не новый выбор.
            return [TextBlock(markdown="Этот вариант уже выбран. Можно добавить его в корзину или изменить условия поездки.")], [
                QuickReply(label="В корзину", action=AddToCartAction(variant_id=v.id)),
                QuickReply(label="Сравнить все", action=CompareAction()),
            ]
        st.selected_variant_id = v.id
        st.stage = TripStage.selected
        v.issues = self.validator.validate_variant(v, st.requirements, self._loc(st))
        blocks: list[UIBlock] = [
            TextBlock(markdown=self.nlg.summarize_turn(st, {"kind": "selected", "hotel": v.hotel.name})),
            VariantsBlock(items=[v], recommended_id=v.id, selected_id=v.id),
        ]
        fix = self._autofix(st, v, tr or Tracer())
        if fix is not None:
            fb, fs = self._fix_blocks(st, v, fix)
            return blocks + fb, fs
        if any(i.code == "arrival_unknown" for i in v.issues):
            blocks.append(
                QuestionBlock(
                    text=f"Во сколько вы будете {self._loc(st).label_in}? Проверю заселение и подскажу про ранний заезд.",
                    paths=["transport.arrival_time"],
                    quick_replies=arrival_quick_replies(),
                )
            )
        return blocks, [
            QuickReply(label="В корзину", action=AddToCartAction(variant_id=v.id)),
            QuickReply(label="Сравнить все", action=CompareAction()),
        ]

    def _compare(self, st: TripState, ids: list[str]) -> Out:
        vs = [v for v in st.variants if not ids or v.id in ids]
        if len(vs) < 2:
            return [TextBlock(markdown="Для сравнения нужно хотя бы два варианта.")], []

        def best(vals: list[float], low: bool) -> list[int]:
            t = min(vals) if low else max(vals)
            return [i for i, x in enumerate(vals) if x == t]

        def row(label: str, cells: list[str], vals: list[float] | None = None, low: bool = True) -> ComparisonRow:
            return ComparisonRow(label=label, cells=cells, best_index=best(vals, low) if vals else [])

        inf = 10**9
        rows = [
            row("Район", [v.hotel.district_name for v in vs]),
            row("Цена за всё", [fmt_rub(v.total_price) for v in vs], [v.total_price for v in vs]),
            row("Питание", [MEALS_RU[v.offer.meal_plan.value] for v in vs]),
            row(
                "Отмена",
                [
                    "бесплатно до " + f"{v.offer.cancellation.free_until:%d.%m}" if v.offer.cancellation.refundable else "невозвратный"
                    for v in vs
                ],
            ),
            row(
                "До моря",
                [f"{v.hotel.distance_to_sea_m} м" if v.hotel.distance_to_sea_m is not None else "горы" for v in vs],
                [v.hotel.distance_to_sea_m if v.hotel.distance_to_sea_m is not None else inf for v in vs],
            ),
            row(
                "Дорога",
                [f"{v.transfer_in.minutes} мин" if v.transfer_in else "—" for v in vs],
                [v.transfer_in.minutes if v.transfer_in else inf for v in vs],
            ),
            row("Рейтинг (демо)", [str(v.hotel.rating or "—") for v in vs], [v.hotel.rating or 0 for v in vs], low=False),
            row("Соответствие запросу", [f"{round(v.score * 100)}%" for v in vs], [v.score for v in vs], low=False),
            row(
                "Риски",
                [str(len([i for i in v.fit_warnings if i.severity.value != "info"])) for v in vs],
                [len([i for i in v.fit_warnings if i.severity.value != "info"]) for v in vs],
            ),
        ]
        return [ComparisonBlock(variant_ids=[v.id for v in vs], headers=[v.hotel.name for v in vs], rows=rows)], [
            QuickReply(label=f"Выбрать {q(v.hotel.name)}", action=SelectVariantAction(variant_id=v.id)) for v in vs
        ]

    # ============================================================ корзина/заказ
    def _add_to_cart(self, st: TripState, v: Variant, with_transfer: bool, tr: Tracer | None = None) -> Out:
        st.selected_variant_id = v.id
        v.issues = self.validator.validate_variant(v, st.requirements, self._loc(st))
        cart = self.booking.put_variant(st.session_id, st.cart_id, v, st.requirements, with_transfer)
        st.cart_id = cart.id
        st.stage = TripStage.in_cart
        fix = self._autofix(st, v, tr or Tracer())
        if fix is not None:
            fb, fs = self._fix_blocks(st, v, fix)
            return [TextBlock(markdown="Собрал поездку в корзину и проверил состыковки."), CartBlock(cart=cart), *fb], fs
        sugg = [QuickReply(label="Оформить", action=CheckoutAction())]
        for i in cart.issues:
            if i.fix and i.code != "unconfirmed_assumptions":  # подтверждение спросим при оформлении
                sugg.insert(0, i.fix)
        blocks: list[UIBlock] = [TextBlock(markdown="Собрал поездку в корзину и проверил состыковки."), CartBlock(cart=cart)]
        if any(i.code == "arrival_unknown" for i in cart.issues):
            blocks.append(
                QuestionBlock(
                    text=f"Во сколько вы будете {self._loc(st).label_in}?",
                    paths=["transport.arrival_time"],
                    quick_replies=arrival_quick_replies(),
                )
            )
        return blocks, sugg

    def _cart_edit(self, st: TripState, a: AddExtraAction | RemoveCartItemAction) -> Out:
        cart = self.booking.repo.get_cart(st.cart_id or "")
        v = self._current_variant(st)
        if cart is None or v is None:
            return [TextBlock(markdown="Корзина пуста.")], []
        try:
            if isinstance(a, AddExtraAction):
                cart = self.booking.add_extra(cart, v, st.requirements, a.kind)
            else:
                cart = self.booking.remove_item(cart, a.item_id, v, st.requirements)
        except BookingError as e:
            return [TextBlock(markdown=str(e)), CartBlock(cart=cart)], []
        return [CartBlock(cart=cart)], [QuickReply(label="Оформить", action=CheckoutAction())]

    def _confirm_checkout(self, st: TripState) -> Out:
        """Понимание через LLM не создаёт заказ: только показывает корзину и кнопку подтверждения."""
        cart = self.booking.repo.get_cart(st.cart_id or "")
        if cart is None:
            return [TextBlock(markdown="Сначала добавьте вариант в корзину.")], []
        return [TextBlock(markdown="Проверьте корзину и подтвердите оформление кнопкой ниже."), CartBlock(cart=cart)], [
            QuickReply(label="Оформить заказ", action=CheckoutAction())
        ]

    def _checkout(self, st: TripState, guest_name: str | None) -> Out:
        cart = self.booking.repo.get_cart(st.cart_id or "")
        if cart is None:
            return [TextBlock(markdown="Сначала добавьте вариант в корзину.")], []
        # Повтор клика, повтор доставки запроса или retry клиента не должен создать второй заказ.
        if st.order_ids:
            previous = self.booking.repo.get_order(st.order_ids[-1])
            if previous is not None and previous.status == OrderStatus.awaiting_payment:
                return [TextBlock(markdown=f"Заказ **{previous.number}** уже создан и ждёт оплаты."), OrderBlock(order=previous)], [
                    QuickReply(label=f"Оплатить {fmt_rub(previous.total)}", action=PayAction(order_id=previous.id))
                ]
        pending = self._pending(st)
        if pending:
            # Предположения допустимы для подбора, но не для покупки: подтверждаем явно.
            lines = "\n".join(f"• {v}" for _, v in pending)
            return [
                QuestionBlock(
                    text=f"Перед оформлением проверьте, что я понял верно — это я предположил сам:\n{lines}",
                    paths=[g for g, _ in pending],
                    quick_replies=[
                        QuickReply(
                            label="Всё верно — оформить",
                            action=ConfirmAssumptionsAction(paths=[g for g, _ in pending], then_checkout=True),
                        ),
                        QuickReply(
                            label="Сдвинуть на неделю",
                            action=ApplyPatchAction(ops=[PatchOp(op="shift_days", path="dates", value=7)], label="Сдвинуть на неделю"),
                        ),
                    ],
                ),
                CartBlock(cart=cart),
            ], []
        try:
            order = self.booking.checkout(cart, guest_name, variant=self._current_variant(st), req=st.requirements)
        except BookingError as e:
            return [TextBlock(markdown=str(e)), CartBlock(cart=cart)], []
        st.order_ids.append(order.id)
        return [
            TextBlock(markdown=f"Заказ **{order.number}** создан. Осталось оплатить (оплата имитируется)."),
            OrderBlock(order=order),
        ], [QuickReply(label=f"Оплатить {fmt_rub(order.total)}", action=PayAction(order_id=order.id))]

    def _pay(self, st: TripState, order_id: str) -> Out:
        if order_id not in st.order_ids:
            return [TextBlock(markdown="Этот заказ не относится к текущей поездке.")], []
        order = self.booking.repo.get_order(order_id)
        if order is None:
            return [TextBlock(markdown="Заказ не найден.")], []
        order = self.booking.pay(order)
        st.stage = TripStage.ordered
        return [
            TextBlock(markdown=f"Оплачено (имитация) — заказ **{order.number}** подтверждён. Хорошей поездки!"),
            OrderBlock(order=order),
        ], self._post_purchase_suggestions(order.id)

    def _post_purchase(self, st: TripState, kind: PostPurchaseKind, details: dict, *, order_id: str | None = None) -> Out:
        target_id = order_id or (st.order_ids[-1] if st.order_ids else None)
        if target_id is not None and target_id not in st.order_ids:
            return [TextBlock(markdown="Этот заказ не относится к текущей поездке.")], []
        order = self.booking.repo.get_order(target_id) if target_id else None
        if order is None or order.status == OrderStatus.awaiting_payment and kind != PostPurchaseKind.cancel:
            return [TextBlock(markdown="Это доступно после оформления заказа.")], []
        order, msg = self.booking.post_purchase(order, kind, details)
        return [TextBlock(markdown=msg), OrderBlock(order=order)], self._post_purchase_suggestions(order.id)

    def _confirm(self, st: TripState, a: ConfirmAssumptionsAction, tr: Tracer) -> Out:
        req = st.requirements
        if "location" in a.paths:
            req.provenance["location_id"] = Source.user
        if "dates" in a.paths:
            for p in ("dates.check_in", "dates.check_out", "dates.nights"):
                req.provenance[p] = Source.user
        if "guests" in a.paths:
            if req.guests.adults is None:
                req.guests.adults = 2
            req.provenance["guests.adults"] = Source.user
            if req.guests.rooms:
                req.provenance["guests.rooms"] = Source.user
            if req.guests.children_ages:
                req.provenance["guests.children_ages"] = Source.user
        tr.note("confirm", "пользователь подтвердил: " + ", ".join(a.paths))
        if a.then_checkout and st.cart_id:
            return self._checkout(st, None)
        return [TextBlock(markdown="Спасибо, зафиксировал.")], []

    def _autofix(self, st: TripState, v: Variant, tr: Tracer) -> Fix | None:
        with tr.step("autofix") as s:
            fix = find_fix(
                v,
                st.requirements,
                ranker=self._svc(st).ranker,
                validator=self.validator,
                location=self._loc(st),
                hotels=list(self.catalog.list_hotels(self._loc(st).id)),
                dismissed=set(st.dismissed_fixes),
            )
            s["detail"] = (
                f"проблема: {fix.problem.code} → искал {fix.problem.strategy} {fix.searched} → {q(fix.variant.hotel.name)}"
                if fix
                else "проблем, которые стоит чинить, нет"
            )
        return fix

    def _fix_blocks(self, st: TripState, v: Variant, fix: Fix) -> Out:
        alt = fix.variant
        if st.variant(alt.id) is None:
            st.variants.append(alt)
        diff = self._diff(v, alt, [fix.problem.strategy])
        text = (
            f"Заметил проблему: {fix.problem.message[0].lower() + fix.problem.message[1:]}\n\n"
            f"Я сам поискал {fix.problem.strategy} {fix.searched} — и нашёл {q(alt.hotel.name)}. "
            "Проверил его теми же правилами: проблема уходит. Заменить?"
        )
        replace = AddToCartAction(variant_id=alt.id) if st.stage == TripStage.in_cart else SelectVariantAction(variant_id=alt.id)
        return [TextBlock(markdown=text), VariantsBlock(items=[alt], recommended_id=alt.id), diff], [
            QuickReply(label=f"Заменить на {q(alt.hotel.name)}", action=replace),
            QuickReply(label=f"Оставить {q(v.hotel.name)}", action=DismissFixAction(code=fix.problem.code, hotel_id=v.hotel.id)),
        ]

    def _refresh_cart(self, st: TripState, before: Variant, after: Variant, changes: list[str], tr: Tracer) -> Out:
        """Пересборка при собранной корзине и сохранённом отеле: обновляем корзину, а не сбрасываем её."""
        old_cart = self.booking.repo.get_cart(st.cart_id or "")
        with_transfer = bool(old_cart and any(i.kind == CartItemKind.transfer for i in old_cart.items))
        extras = [
            i.kind for i in (old_cart.items if old_cart else []) if i.kind in (CartItemKind.early_check_in, CartItemKind.late_check_out)
        ]
        st.selected_variant_id = after.id
        cart = self.booking.put_variant(st.session_id, st.cart_id, after, st.requirements, with_transfer)
        for k in extras:
            try:
                cart = self.booking.add_extra(cart, after, st.requirements, k)
            except BookingError:
                pass
        st.cart_id = cart.id
        st.stage = TripStage.in_cart
        tr.note("cart", "корзина пересчитана под новые условия")
        diff = self._diff(before, after, changes)
        return [
            TextBlock(markdown=f"{diff.summary} Корзину обновил — оформлять заново ничего не нужно."),
            diff,
            CartBlock(cart=cart),
        ], [QuickReply(label="Оформить", action=CheckoutAction())]

    # ============================================================ вопросы
    def _answer(self, st: TripState, question: str) -> Out:
        v = self._current_variant(st)
        loc = self._loc(st)
        ctx: dict = {"location": {"name": loc.name, "country": loc.country, "abroad": loc.is_abroad}}
        if loc.entry:
            ctx["entry"] = loc.entry.model_dump(mode="json", exclude={"sources", "checked_at"})
        if v:
            h = self.catalog.get_hotel(v.hotel.id)
            ctx["hotel"] = {
                **v.hotel.model_dump(mode="json"),
                "description": h.description if h else None,
                "meal_plan": v.offer.meal_plan.value,
                "total_price": v.total_price,
            }
            d = loc.district(v.hotel.district_id)
            month = (st.requirements.dates.check_in or self.today()).month
            if d and (c := self.catalog.climate(loc.id, d.climate_zone, month)):
                ctx["climate"] = c.model_dump(mode="json")
        return [TextBlock(markdown=self.nlg.answer_question(question, st, ctx))], self._proposal_suggestions(st)

    # ============================================================ вспомогательное
    def _diff(self, before: Variant, after: Variant, changes: list[str], pinned=None) -> DiffBlock:  # noqa: ANN001
        items: list[DiffItem] = []
        kept: list[str] = []
        why_replaced = ""
        if before.hotel.id == after.hotel.id:
            kept.append(f"Отель {q(after.hotel.name)} сохранён" + (" — вы его выбрали" if pinned and pinned.kept else ""))
        else:
            items.append(DiffItem(label="Отель", before=before.hotel.name, after=after.hotel.name, impact="neutral"))
            if pinned is not None and not pinned.kept and pinned.reason:
                why_replaced = f" {q(before.hotel.name)} пришлось заменить: {pinned.reason}."
        delta = after.total_price - before.total_price
        items.append(
            DiffItem(
                label="Итого",
                before=fmt_rub(before.total_price),
                after=fmt_rub(after.total_price),
                impact="better" if delta < 0 else "worse" if delta > 0 else "neutral",
            )
        )
        if before.offer.meal_plan != after.offer.meal_plan:
            items.append(
                DiffItem(label="Питание", before=MEALS_RU[before.offer.meal_plan.value], after=MEALS_RU[after.offer.meal_plan.value])
            )
        else:
            kept.append("Питание то же")
        if before.offer.cancellation.refundable != after.offer.cancellation.refundable:
            items.append(
                DiffItem(
                    label="Отмена",
                    before="бесплатная" if before.offer.cancellation.refundable else "невозвратный",
                    after="бесплатная" if after.offer.cancellation.refundable else "невозвратный",
                    impact="better" if after.offer.cancellation.refundable else "worse",
                )
            )
        if before.offer.check_in != after.offer.check_in:
            items.append(
                DiffItem(
                    label="Даты",
                    before=f"{before.offer.check_in:%d.%m}–{before.offer.check_out:%d.%m}",
                    after=f"{after.offer.check_in:%d.%m}–{after.offer.check_out:%d.%m}",
                )
            )
        if before.outbound or after.outbound:

            def leg(v: Variant) -> str:
                o = v.outbound
                if o is None:
                    return "без билетов"
                return f"{'прямой' if o.stops == 0 else 'с пересадкой'}, {o.depart_at:%H:%M}–{o.arrive_at:%H:%M}"

            b_leg, a_leg = leg(before), leg(after)
            if b_leg != a_leg:
                better = after.outbound is not None and (before.outbound is None or after.outbound.stops < before.outbound.stops)
                items.append(DiffItem(label="Перелёт/поезд туда", before=b_leg, after=a_leg, impact="better" if better else "neutral"))
            else:
                kept.append("Билеты те же")
        if before.transfer_in and after.transfer_in and before.transfer_in.minutes != after.transfer_in.minutes:
            m1, m2 = before.transfer_in.minutes, after.transfer_in.minutes
            items.append(DiffItem(label="Дорога", before=f"{m1} мин", after=f"{m2} мин", impact="better" if m2 < m1 else "worse"))
        sign = "дешевле" if delta < 0 else "дороже" if delta > 0 else "та же цена"
        money = f"на {fmt_rub(abs(delta))} {sign}" if delta else sign
        summary = f"Пересобрал план ({'; '.join(changes)}).{why_replaced} Рекомендация: {q(after.hotel.name)}, {money}."
        return DiffBlock(summary=summary, requirement_changes=changes, items=items, kept=kept)

    def _understood_text(self, req: Requirements) -> str:
        parts = [guests_label(req)]
        if req.budget.amount:
            parts.append(f"до {fmt_rub(req.budget.amount)}")
        if req.preferences:
            parts.append(", ".join(PREF_LABELS[p.key].lower() for p in req.preferences[:4]))
        return "Понял: " + "; ".join(parts) + "."

    def _proposal_suggestions(self, st: TripState) -> list[QuickReply]:
        req = st.requirements
        out: list[QuickReply] = []
        if st.recommended_variant_id:
            out.append(QuickReply(label="Беру рекомендованный", action=SelectVariantAction(variant_id=st.recommended_variant_id)))
        cheaper = (
            [PatchOp(op="scale", path="budget.amount", value=0.85)]
            if req.budget.amount
            else [PatchOp(op="add", path="preferences", value={"key": "budget_value", "priority": "important"})]
        )
        out.append(QuickReply(label="Дешевле", action=ApplyPatchAction(ops=cheaper, label="Дешевле")))
        if any(d.kind.value == "sea" for d in self._loc(st).districts):
            near = QuickReply(
                label="Ближе к морю",
                action=ApplyPatchAction(
                    ops=[PatchOp(op="add", path="preferences", value={"key": "near_sea", "priority": "must", "value": 400})],
                    label="Ближе к морю",
                ),
            )
        else:  # в Тбилиси моря нет — предлагаем центр
            near = QuickReply(
                label="Ближе к центру",
                action=ApplyPatchAction(
                    ops=[PatchOp(op="add", path="preferences", value={"key": "city_center", "priority": "important"})],
                    label="Ближе к центру",
                ),
            )
        out.append(near)
        if req.dates.is_exact:
            out.append(
                QuickReply(label="+2 дня", action=ApplyPatchAction(ops=[PatchOp(op="shift_days", path="dates", value=2)], label="+2 дня"))
            )
        if len(st.variants) >= 2:
            out.append(QuickReply(label="Сравнить", action=CompareAction()))
        return out

    @staticmethod
    def _post_purchase_suggestions(order_id: str) -> list[QuickReply]:
        return [
            QuickReply(label="Ранний заезд", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.early_check_in)),
            QuickReply(label="Напомнить о поездке", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.reminder)),
            QuickReply(label="Добавить трансфер", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.add_transfer)),
        ]

    def _greeting(self) -> Out:
        text = "Расскажите, куда и когда хотите поехать. Остальное уточню по ходу."
        return [TextBlock(markdown=text)], []

    @staticmethod
    def _examples() -> list[QuickReply]:
        """Подсказки, когда запрос пустой или не про поездку (приветствие намеренно без них — см. _greeting)."""
        return [
            QuickReply(
                label="Семья, ноябрь, море", text="Хотим с женой и сыном 6 лет в ноябре на неделю к морю, до 120 тысяч, нужен бассейн"
            ),
            QuickReply(label="Где тепло в декабре?", text="Куда поехать на море в декабре, чтобы купаться? Летим из Москвы вдвоём"),
            QuickReply(label="Вдвоём в горы на НГ", text="Вдвоём на новогодние в горы, хочется романтично и чтобы был спа"),
            QuickReply(label="Тбилиси на выходные", text="В Тбилиси на выходные вдвоём, поесть и погулять по старому городу"),
        ]

    def _ask_for_details(self) -> Out:
        return [TextBlock(markdown="Расскажите, когда хотите поехать, кто едет и что важно — подберу варианты.")], self._examples()

    def _reset(self, st: TripState) -> Out:
        st.requirements = Requirements(location_id=self.location_id)
        st.variants, st.previous_variants = [], []
        st.recommended_variant_id = st.selected_variant_id = st.cart_id = None
        st.order_ids = []
        st.dismissed_fixes = []
        st.stage = TripStage.collecting
        st.clarifications_asked, st.asked_paths = 0, []
        return self._greeting()

    def _response(self, st: TripState, blocks: list[UIBlock], sugg: list[QuickReply], trace: list[TraceStep], mode: str) -> TurnResponse:
        return TurnResponse(
            session_id=st.session_id,
            version=st.version,
            stage=st.stage,
            blocks=blocks,
            suggestions=sugg,
            requirements=st.requirements,
            trace=trace,
            mode="llm" if mode == "llm" else "rules",
            # пока город выбираем (сравнение направлений), в путевом листе его не показываем
            location=None
            if st.stage == TripStage.collecting and st.requirements.provenance.get("location_id") != Source.user
            else self._summary(st),
        )

    def _summary(self, st: TripState) -> LocationSummary:
        loc = self._loc(st)
        return LocationSummary(
            id=loc.id,
            name=loc.name,
            country=loc.country,
            abroad=loc.is_abroad,
            hotels=len(self.catalog.list_hotels(loc.id)),
            tagline=loc.tagline,
        )


__all__ = ["MAX_QUESTIONS", "Agent", "SessionNotFound", "VersionConflict"]
