"""Генерация текста ответа. Владелец: ML1.

TemplateNlg — детерминированные шаблоны (работают всегда, в т.ч. без LLM).
LlmNlg — «очеловечивает» шаблонный текст, НО с проверкой заземления: каждое число в ответе
модели должно встречаться в фактах; иначе отдаём шаблон. Так LLM не может выдумать цену.
"""

from __future__ import annotations

import json
import logging
import re

from ..ports import LLMClient
from ..requirements_ops import fmt_rub
from ..schemas import TripState

log = logging.getLogger(__name__)
_NUM = re.compile(r"\d[\d\s ]*\d|\d")

DEMO_NOTE = "_Цены и наличие — демонстрационные; отели и климат — из открытых данных._"


class TemplateNlg:
    def summarize_turn(self, state: TripState, facts: dict) -> str:
        kind = facts.get("kind")
        if kind == "proposals":
            v = state.variants[0]
            parts = [
                f"Нашёл {facts['count']} {'вариант' if facts['count'] == 1 else 'варианта' if facts['count'] < 5 else 'вариантов'} "
                f"из {facts['considered']} отелей ({facts['dates']}, {facts['guests']}).",
                f"Рекомендую **{v.hotel.name}** ({v.hotel.district_name}) — {fmt_rub(v.total_price)} за всё.",
            ]
            if v.why:
                parts.append("Почему: " + "; ".join(v.why[:2]).lower() + ".")
            if v.tradeoffs:
                parts.append("Чем жертвуем: " + v.tradeoffs[0].lower() + ".")
            return " ".join(parts) + "\n\n" + DEMO_NOTE
        if kind == "no_results":
            reasons = ", ".join(f"{label} — {n}" for label, n in facts["top_rejections"])
            text = f"Под все условия ничего не нашлось. Чаще всего отсеивалось: {reasons}."
            if facts["relaxations"]:
                text += " Вот что можно ослабить — я заранее проверил, сколько вариантов появится:"
            return text
        if kind == "replanned":
            return facts["summary"]
        if kind == "selected":
            return f"Отличный выбор: **{facts['hotel']}**. Проверил состыковки — смотрите замечания ниже."
        return facts.get("text", "")

    def answer_question(self, question: str, state: TripState, context: dict) -> str:
        """Шаблонные ответы на частые вопросы по фактам отеля; остальное — честно «не знаю»."""
        h = context.get("hotel")
        if not h:
            return "Сначала подберу варианты — тогда смогу ответить по конкретному отелю."
        q = question.lower()
        am = set(h.get("amenities", []))
        if "бассейн" in q:
            return f"В {_q(h['name'])} " + ("есть бассейн." if am & {"pool", "heated_pool", "indoor_pool"} else "бассейна нет.")
        if re.search(r"мор|пляж", q):
            d = h.get("distance_to_sea_m")
            return f"До моря от {_q(h['name'])} около {d} м." if d is not None else "Это горный район, моря рядом нет."
        if re.search(r"собак|живот|кош|пит", q):
            p = h.get("pets_allowed")
            return {
                True: "С животными можно.",
                False: "С животными нельзя.",
                None: "Правило для животных неизвестно — уточним у отеля.",
            }[p]
        if re.search(r"заезд|заселен|выезд", q):
            return f"Заселение с {h['check_in_time']}, выезд до {h['check_out_time']}."
        if re.search(r"погод|температур|тепло|холодно|вода", q) and context.get("climate"):
            c = context["climate"]
            return (
                f"По климатическим нормам: днём около {c['air_temp_day_c']}°C, ночью {c['air_temp_night_c']}°C"
                + (f", море около {c['sea_temp_c']}°C" if c.get("sea_temp_c") is not None else "")
                + "."
            )
        return "Точного ответа в данных нет — уточню у отеля при бронировании."


class LlmNlg(TemplateNlg):
    SYSTEM = (
        "Ты — дружелюбный тревел-агент Островка. Перепиши черновик ответа живым русским языком, 2–4 предложения. "
        "Используй ТОЛЬКО факты из черновика и JSON. Не добавляй новых чисел, цен, названий. Markdown допускается."
    )

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def summarize_turn(self, state: TripState, facts: dict) -> str:
        draft = super().summarize_turn(state, facts)
        if facts.get("kind") not in ("proposals", "replanned"):
            return draft
        try:
            out = self.llm.complete_text(
                self.SYSTEM, f"Черновик:\n{draft}\n\nФакты:\n{json.dumps(facts, ensure_ascii=False, default=str)}"
            )
        except Exception as e:
            log.warning("LLM NLG failed: %s", e)
            return draft
        if not _grounded(out, draft + json.dumps(facts, ensure_ascii=False, default=str)):
            log.info("LLM NLG output not grounded, using template")
            return draft
        return out if DEMO_NOTE in out else out + "\n\n" + DEMO_NOTE

    def answer_question(self, question: str, state: TripState, context: dict) -> str:
        base = super().answer_question(question, state, context)
        if not context.get("hotel"):
            return base
        try:
            out = self.llm.complete_text(
                "Ответь на вопрос путешественника строго по JSON-фактам. Если ответа в фактах нет — так и скажи. "
                "1–3 предложения, без выдуманных чисел.",
                f"Вопрос: {question}\nФакты: {json.dumps(context, ensure_ascii=False, default=str)}",
            )
        except Exception:
            return base
        return out if _grounded(out, json.dumps(context, ensure_ascii=False, default=str) + question) else base


def _q(name: str) -> str:
    return name if "«" in name else f"«{name}»"


def _grounded(text: str, source: str) -> bool:
    src = re.sub(r"[\s ]", "", source)
    for m in _NUM.findall(text):
        n = re.sub(r"[\s ]", "", m)
        if len(n) >= 2 and n not in src:
            return False
    return True
