"""Ядро: патчи, NLU-правила, цены, ранжирование, валидатор."""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError
from trip_agent.agent.nlu_rules import RuleNlu
from trip_agent.requirements_ops import PatchError, apply_patch
from trip_agent.schemas import Intent, PatchOp, Preference, PrefKey, Priority, Requirements, Source, TripState

from .conftest import TODAY


def _state() -> TripState:
    from datetime import UTC, datetime

    return TripState(session_id="t", created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


# ---------------------------------------------------------------- патчи
def test_patch_shift_scale_prefs():
    r = Requirements()
    r, _ = apply_patch(
        r,
        [
            PatchOp(op="set", path="dates.check_in", value="2026-11-07"),
            PatchOp(op="set", path="dates.nights", value=7),
            PatchOp(op="set", path="budget.amount", value=100_000),
            PatchOp(op="add", path="preferences", value={"key": "pool", "priority": "must"}),
        ],
    )
    assert r.dates.check_out == date(2026, 11, 14) and r.dates.nights == 7
    r, ch = apply_patch(
        r, [PatchOp(op="shift_days", path="dates", value=2), PatchOp(op="scale", path="budget.amount", value=0.85)]
    )
    assert r.dates.check_in == date(2026, 11, 9) and r.dates.check_out == date(2026, 11, 16)
    assert r.budget.amount == 85_000 and len(ch) == 2
    r, _ = apply_patch(r, [PatchOp(op="remove", path="preferences", value="pool")])
    assert r.pref(PrefKey.pool) is None
    assert r.provenance["budget.amount"] == Source.user


def test_patch_path_whitelist_enforced():
    with pytest.raises(ValidationError):
        PatchOp(op="set", path="hotel.price", value=1)  # не в белом списке путей


def test_patch_invalid_value_raises():
    with pytest.raises(PatchError):
        apply_patch(Requirements(), [PatchOp(op="set", path="guests.adults", value=0)])


# ---------------------------------------------------------------- NLU правила
@pytest.mark.parametrize(
    ("text", "intent", "expect"),
    [
        (
            "Хотим с женой и сыном 6 лет в ноябре на неделю к морю, до 120 тысяч",
            Intent.plan_trip,
            {"guests.adults": 2, "guests.children_ages": [6], "dates.month": 11, "dates.nights": 7, "budget.amount": 120_000},
        ),
        (
            "с 10 по 17 декабря вдвоём",
            Intent.plan_trip,
            {"dates.check_in": "2026-12-10", "dates.check_out": "2026-12-17", "guests.adults": 2},
        ),
        ("бюджет 8 тысяч за ночь", Intent.plan_trip, {"budget.amount": 8000, "budget.scope": "per_night"}),
        ("не знаю куда поехать, хочу тихо", Intent.inspire, {}),
        ("сравни варианты", Intent.compare, {}),
        ("начать заново", Intent.reset, {}),
    ],
)
def test_rule_nlu(text, intent, expect):
    res = RuleNlu().parse(text, _state(), TODAY)
    assert res.intent == intent
    got = {o.path: o.value for o in res.patch if o.op == "set"}
    for k, v in expect.items():
        assert got.get(k) == v, f"{k}: {got.get(k)!r} != {v!r} (patch={res.patch})"


def test_rule_nlu_pets_and_shift():
    st = _state()
    st.turn_count = 2
    st.requirements.dates.check_in = date(2026, 11, 7)
    res = RuleNlu().parse("а если на два дня позже и с собакой?", st, TODAY)
    ops = {(o.op, o.path): o.value for o in res.patch}
    assert ops[("shift_days", "dates")] == 2
    assert ops[("add", "guests.pets")] == "dog"
    assert res.intent == Intent.modify


# ---------------------------------------------------------------- цены
def test_pricing_deterministic_and_capacity(app, hotels):
    pricing = app.agent.pricing
    req = Requirements()
    req.guests.adults = 2
    h = hotels[0]
    a = pricing.quote(h, req, date(2026, 11, 7), date(2026, 11, 14))
    b = pricing.quote(h, req, date(2026, 11, 7), date(2026, 11, 14))
    assert [o.price_total for o in a] == [o.price_total for o in b]
    assert all(o.is_demo_price for o in a)
    req.guests.adults = 9
    assert pricing.quote(h, req, date(2026, 11, 7), date(2026, 11, 14)) == []


# ---------------------------------------------------------------- ранжирование
def _req() -> Requirements:
    r = Requirements()
    r.guests.adults = 2
    r.dates.check_in, r.dates.check_out = date(2026, 11, 7), date(2026, 11, 14)
    return r


def test_ranking_respects_hard_constraints(app, hotels):
    r = _req()
    r.guests.pets = ["dog"]
    r.budget.amount = 90_000
    r.preferences = [Preference(key=PrefKey.pool, priority=Priority.must)]
    res = app.agent.ranker.rank(r, hotels, r.dates.check_in, r.dates.check_out)
    assert res.variants, "ожидаем варианты на фикстурах"
    by_id = {h.id: h for h in hotels}
    for v in res.variants:
        h = by_id[v.hotel.id]
        assert h.pets.allowed is True, "собака: только отели, где ТОЧНО можно"
        assert v.total_price <= 90_000
        assert {"pool", "heated_pool", "indoor_pool"} & {a.value for a in h.amenities}
        assert v.breakdown and abs(sum(c.contribution for c in v.breakdown) - v.score) < 0.01
    assert len({v.hotel.id for v in res.variants}) == len(res.variants), "варианты — разные отели"


def test_ranking_impossible_gives_relaxations(app, hotels):
    r = _req()
    r.budget.amount = 3_000
    res = app.agent.ranker.rank(r, hotels, r.dates.check_in, r.dates.check_out)
    assert not res.variants
    assert res.stats.rejected.get("over_budget")
    assert any(x.results_count > 0 for x in res.relaxations), "должны предложить, что ослабить"


# ---------------------------------------------------------------- валидатор
def test_validator_early_arrival(app, hotels):
    r = _req()
    res = app.agent.ranker.rank(r, hotels, r.dates.check_in, r.dates.check_out)
    v = res.variants[0]
    r.transport.arrival_time = time(7, 0)
    issues = app.agent.validator.validate_variant(v, r)
    assert any(i.code == "early_arrival" for i in issues)
    r.transport.arrival_time = None
    assert any(i.code == "arrival_unknown" for i in app.agent.validator.validate_variant(v, r))
