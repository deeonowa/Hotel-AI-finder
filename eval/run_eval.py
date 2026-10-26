"""Прогон тестовых сценариев и метрики качества агента. Владелец: ML2 (+ ML1 для NLU-кейсов).

    python eval/run_eval.py                 # режим правил (без LLM)
    python eval/run_eval.py --mode llm      # с LLM из .env
    python eval/run_eval.py --only pets     # по тегу

Метрики (как в задании трека):
  completion_rate        — доля сценариев, достигших цели (вплоть до оплаченного заказа)
  avg_clarifications     — среднее число уточняющих вопросов
  turns_to_first_offer   — сколько ходов до первых вариантов
  ms_to_first_offer      — время до первых вариантов (мс)
  constraint_violations  — вариантов, нарушивших жёсткие требования (должно быть 0)
  logistics_errors       — блокирующих ошибок состыковок в корзине (должно быть 0)
  crashes                — исключений (должно быть 0)
Отчёт: eval/report.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from trip_agent.config import Settings  # noqa: E402
from trip_agent.container import build  # noqa: E402
from trip_agent.schemas import TurnRequest  # noqa: E402
from trip_agent.validation.rules import pending_confirmations  # noqa: E402

TODAY = date(2026, 10, 26)


def _blocks(r, t):
    return [b for b in r.blocks if b.type == t]


def run_scenario(app, sc: dict) -> dict:
    agent = app.agent
    hotels = {h.id: h for h in app.catalog.list_hotels("sochi")}
    loc = app.catalog.get_location("sochi")
    r = agent.new_session()
    sid = r.session_id
    res = {
        "id": sc["id"],
        "goal": sc["goal"],
        "ok": False,
        "clarifications": 0,
        "turns_to_offer": None,
        "ms_to_offer": None,
        "violations": [],
        "logistics_errors": 0,
        "crash": None,
        "seen": set(),
        "trace": [],
    }
    t_start = time.perf_counter()
    last = r
    last_variants: list = []
    try:
        for i, turn in enumerate(sc["turns"], start=1):
            st = agent.get_state(sid)
            if turn.startswith("@"):
                cmd = turn[1:]
                target = st.selected_variant_id or st.recommended_variant_id
                action = {
                    "select": {"type": "select_variant", "variant_id": target},
                    "cart": {"type": "add_to_cart", "variant_id": target},
                    "checkout": {"type": "checkout", "guest_name": "Тест"},
                    "confirm": {"type": "confirm_assumptions", "paths": ["dates", "guests"], "then_checkout": True},
                    "second": {"type": "add_to_cart", "variant_id": st.variants[1].id if len(st.variants) > 1 else target},
                    "fix": _fix_action(r),
                    "keep": _keep_action(r),
                    "pay": {"type": "pay", "order_id": (st.order_ids or [""])[-1]},
                }[cmd]
                r = agent.turn(sid, TurnRequest(action=action))
            else:
                r = agent.turn(sid, TurnRequest(text=turn))
            last = r
            types = {b.type for b in r.blocks}
            res["seen"] |= types
            if res["turns_to_offer"] is None:  # уточнения считаем только ДО первой рекомендации
                res["clarifications"] += len(_blocks(r, "question"))
            if "variants" in types and res["turns_to_offer"] is None:
                res["turns_to_offer"] = i
                res["ms_to_offer"] = int((time.perf_counter() - t_start) * 1000)
            if vbs := _blocks(r, "variants"):
                last_variants = [v for vb in vbs for v in vb.items]
            for cb in _blocks(r, "cart"):
                # подтверждение предположений — это не ошибка состыковки, а осознанный «стоп» перед покупкой
                res["logistics_errors"] += sum(
                    1 for x in cb.cart.issues if x.severity.value == "blocker" and x.code != "unconfirmed_assumptions"
                )
            for ob in _blocks(r, "order"):
                if pending_confirmations(agent.get_state(sid).requirements):
                    res["violations"].append(f"заказ {ob.order.number} создан с неподтверждёнными предположениями")
            res["trace"] += [f"{t.name}:{t.detail}" for t in r.trace]
    except Exception as e:  # noqa: BLE001
        res["crash"] = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"

    st = agent.get_state(sid)
    checks = sc.get("checks", {})
    # жёсткие требования проверяем на ИТОГОВОЙ выдаче (после всех пересборок)
    for v in last_variants:
        res["violations"] += _check(v, checks, hotels, loc)
    # Интерпретация: итоговые требования должны совпасть с ожидаемыми (а не просто «показали карточки»)
    for path, val in (sc.get("expect_req") or {}).items():
        got = _req_get(st.requirements, path)
        if (sorted(map(str, got)) != sorted(map(str, val))) if isinstance(val, list) else got != val:
            res["violations"].append(f"понял не так: {path}={got!r}, ожидалось {val!r}")
    for key in sc.get("expect_prefs", []):
        if key not in {p.key.value for p in st.requirements.preferences}:
            res["violations"].append(f"не понял пожелание {key}")
    if (tr := sc.get("expect_trace")) and not any(tr in x for x in res["trace"]):
        res["violations"].append(f"в trace нет «{tr}»")
    if checks.get("kept_hotel") and st.selected_variant_id is None:
        res["violations"].append("выбранный отель потерян при пересборке")
    g, seen = sc["goal"], res["seen"]
    no_res_ok = "no_results" in seen and len(_relax(last)) >= max(1, checks.get("relaxations_min", 1))
    reached = {
        "order_paid": st.stage.value == "ordered",
        "variants": "variants" in seen,
        "diff": "diff" in seen,
        # «нет результатов» засчитываем ТОЛЬКО если агент объяснил причину и предложил проверенные ослабления
        "diff_or_no_results": "diff" in seen or no_res_ok,
        "variants_or_no_results": "variants" in seen or no_res_ok,
        "no_results": no_res_ok,
        "autofix": any(x.startswith("autofix:проблема") for x in res["trace"]),
        "destinations": "destinations" in seen,
        "selected": st.stage.value in ("selected", "in_cart"),
        "in_cart": st.stage.value == "in_cart",
        "in_cart_kept": st.stage.value == "in_cart" and st.selected_variant_id is not None,
        "no_crash": res["crash"] is None,
        "any_progress": bool(seen & {"variants", "question", "no_results", "destinations"}),
    }[g]
    if checks.get("no_variants") and "variants" in seen:
        res["violations"].append("variants shown for non-travel/injection input")
    if "max_clarifications" in checks and res["clarifications"] > checks["max_clarifications"]:
        res["violations"].append(f"too many questions: {res['clarifications']}")
    res["ok"] = reached and res["crash"] is None and not res["violations"]
    res["seen"] = sorted(seen)
    res.pop("trace")
    return res


def _req_get(req, path: str):
    if path == "children_count":
        return len(req.guests.children_ages)
    obj = req
    for part in path.split("."):
        obj = getattr(obj, part)
    if isinstance(obj, list):
        return [getattr(x, "value", x) for x in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return getattr(obj, "value", obj)


def _fix_action(r) -> dict:
    q = next((s for s in r.suggestions if s.label.startswith("Заменить") and s.action), None)
    return q.action.model_dump(mode="json") if q else {"type": "compare"}


def _keep_action(r) -> dict:
    q = next((s for s in r.suggestions if s.label.startswith("Оставить") and s.action), None)
    return q.action.model_dump(mode="json") if q else {"type": "compare"}


def _relax(r):
    return [x for b in r.blocks if b.type == "no_results" for x in b.relaxations]


def _check(v, checks: dict, hotels: dict, loc) -> list[str]:
    out = []
    h = hotels[v.hotel.id]
    d = loc.district(h.district_id)
    if (m := checks.get("budget_max")) and v.total_price > m:
        out.append(f"{v.hotel.name}: total {v.total_price} > {m}")
    if (m := checks.get("per_night_max")) and v.offer.price_per_night_avg > m:
        out.append(f"{v.hotel.name}: per night {v.offer.price_per_night_avg} > {m}")
    if checks.get("pets") and h.pets.allowed is not True:
        out.append(f"{v.hotel.name}: pets not allowed/unknown")
    if (am := checks.get("must_amenity_any")) and not set(am) & {a.value for a in h.amenities}:
        out.append(f"{v.hotel.name}: none of {am}")
    if checks.get("children") and h.adults_only:
        out.append(f"{v.hotel.name}: adults only")
    if (dist := checks.get("district")) and h.district_id != dist:
        out.append(f"{v.hotel.name}: district {h.district_id} != {dist}")
    if (k := checks.get("district_kind")) and d and d.kind.value != k:
        out.append(f"{v.hotel.name}: district kind {d.kind.value} != {k}")
    if (meal := checks.get("meal")) and v.offer.meal_plan.value != meal:
        out.append(f"{v.hotel.name}: meal {v.offer.meal_plan.value} != {meal}")
    if (ms := checks.get("meal_any")) and v.offer.meal_plan.value not in ms:
        out.append(f"{v.hotel.name}: meal {v.offer.meal_plan.value} not in {ms}")
    if checks.get("refundable") and not v.offer.cancellation.refundable:
        out.append(f"{v.hotel.name}: not refundable")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rules", "llm", "auto"], default="rules")
    ap.add_argument("--only", help="тег сценариев")
    ap.add_argument("--out", default=str(ROOT / "eval" / "report.md"))
    args = ap.parse_args()

    app = build(Settings(agent_mode=args.mode), today=lambda: TODAY, db_path=":memory:")
    scenarios = [json.loads(x) for x in (ROOT / "eval" / "scenarios.jsonl").read_text("utf-8").splitlines() if x.strip()]
    if args.only:
        scenarios = [s for s in scenarios if args.only in s.get("tags", [])]

    results = [run_scenario(app, s) for s in scenarios]
    n = len(results)
    offered = [r for r in results if r["turns_to_offer"]]
    metrics = {
        "mode": app.mode,
        "scenarios": n,
        "completion_rate": round(sum(r["ok"] for r in results) / n, 3) if n else 0,
        "avg_clarifications": round(sum(r["clarifications"] for r in results) / n, 2) if n else 0,
        "avg_turns_to_first_offer": round(sum(r["turns_to_offer"] for r in offered) / len(offered), 2) if offered else None,
        "avg_ms_to_first_offer": int(sum(r["ms_to_offer"] for r in offered) / len(offered)) if offered else None,
        "constraint_violations": sum(
            len([v for v in r["violations"] if ":" in v and not v.startswith(("понял не так", "не понял", "в trace"))]) for r in results
        ),
        "logistics_errors": sum(r["logistics_errors"] for r in results),
        "crashes": sum(1 for r in results if r["crash"]),
        "interpretation_errors": sum(len([v for v in r["violations"] if v.startswith(("понял не так", "не понял"))]) for r in results),
    }
    lines = [f"# Eval report ({metrics['mode']})", "", "| метрика | значение |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in metrics.items()]
    lines += ["", "| сценарий | цель | ок | вопросов | ход до вариантов | замечания |", "|---|---|---|---|---|---|"]
    for r in results:
        note = "; ".join(r["violations"])[:160] or (r["crash"] or "").split("\n")[0][:160]
        lines.append(
            f"| {r['id']} | {r['goal']} | {'✅' if r['ok'] else '❌'} | {r['clarifications']} | {r['turns_to_offer'] or '—'} | {note} |"
        )
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
