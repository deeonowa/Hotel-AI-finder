"""Проверка ПОНИМАНИЯ запросов (NLU): правильно ли агент интерпретировал требования. Владельцы: ML1 + ML2.

    python eval/nlu_eval.py                      # правила, оба набора
    python eval/nlu_eval.py --mode llm           # LLM из .env (с откатом на правила при сбое)
    python eval/nlu_eval.py --set holdout -v     # только отложенный набор, с разбором ошибок

Наборы:
  eval/nlu_dev.jsonl      — рабочий: на нём улучшаем правила и промпт;
  eval/nlu_holdout.jsonl  — ОТЛОЖЕННЫЙ: написан до правок NLU, на нём не тюним. Показывает качество
                            на формулировках, которых система «не видела».
  Кейсы d50+ в рабочем наборе — направления в других странах (город, страна, район, «из» ≠ «куда»,
  города не из каталога); написаны вместе с правилами, поэтому это рабочий, а не отложенный набор.

Проверяется смысл, а не форма: патч применяется к требованиям, и сравниваются ИТОГОВЫЕ значения
полей (а не конкретные операции). Для «изменений» (plan: true) сначала строится реальное состояние
диалога базовым запросом BASE.

Метрики: intent_accuracy, slot_accuracy (доля верно извлечённых ожидаемых полей), case_accuracy
(кейс верен целиком), safety (инъекции не меняют требования). Отчёт — eval/nlu_report.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from llm_pace import pace, quota_note  # noqa: E402
from trip_agent.config import Settings  # noqa: E402
from trip_agent.container import build  # noqa: E402
from trip_agent.requirements_ops import PatchError, apply_patch  # noqa: E402
from trip_agent.schemas import Source, TurnRequest  # noqa: E402

TODAY = date(2026, 10, 26)
BASE = "в Сочи вдвоём с 10 по 17 ноября у моря, до 100 тысяч, с бассейном"


def _get(req, path: str):
    if path == "children_count":
        return len(req.guests.children_ages)
    obj = req
    for part in path.split("."):
        obj = getattr(obj, part)
    if isinstance(obj, list):
        return [getattr(x, "value", x) for x in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()[:5] if path.endswith("_time") else obj.isoformat()
    return getattr(obj, "value", obj)


def run_case(app, case: dict, base_state) -> dict:
    agent = app.agent
    state = base_state.model_copy(deep=True) if case.get("plan") else agent.get_state(agent.new_session().session_id)
    res = agent.nlu.parse(case["text"], state, TODAY)
    out = {"id": case["id"], "text": case["text"], "intent": res.intent.value, "parser": res.parser, "errors": []}

    want = case.get("intent_any") or [case["intent"]]
    out["intent_ok"] = res.intent.value in want
    if not out["intent_ok"]:
        out["errors"].append(f"intent {res.intent.value} ∉ {want}")

    try:
        req, _ = apply_patch(state.requirements, res.patch, source=Source.user, today=TODAY)
    except PatchError as e:
        req = state.requirements
        out["errors"].append(f"patch error: {e}")

    slots_total = slots_ok = 0
    for path, val in (case.get("expect") or {}).items():
        slots_total += 1
        got = res.variant_ordinal if path == "variant_ordinal" else _get(req, path)
        if isinstance(val, list) and isinstance(got, list):
            ok = sorted(map(str, got)) == sorted(map(str, val))
        else:
            ok = got == val
        slots_ok += ok
        if not ok:
            out["errors"].append(f"{path}: {got!r} ≠ {val!r}")
    keys = {p.key.value: p for p in req.preferences}
    for k in case.get("prefs", []):
        slots_total += 1
        slots_ok += k in keys
        if k not in keys:
            out["errors"].append(f"нет пожелания {k}")
    for k in case.get("must", []):
        slots_total += 1
        ok = k in keys and keys[k].priority.value == "must"
        slots_ok += ok
        if not ok:
            out["errors"].append(f"{k} должно быть обязательным")
    for k in case.get("prefs_absent", []):
        slots_total += 1
        slots_ok += k not in keys
        if k in keys:
            out["errors"].append(f"{k} должно быть убрано")
    if "unknown" in case:  # город не из каталога: не подменяем, а честно называем
        slots_total += 1
        ok = res.unknown_destination == case["unknown"] and not any(o.path == "location_id" for o in res.patch)
        slots_ok += ok
        if not ok:
            out["errors"].append(f"unknown_destination: {res.unknown_destination!r} ≠ {case['unknown']!r}")
    if "candidates" in case:  # «Сочи или Анталья?» — сравнить именно названные
        slots_total += 1
        ok = sorted(res.candidate_location_ids) == sorted(case["candidates"])
        slots_ok += ok
        if not ok:
            out["errors"].append(f"candidates: {res.candidate_location_ids} ≠ {case['candidates']}")
    if case.get("no_patch"):
        slots_total += 1
        ok = not res.patch
        slots_ok += ok
        if not ok:
            out["errors"].append(f"инъекция изменила требования: {[o.path for o in res.patch]}")
        out["safety_ok"] = ok
    out["slots_total"], out["slots_ok"] = slots_total, slots_ok
    out["ok"] = not out["errors"]
    return out


def evaluate(app, cases: list[dict]) -> tuple[dict, list[dict]]:
    sid = app.agent.new_session().session_id
    app.agent.turn(sid, TurnRequest(text=BASE))
    base_state = app.agent.get_state(sid)
    results = [run_case(app, c, base_state) for c in cases]
    n = len(results) or 1
    st = sum(r["slots_total"] for r in results) or 1
    safety = [r["safety_ok"] for r in results if "safety_ok" in r]
    metrics = {
        "cases": len(results),
        "intent_accuracy": round(sum(r["intent_ok"] for r in results) / n, 3),
        "slot_accuracy": round(sum(r["slots_ok"] for r in results) / st, 3),
        "case_accuracy": round(sum(r["ok"] for r in results) / n, 3),
        "safety": f"{sum(safety)}/{len(safety)}" if safety else "—",
        "parser": ", ".join(sorted({r["parser"] for r in results})),
        # доля кейсов, которые разобрала модель (остальные — правила: сбой, 429, низкая уверенность)
        "llm_share": round(sum(r["parser"] == "llm" for r in results) / n, 3),
    }
    return metrics, results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rules", "llm", "auto"], default="rules")
    ap.add_argument("--set", choices=["dev", "holdout", "holdout2", "all"], default="all")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--rpm", type=float, default=18, help="LLM: не чаще N запросов в минуту (бесплатные модели — 20)")
    ap.add_argument("--out", default=str(ROOT / "eval" / "nlu_report.md"))
    args = ap.parse_args()

    app = build(Settings(agent_mode=args.mode, llm_for_nlg=False), today=lambda: TODAY, database_url="memory://")
    pace(app, args.rpm)
    sets = ["dev", "holdout", "holdout2"] if args.set == "all" else [args.set]
    lines = [f"# NLU eval ({app.mode})", "", f"Сегодня (фиксировано): {TODAY}. База для изменений: «{BASE}».", ""]
    for name in sets:
        path = ROOT / "eval" / f"nlu_{name}.jsonl"
        cases = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        metrics, results = evaluate(app, cases)
        lines += [f"## {name}", "", "| метрика | значение |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in metrics.items()]
        bad = [r for r in results if not r["ok"]]
        if bad:
            lines += ["", "| кейс | запрос | ошибки |", "|---|---|---|"]
            lines += [f"| {r['id']} | {r['text'][:70]} | {'; '.join(r['errors'])[:160]} |" for r in bad]
        lines.append("")
        print(f"[{name}] {metrics}")
        if args.verbose:
            for r in bad:
                print(f"  ✗ {r['id']}: {r['text']}\n      {r['errors']}")
    if note := quota_note():
        lines += ["", *note]
        print("\n".join(note))
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
