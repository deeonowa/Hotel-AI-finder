"""Темп запросов к LLM для eval: бесплатные модели OpenRouter ограничены ~20 запросами в минуту
(и 50 в сутки, пока на счёте меньше 10 кредитов).

Без паузы eval упирается в 429, клиент уходит в «остывание», и ходы молча разбираются правилами —
метрики LLM-режима получаются фальшивыми. Поэтому в LLM-режиме eval выдерживает темп, честно считает
долю ходов, разобранных моделью (llm_share), и пишет в отчёт, если провайдер исчерпал дневной лимит.
"""

from __future__ import annotations

import time

STATUS = {"calls": 0, "errors": 0, "daily_limit": False}


def pace(app, rpm: float) -> None:
    """Обернуть complete_json/complete_text клиента LLM паузой: не чаще rpm вызовов в минуту."""
    llm = getattr(app.agent.nlu, "llm", None)
    if llm is None or rpm <= 0:
        return
    gap = 60.0 / rpm
    last = [0.0]

    def wrap(fn):  # noqa: ANN001, ANN202
        def inner(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            wait = last[0] + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            STATUS["calls"] += 1
            try:
                return fn(*a, **kw)
            except Exception as e:
                STATUS["errors"] += 1
                STATUS["daily_limit"] |= "per-day" in str(e)
                raise

        return inner

    llm.complete_json = wrap(llm.complete_json)
    llm.complete_text = wrap(llm.complete_text)


def quota_note() -> list[str]:
    """Строки для отчёта: сколько было вызовов модели и не кончился ли дневной лимит."""
    if not STATUS["calls"]:
        return []
    out = [f"Вызовов LLM: {STATUS['calls']}, ошибок: {STATUS['errors']}."]
    if STATUS["daily_limit"]:
        out.append(
            "⚠️ Провайдер исчерпал дневной лимит бесплатных запросов — дальше ходы разбирали правила "
            "(см. llm_share). Это не качество модели, а квота ключа."
        )
    return out
