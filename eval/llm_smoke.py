"""Живая проверка гибридного режима: 10 ходов через настоящую модель из .env + искусственный отказ провайдера.

    python eval/llm_smoke.py              # 10 ходов, ~10–12 запросов к модели
    python eval/llm_smoke.py --fallback   # то же с недоступным провайдером: всё должно уйти в правила

Печатает по каждому ходу: время, кто разобрал (llm/rules), что отбросила проверка, итоговые требования.
Бесплатная модель OpenRouter без пополнения — 50 запросов в сутки: smoke укладывается, полный eval — нет.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from trip_agent.config import Settings  # noqa: E402
from trip_agent.container import build  # noqa: E402
from trip_agent.schemas import TurnRequest  # noqa: E402

DIALOGS = [
    ["Хотим с женой и сыном 6 лет к тёплому морю в декабре на неделю, до 250 тысяч, летим из Москвы"],
    ["Хочу заграницу", "в декабре на неделю вдвоём"],
    ["в Дубай вдвоём с 5 по 12 декабря", "Хочу заграницу но не в дубай", "Предложи другие отели"],
    ["а давай в Сибирь в феврале"],
    ["в Анталью с 10 по 17 ноября вдвоём, летим из Казани", "какие есть рейсы с багажом?"],
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallback", action="store_true", help="провайдер недоступен (127.0.0.1:9) — проверка отката на правила")
    args = ap.parse_args()
    extra = {"llm_base_url": "http://127.0.0.1:9/v1", "llm_timeout_s": 3} if args.fallback else {}
    app = build(Settings(agent_mode="llm", **extra), today=date.today, database_url="memory://")
    a = app.agent
    print(f"mode={app.mode} model={app.settings.llm_model} extra_body={app.settings.llm_extra_body}")
    turns = llm_turns = 0
    for dialog in DIALOGS:
        sid = a.new_session().session_id
        for text in dialog:
            t0 = time.perf_counter()
            r = a.turn(sid, TurnRequest(text=text))
            ms = int((time.perf_counter() - t0) * 1000)
            tr = {x.name: x.detail for x in r.trace}
            req = a.get_state(sid).requirements
            turns += 1
            llm_turns += r.mode == "llm"
            guard, back = tr.get("nlu_guard", "—"), tr.get("nlu_fallback", "—")
            print(f"\n» {text}\n  {ms} мс · {r.mode} · {tr.get('nlu')} · guard: {guard} · откат: {back}")
            print(f"  блоки: {[b.type for b in r.blocks]} · направление: {req.location_id} ({req.provenance.get('location_id', 'default')})"
                  f" · география: {req.geo_scope} · исключены: {req.excluded_location_ids}")  # fmt: skip
    print(f"\nходов: {turns}, разобрано моделью: {llm_turns}")


if __name__ == "__main__":
    main()
