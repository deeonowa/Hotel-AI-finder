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
from ..ports import CatalogProvider, Nlg, Nlu, PricingProvider, Ranker, SessionStore, TransferProvider, Validator
from ..ranking.baseline import reject_label
from ..requirements_ops import PREF_LABELS, PatchError, apply_patch, fmt_rub
from ..schemas import (
    Action,
    AddExtraAction,
    AddToCartAction,
    ApplyPatchAction,
    AssumptionsBlock,
    CartBlock,
    CheckoutAction,
    CompareAction,
    ComparisonBlock,
    ComparisonRow,
    DestinationsBlock,
    DiffBlock,
    DiffItem,
    Intent,
    NluResult,
    NoResultsBlock,
    OrderBlock,
    OrderStatus,
    PatchOp,
    PayAction,
    PostPurchaseAction,
    PostPurchaseKind,
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
from ..validation.rules import arrival_quick_replies
from .inspiration import suggest_districts
from .policy import (
    MAX_QUESTIONS,
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
        pricing: PricingProvider,
        transfer: TransferProvider,
        ranker: Ranker,
        validator: Validator,
        nlu: Nlu,
        nlg: Nlg,
        store: SessionStore,
        booking: BookingService,
        location_id: str,
        mode: str = "rules",
        today: Callable[[], date] = date.today,
    ):
        self.catalog, self.pricing, self.transfer = catalog, pricing, transfer
        self.ranker, self.validator, self.nlu, self.nlg = ranker, validator, nlu, nlg
        self.store, self.booking = store, booking
        self.location_id = location_id
        self.location = catalog.get_location(location_id)
        self.mode = mode
        self.today = today

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
                return self._post_purchase(
                    st, PostPurchaseKind.change_dates, {"patch": [o.model_dump(mode="json") for o in nlu.patch]}
                )
            if not nlu.patch and i != Intent.inspire:
                return self._ask_for_details()
            changes = self._apply(st, nlu.patch, nlu.assumed_paths, tr)
            if changes is None:
                return [TextBlock(markdown="Не смог применить изменение — переформулируйте, пожалуйста.")], []
            if self._logistics_only(st, nlu.patch):
                return self._recheck(st, tr)
            return self._plan(st, tr, changes, inspire=(i == Intent.inspire))
        if i == Intent.select_variant:
            v = self._resolve_variant(st, nlu.target_variant_ids, nlu.variant_ordinal)
            return (
                self._select(st, v) if v else ([TextBlock(markdown="Какой вариант выбрать? Нажмите «Выбрать» на карточке.")], [])
            )
        if i == Intent.compare:
            return self._compare(st, nlu.target_variant_ids)
        if i == Intent.book:
            if st.cart_id and st.stage == TripStage.in_cart:
                return self._checkout(st, None)
            v = self._resolve_variant(st, nlu.target_variant_ids, nlu.variant_ordinal) or self._current_variant(st)
            return self._add_to_cart(st, v, True) if v else self._ask_for_details()
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
            return self._select(st, v) if v else ([TextBlock(markdown="Вариант устарел — обновил выдачу.")], [])
        if isinstance(a, CompareAction):
            return self._compare(st, a.variant_ids)
        if isinstance(a, AddToCartAction):
            v = st.variant(a.variant_id)
            return self._add_to_cart(st, v, a.with_transfer) if v else ([TextBlock(markdown="Вариант устарел.")], [])
        if isinstance(a, AddExtraAction | RemoveCartItemAction):
            return self._cart_edit(st, a)
        if isinstance(a, CheckoutAction):
            return self._checkout(st, a.guest_name)
        if isinstance(a, PayAction):
            return self._pay(st, a.order_id)
        if isinstance(a, PostPurchaseAction):
            return self._post_purchase(st, a.kind, a.details)
        if isinstance(a, ResetAction):
            return self._reset(st)
        return [TextBlock(markdown="Неизвестное действие.")], []

    # ============================================================ требования
    def _apply(self, st: TripState, ops: list[PatchOp], assumed: list[str], tr: Tracer) -> list[str] | None:
        with tr.step("apply_patch") as s:
            try:
                new_req, changes = apply_patch(st.requirements, ops, source=Source.user, today=self.today())
            except PatchError as e:
                s["detail"] = f"ошибка: {e}"
                return None
            for p in assumed:
                new_req.provenance[p] = Source.assumed
            st.requirements = new_req
            s["detail"] = "; ".join(changes) or "без изменений"
        return changes

    # ============================================================ планирование
    def _plan(self, st: TripState, tr: Tracer, changes: list[str], *, inspire: bool) -> Out:
        req = st.requirements
        today = self.today()

        imp = implicit_preferences(req)
        if imp:
            req.preferences += imp
            tr.note("implicit_prefs", ", ".join(p.key.value for p in imp))

        q = critical_question(req, st.asked_paths, st.clarifications_asked)
        if q is not None and not st.variants:
            st.clarifications_asked += 1
            st.asked_paths += q.paths
            st.stage = TripStage.collecting
            tr.note("clarify", q.text)
            understood = self._understood_text(req)
            return [TextBlock(markdown=understood), q], []
        if not req.dates.check_in and not req.dates.month:  # лимит вопросов исчерпан — предполагаем
            st.requirements, _ = apply_patch(
                req, [PatchOp(op="set", path="dates.month", value=today.month % 12 + 1)], source=Source.assumed
            )
            req = st.requirements

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

        if inspire and not req.district_ids:
            with tr.step("inspire") as s:
                cards = suggest_districts(req, self.location, self.catalog, self.pricing, self.transfer, ci, co)
                s["detail"] = ", ".join(c.name for c in cards)
            st.stage = TripStage.collecting
            text = "Вот районы Большого Сочи, которые подходят под ваш запрос. Выберите — или покажу лучшие отели по всем сразу."
            return [TextBlock(markdown=text), chips, DestinationsBlock(items=cards)], [
                QuickReply(label="Показать лучшие везде", action=ApplyPatchAction(ops=[PatchOp(op="unset", path="district_ids")]))
            ]

        previous = list(st.variants)
        prev_rec = st.variant(st.recommended_variant_id) if st.recommended_variant_id else None
        hotels = self.catalog.list_hotels(self.location_id)
        with tr.step("search_rank") as s:
            rr = self.ranker.rank(req, hotels, ci, co, previous=previous)
            st_ = rr.stats
            s["detail"] = (
                f"{st_.hotels_total} отелей → после фильтров {st_.after_hard_filters} → "
                f"с ценами в бюджете {st_.with_offers} → показано {len(rr.variants)}"
            )
        with tr.step("validate") as s:
            for v in rr.variants:
                v.issues = self.validator.validate_variant(v, req, self.location)
            s["detail"] = f"замечаний: {sum(len(v.issues) for v in rr.variants)}"
        st.last_stats = rr.stats

        if not rr.variants:
            top = Counter(rr.stats.rejected).most_common(3)
            facts = {
                "kind": "no_results",
                "top_rejections": [(reject_label(k), n) for k, n in top],
                "relaxations": [r.label for r in rr.relaxations],
            }
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
        if st.selected_variant_id and not st.variant(st.selected_variant_id):
            st.selected_variant_id = None
        st.stage = TripStage.proposing
        new_rec = rr.variants[0]

        blocks: list[UIBlock] = []
        if prev_rec is not None and changes:
            diff = self._diff(prev_rec, new_rec, changes)
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
        blocks += [chips, VariantsBlock(items=rr.variants, recommended_id=rr.recommended_id, stats=rr.stats)]
        return blocks, self._proposal_suggestions(st)

    @staticmethod
    def _logistics_only(st: TripState, ops: list[PatchOp]) -> bool:
        """Изменилось только время/способ прибытия, а вариант уже выбран → не ищем заново, а перепроверяем."""
        return (
            bool(ops)
            and st.stage in (TripStage.selected, TripStage.in_cart)
            and all(o.path.startswith("transport.") for o in ops)
        )

    def _recheck(self, st: TripState, tr: Tracer) -> Out:
        v = self._current_variant(st)
        if v is None:
            return self._ask_for_details()
        with tr.step("validate") as s:
            v.issues = self.validator.validate_variant(v, st.requirements, self.location)
            s["detail"] = "перепроверил состыковки выбранного варианта"
        if st.stage == TripStage.in_cart and (cart := self.booking.repo.get_cart(st.cart_id or "")):
            cart.issues = self.validator.validate_cart(cart, v, st.requirements)
            self.booking.repo.save_cart(cart)
            sugg = [i.fix for i in cart.issues if i.fix] + [QuickReply(label="Оформить", action=CheckoutAction())]
            problems = [i for i in cart.issues if i.severity.value != "info"]
            text = (
                ("Проверил состыковки: " + " ".join(i.message for i in problems))
                if problems
                else "Проверил состыковки — всё сходится."
            )
            return [TextBlock(markdown=text), CartBlock(cart=cart)], sugg
        return self._select(st, v)

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

    def _select(self, st: TripState, v: Variant) -> Out:
        st.selected_variant_id = v.id
        st.stage = TripStage.selected
        v.issues = self.validator.validate_variant(v, st.requirements, self.location)
        blocks: list[UIBlock] = [
            TextBlock(markdown=self.nlg.summarize_turn(st, {"kind": "selected", "hotel": v.hotel.name})),
            VariantsBlock(items=[v], recommended_id=v.id),
        ]
        if any(i.code == "arrival_unknown" for i in v.issues):
            blocks.append(
                QuestionBlock(
                    text="Во сколько вы будете в Сочи? Проверю заселение и подскажу про ранний заезд.",
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
                    "бесплатно до " + f"{v.offer.cancellation.free_until:%d.%m}"
                    if v.offer.cancellation.refundable
                    else "невозвратный"
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
    def _add_to_cart(self, st: TripState, v: Variant, with_transfer: bool) -> Out:
        st.selected_variant_id = v.id
        cart = self.booking.put_variant(st.session_id, st.cart_id, v, st.requirements, with_transfer)
        st.cart_id = cart.id
        st.stage = TripStage.in_cart
        sugg = [QuickReply(label="Оформить", action=CheckoutAction())]
        for i in cart.issues:
            if i.fix:
                sugg.insert(0, i.fix)
        blocks: list[UIBlock] = [TextBlock(markdown="Собрал поездку в корзину и проверил состыковки."), CartBlock(cart=cart)]
        if any(i.code == "arrival_unknown" for i in cart.issues):
            blocks.append(
                QuestionBlock(
                    text="Во сколько вы будете в Сочи?", paths=["transport.arrival_time"], quick_replies=arrival_quick_replies()
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

    def _checkout(self, st: TripState, guest_name: str | None) -> Out:
        cart = self.booking.repo.get_cart(st.cart_id or "")
        if cart is None:
            return [TextBlock(markdown="Сначала добавьте вариант в корзину.")], []
        try:
            order = self.booking.checkout(cart, guest_name)
        except BookingError as e:
            return [TextBlock(markdown=str(e)), CartBlock(cart=cart)], []
        st.order_ids.append(order.id)
        return [
            TextBlock(markdown=f"Заказ **{order.number}** создан. Осталось оплатить (оплата имитируется)."),
            OrderBlock(order=order),
        ], [QuickReply(label=f"Оплатить {fmt_rub(order.total)}", action=PayAction(order_id=order.id))]

    def _pay(self, st: TripState, order_id: str) -> Out:
        order = self.booking.repo.get_order(order_id)
        if order is None:
            return [TextBlock(markdown="Заказ не найден.")], []
        order = self.booking.pay(order)
        st.stage = TripStage.ordered
        return [
            TextBlock(markdown=f"Оплачено (имитация) — заказ **{order.number}** подтверждён. Хорошей поездки!"),
            OrderBlock(order=order),
        ], self._post_purchase_suggestions(order.id)

    def _post_purchase(self, st: TripState, kind: PostPurchaseKind, details: dict) -> Out:
        order = self.booking.repo.get_order(st.order_ids[-1]) if st.order_ids else None
        if order is None or order.status == OrderStatus.awaiting_payment and kind != PostPurchaseKind.cancel:
            return [TextBlock(markdown="Это доступно после оформления заказа.")], []
        order, msg = self.booking.post_purchase(order, kind, details)
        return [TextBlock(markdown=msg), OrderBlock(order=order)], self._post_purchase_suggestions(order.id)

    # ============================================================ вопросы
    def _answer(self, st: TripState, question: str) -> Out:
        v = self._current_variant(st)
        ctx: dict = {}
        if v:
            h = self.catalog.get_hotel(v.hotel.id)
            ctx["hotel"] = {
                **v.hotel.model_dump(mode="json"),
                "description": h.description if h else None,
                "meal_plan": v.offer.meal_plan.value,
                "total_price": v.total_price,
            }
            d = self.location.district(v.hotel.district_id)
            month = (st.requirements.dates.check_in or self.today()).month
            if d and (c := self.catalog.climate(self.location_id, d.climate_zone, month)):
                ctx["climate"] = c.model_dump(mode="json")
        return [TextBlock(markdown=self.nlg.answer_question(question, st, ctx))], self._proposal_suggestions(st)

    # ============================================================ вспомогательное
    def _diff(self, before: Variant, after: Variant, changes: list[str]) -> DiffBlock:
        items: list[DiffItem] = []
        kept: list[str] = []
        if before.hotel.id == after.hotel.id:
            kept.append(f"Отель {q(after.hotel.name)} сохранён")
        else:
            items.append(DiffItem(label="Отель", before=before.hotel.name, after=after.hotel.name))
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
                DiffItem(
                    label="Питание", before=MEALS_RU[before.offer.meal_plan.value], after=MEALS_RU[after.offer.meal_plan.value]
                )
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
        if before.transfer_in and after.transfer_in and before.transfer_in.minutes != after.transfer_in.minutes:
            m1, m2 = before.transfer_in.minutes, after.transfer_in.minutes
            items.append(DiffItem(label="Дорога", before=f"{m1} мин", after=f"{m2} мин", impact="better" if m2 < m1 else "worse"))
        sign = "дешевле" if delta < 0 else "дороже" if delta > 0 else "та же цена"
        money = f"на {fmt_rub(abs(delta))} {sign}" if delta else sign
        summary = f"Пересобрал план ({'; '.join(changes)}). Рекомендация: {q(after.hotel.name)}, {money}."
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
        out.append(
            QuickReply(
                label="Ближе к морю",
                action=ApplyPatchAction(
                    ops=[PatchOp(op="add", path="preferences", value={"key": "near_sea", "priority": "must", "value": 400})],
                    label="Ближе к морю",
                ),
            )
        )
        if req.dates.is_exact:
            out.append(
                QuickReply(
                    label="+2 дня", action=ApplyPatchAction(ops=[PatchOp(op="shift_days", path="dates", value=2)], label="+2 дня")
                )
            )
        if len(st.variants) >= 2:
            out.append(QuickReply(label="Сравнить", action=CompareAction()))
        return out

    @staticmethod
    def _post_purchase_suggestions(order_id: str) -> list[QuickReply]:
        return [
            QuickReply(label="Ранний заезд", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.early_check_in)),
            QuickReply(label="Напомнить о поездке", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.reminder)),
            QuickReply(
                label="Добавить трансфер", action=PostPurchaseAction(order_id=order_id, kind=PostPurchaseKind.add_transfer)
            ),
        ]

    def _greeting(self) -> Out:
        text = (
            f"Привет! Я подберу поездку в **{self.location.name}** — от идеи до оформления. "
            "Опишите своими словами: когда, кто едет, бюджет, чего хочется. Можно размыто — уточню только главное."
        )
        return [TextBlock(markdown=text)], self._examples()

    @staticmethod
    def _examples() -> list[QuickReply]:
        return [
            QuickReply(
                label="Семья, ноябрь, море",
                text="Хотим с женой и сыном 6 лет в ноябре на неделю к морю, до 120 тысяч, нужен бассейн",
            ),
            QuickReply(label="Вдвоём в горы на НГ", text="Вдвоём на новогодние в горы, хочется романтично и чтобы был спа"),
            QuickReply(label="Не знаю куда, с собакой", text="Не знаю куда поехать, хочу тихо и с собакой в декабре на неделю"),
            QuickReply(label="Один, подешевле", text="Еду один на ближайшие выходные, подешевле, прилетаю в 9:30"),
        ]

    def _ask_for_details(self) -> Out:
        return [
            TextBlock(markdown="Расскажите, когда хотите поехать, кто едет и что важно — подберу варианты.")
        ], self._examples()

    def _reset(self, st: TripState) -> Out:
        st.requirements = Requirements(location_id=self.location_id)
        st.variants, st.previous_variants = [], []
        st.recommended_variant_id = st.selected_variant_id = st.cart_id = None
        st.stage = TripStage.collecting
        st.clarifications_asked, st.asked_paths = 0, []
        return self._greeting()

    def _response(
        self, st: TripState, blocks: list[UIBlock], sugg: list[QuickReply], trace: list[TraceStep], mode: str
    ) -> TurnResponse:
        return TurnResponse(
            session_id=st.session_id,
            version=st.version,
            stage=st.stage,
            blocks=blocks,
            suggestions=sugg,
            requirements=st.requirements,
            trace=trace,
            mode="llm" if mode == "llm" else "rules",
        )


__all__ = ["MAX_QUESTIONS", "Agent", "SessionNotFound", "VersionConflict"]
