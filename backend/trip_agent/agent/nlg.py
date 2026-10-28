"""Генерация текста ответа. Владелец: ML1.

TemplateNlg — детерминированные шаблоны (работают всегда, в т.ч. без LLM).
LlmNlg — «очеловечивает» шаблонный текст, НО с проверкой заземления: каждое число, название
в «кавычках», месяц и обещание («гарантируем», «бесплатная отмена», «успеете») в ответе модели
должны встречаться в фактах; иначе отдаём шаблон. Так LLM не может выдумать цену, отель или дату.
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
_QUOTED = re.compile(r"«([^»]{2,80})»")
_BOLD_NAME = re.compile(r"\*\*([A-ZА-ЯЁ][^*:]{1,80})\*\*")
_MONTH_STEMS = ("январ", "феврал", "март", "апрел", r"ма[йяе]\b", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
_MONTHS = tuple(re.compile(r"\b" + m) for m in _MONTH_STEMS)
# обещания, которые агент даёт только по данным (тариф, правила отеля), но не от себя
_PROMISES = tuple(
    re.compile(p)
    for p in (
        r"гарантир",
        r"\bобязательно\b",
        r"\bточно\b",
        r"бесплатн",
        r"без доплат",
        r"100\s?%",
        r"\bуспе(?:ете|ем|ть|ваете)\b",
        r"возврат",
        r"скидк",
    )
)

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
                parts.append("Почему: " + "; ".join(_lc(x) for x in v.why[:2]) + ".")
            if v.tradeoffs:
                parts.append("Чем жертвуем: " + _lc(v.tradeoffs[0]) + ".")
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
        q = question.lower()
        loc = context.get("location") or {}
        if re.search(r"виз[аеуы]|паспорт|страховк|въезд|границ", q) and loc:
            e = context.get("entry")
            if not e:
                return f"{loc['name']} — поездка по России: загранпаспорт и виза не нужны."
            return " ".join(x for x in (e.get("passport"), e.get("visa"), e.get("insurance")) if x)
        if re.search(r"карт[аыуо]|налич|деньг|валют|оплат\w* на месте", q) and context.get("entry"):
            return context["entry"].get("money") or "Точных данных об оплате на месте нет."
        h = context.get("hotel")
        if not h:
            return "Сначала подберу варианты — тогда смогу ответить по конкретному отелю."
        am = set(h.get("amenities", []))
        if "бассейн" in q:
            return f"В {_q(h['name'])} " + ("есть бассейн." if am & {"pool", "heated_pool", "indoor_pool"} else "бассейна нет.")
        if re.search(r"мор|пляж", q):
            d = h.get("distance_to_sea_m")
            return f"До моря от {_q(h['name'])} около {d} м." if d is not None else "Моря рядом нет."
        if re.search(r"собак|живот|кош|пит", q):
            p = h.get("pets_allowed")
            return {
                True: "С животными можно.",
                False: "С животными нельзя.",
                None: "Правило для животных неизвестно — уточним у отеля.",
            }[p]
        if re.search(r"заезд|засел|выезд", q):
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
        "Используй ТОЛЬКО факты из черновика и JSON. Не добавляй новых чисел, цен, дат, названий отелей и районов, "
        "не обещай того, чего нет в фактах (бесплатная отмена, гарантии, «успеете»). Markdown допускается."
    )

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def summarize_turn(self, state: TripState, facts: dict) -> str:
        draft = super().summarize_turn(state, facts)
        if facts.get("kind") not in ("proposals", "replanned", "no_results"):
            return draft
        try:
            out = self.llm.complete_text(self.SYSTEM, f"Черновик:\n{draft}\n\nФакты:\n{json.dumps(facts, ensure_ascii=False, default=str)}")
        except Exception as e:
            log.warning("LLM NLG failed: %s", e)
            return draft
        if not _grounded(out, draft + json.dumps(facts, ensure_ascii=False, default=str)):
            log.info("LLM NLG output not grounded, using template")
            return draft
        return out if DEMO_NOTE in out else out + "\n\n" + DEMO_NOTE

    def answer_question(self, question: str, state: TripState, context: dict) -> str:
        base = super().answer_question(question, state, context)
        if not context.get("hotel") or re.search(r"виз[аеуы]|паспорт|страховк|въезд", question.lower()):
            return base  # правила въезда — только дословно из проверенных данных, без пересказа моделью
        try:
            out = self.llm.complete_text(
                "Ответь на вопрос путешественника строго по JSON-фактам. Если ответа в фактах нет — так и скажи. "
                "1–3 предложения, без выдуманных чисел.",
                f"Вопрос: {question}\nФакты: {json.dumps(context, ensure_ascii=False, default=str)}",
            )
        except Exception:
            return base
        # числа из вопроса («прилетаем в 7:30») повторять можно, а названия и обещания — только из фактов
        return out if _grounded(out, json.dumps(context, ensure_ascii=False, default=str) + base, numbers_also_from=question) else base


def _lc(s: str) -> str:
    """Первая буква строчная — имена собственные внутри не трогаем."""
    return s[:1].lower() + s[1:] if s else s


def _q(name: str) -> str:
    return name if "«" in name else f"«{name}»"


def _grounded(text: str, source: str, *, numbers_also_from: str = "") -> bool:
    """Ответ модели не содержит фактов, которых нет в source: чисел, названий, месяцев, обещаний."""
    src = re.sub(r"[\s ]", "", source + numbers_also_from)
    for m in _NUM.findall(text):
        n = re.sub(r"[\s ]", "", m)
        if len(n) >= 2 and n not in src:
            return False
    flat = re.sub(r"\s+", " ", source).lower()
    for name in _QUOTED.findall(text) + _BOLD_NAME.findall(text):
        if re.sub(r"\s+", " ", name).strip().lower() not in flat:
            return False  # отель/район, которого нет в фактах
    low = text.lower()
    if any(m.search(low) and not m.search(flat) for m in _MONTHS):
        return False  # дата, которой нет в фактах
    return not any(p.search(low) and not p.search(flat) for p in _PROMISES)
