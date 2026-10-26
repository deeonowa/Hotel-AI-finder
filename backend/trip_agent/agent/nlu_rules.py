"""NLU на правилах (регулярки, русский язык). Владелец: ML1.

Зачем, если есть LLM:
  * режим без ключа — жюри запустит даже без доступа к модели (агент работает, просто попроще);
  * fallback, когда LLM недоступна/ответила мусором;
  * быстрый эталон для eval: LLM-NLU должен быть НЕ хуже правил на тех же сценариях.
"""

from __future__ import annotations

import re
from datetime import date

from ..schemas import Intent, NluResult, PatchOp, PostPurchaseKind, TripStage, TripState

MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "мая": 5,
    "май": 5,
    "мае": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}
MONTH_RE = r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
NUM_WORDS = {
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "двое": 2,
    "трое": 3,
    "четверо": 4,
}
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "1": 1, "2": 2, "3": 3}

DISTRICTS = [
    (r"лазаревск", "lazarevskoe"),
    (r"дагомыс", "dagomys"),
    (r"центральн\w* район", "central"),
    (r"хост", "khosta"),
    (r"адлер", "adler"),
    (r"сириус|имеретин|олимпийск\w* парк", "sirius"),
    (r"красн\w* полян|роза хутор|эсто-?садок|горки город", "krasnaya_polyana"),
]

PREFS: list[tuple[str, str, str, float | int | None]] = [
    # (regex, key, priority, value)
    (r"перв\w* лини", "beach_first_line", "important", None),
    (r"вид\w* на море", "sea_view", "important", None),
    (r"\bморе\b|\bморя\b|у моря|к морю|пляж|покупаться", "near_sea", "important", None),
    (r"лыж|сноуборд|горнолыж|склон", "ski_access", "important", None),
    (r"\bгор[ыауе]?\b|в горах|горн\w* воздух", "mountains", "important", None),
    (r"тих|спокойн|уединен|без шума|не шумн", "quiet", "important", None),
    (r"тусов|ночн\w* жизн|клуб|вечеринк", "nightlife", "important", None),
    (r"в центре|центр города|рядом с центром", "city_center", "important", None),
    (r"детск\w* клуб|аниматор|анимаци", "kids_club", "important", None),
    (r"подогрев\w* бассейн|крыт\w* бассейн|тёпл\w* бассейн|тепл\w* бассейн", "heated_pool", "important", None),
    (r"бассейн", "pool", "important", None),
    (r"\bспа\b|\bspa\b|массаж|сауна|баня", "spa", "nice", None),
    (r"романт|годовщин|медов\w* месяц|предложение сделать", "romantic", "important", None),
    (r"всё включено|все включено|all inclusive|ультра", "all_inclusive", "important", None),
    (r"завтрак", "breakfast_included", "important", None),
    (r"бесплатн\w* отмен|можно отменить|возможност\w* отмен|возврат", "free_cancellation", "important", None),
    (r"парковк|на машине|на авто|на своей машине", "parking", "important", None),
    (r"коляск|инвалид|доступн\w* сред|пожил", "accessible", "important", None),
    (
        r"недалеко от аэропорта|короткий трансфер|без долгой дороги|не хочу долго ехать|ближе к аэропорту",
        "short_transfer",
        "important",
        None,
    ),
    (r"хорош\w* отзыв|высок\w* рейтинг", "high_rating", "important", None),
    (r"люкс|премиум|роскош|дорог\w* отел", "luxury", "important", None),
    (r"удалёнк|удаленк|поработать|ноутбук|рабоч\w* мест", "work_friendly", "nice", None),
    (r"природ|лес|водопад|треккинг|поход", "nature", "nice", None),
    (r"санатор|лечени|оздоров|процедур", "wellness", "important", None),
    (r"подешевле|дешевле|бюджетн|недорог|эконом", "budget_value", "important", None),
]


def _num(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUM_WORDS.get(token)


def _month(word: str) -> int | None:
    w = word.lower()
    for stem, m in MONTHS.items():
        if w.startswith(stem):
            return m
    return None


def _year_for(month: int, day: int, today: date) -> int:
    return today.year + (1 if (month, day) < (today.month, today.day) else 0)


class RuleNlu:
    def parse(self, text: str, state: TripState, today: date) -> NluResult:
        t = " " + text.lower().replace("ё", "е") + " "
        ops: list[PatchOp] = []
        assumed: list[str] = []
        has_req = state.turn_count > 0 and bool(
            state.variants or state.requirements.dates.month or state.requirements.dates.check_in
        )

        # ---- служебные интенты ------------------------------------------------
        if re.search(r"начать заново|начнем сначала|новый поиск|забудь вс", t):
            return NluResult(intent=Intent.reset, parser="rules")
        if state.stage == TripStage.ordered or state.order_ids:
            if re.search(r"ранн\w* заезд|заселиться раньше", t):
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=PostPurchaseKind.early_check_in, parser="rules")
            if re.search(r"поздн\w* выезд", t):
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=PostPurchaseKind.late_check_out, parser="rules")
            if re.search(r"трансфер", t):
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=PostPurchaseKind.add_transfer, parser="rules")
            if re.search(r"напомн|напомин", t):
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=PostPurchaseKind.reminder, parser="rules")
            if re.search(r"отмени\w* брон|отменить заказ", t):
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=PostPurchaseKind.cancel, parser="rules")
        if re.search(r"оформ|забронир|брониру|в корзину|оплат|\bберу\b|\bберем\b", t):
            return NluResult(intent=Intent.book, parser="rules")
        m = re.search(r"(перв|втор|трет|\b[123]\b)\w*\s*(вариант|отел)?", t)
        if re.search(r"выбира|выберу|давай|хочу|нравится|подходит", t) and m and re.search(r"вариант|отел|перв|втор|трет", t):
            return NluResult(
                intent=Intent.select_variant,
                variant_ordinal=ORDINALS.get(m.group(1)[:4].rstrip("ь"), None) or ORDINALS.get(m.group(1), None),
                parser="rules",
            )
        if re.search(r"сравн|чем отлича|в чем разниц|что лучше", t):
            return NluResult(intent=Intent.compare, parser="rules")

        # ---- вопрос о выбранном/показанном отеле (не путать с изменением условий) --------
        if state.variants and re.search(
            r"\b(есть ли|можно ли|разрешено ли|далеко ли|сколько|какой|какая|какие|где|во сколько|а там)\b", t
        ) and not re.search(r"вариант|найди|покажи|подбери|хочу|давай|поменя|измени", t):
            return NluResult(intent=Intent.ask_info, info_question=text.strip(), parser="rules")

        # ---- сдвиг дат ----------------------------------------------------------
        if mm := re.search(r"на (\w+) (дн\w*|недел\w*) (позже|раньше)", t):
            n = _num(mm.group(1)) or (1 if mm.group(1) in ("одну", "день") else None)
            if n:
                days = n * (7 if mm.group(2).startswith("недел") else 1) * (1 if mm.group(3) == "позже" else -1)
                ops.append(PatchOp(op="shift_days", path="dates", value=days))
        elif re.search(r"на (день|сутки) (позже|раньше)", t):
            ops.append(PatchOp(op="shift_days", path="dates", value=1 if "позже" in t else -1))

        # ---- точные даты --------------------------------------------------------
        if mm := re.search(r"(?:с\s+)?(\d{1,2})\s*(?:-|–|по|до)\s*(\d{1,2})\s+" + MONTH_RE, t):
            d1, d2, mon = int(mm.group(1)), int(mm.group(2)), _month(mm.group(3))
            if mon:
                y = _year_for(mon, d1, today)
                try:
                    ci, co = date(y, mon, d1), date(y, mon, d2)
                    if co > ci:
                        ops += [
                            PatchOp(op="set", path="dates.check_in", value=ci.isoformat()),
                            PatchOp(op="set", path="dates.check_out", value=co.isoformat()),
                        ]
                except ValueError:
                    pass
        elif mm := re.search(r"(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?\s*(?:-|–|по|до)\s*(\d{1,2})\.(\d{1,2})", t):
            d1, m1, d2, m2 = map(int, mm.groups())
            try:
                y = _year_for(m1, d1, today)
                ci, co = date(y, m1, d1), date(y if m2 >= m1 else y + 1, m2, d2)
                ops += [
                    PatchOp(op="set", path="dates.check_in", value=ci.isoformat()),
                    PatchOp(op="set", path="dates.check_out", value=co.isoformat()),
                ]
            except ValueError:
                pass
        elif mm := re.search(r"с\s+(\d{1,2})\s+" + MONTH_RE, t):
            mon = _month(mm.group(2))
            if mon:
                d1 = int(mm.group(1))
                try:
                    ops.append(
                        PatchOp(op="set", path="dates.check_in", value=date(_year_for(mon, d1, today), mon, d1).isoformat())
                    )
                except ValueError:
                    pass
        elif mm := re.search(r"\bв\s+" + MONTH_RE, t) or re.search(r"\b" + MONTH_RE + r"\b", t):
            mon = _month(mm.group(1))
            if mon and not re.search(r"\d\s+" + mm.group(1), t):
                ops.append(PatchOp(op="set", path="dates.month", value=mon))
        if re.search(r"на новогодн|на новый год", t):
            y = today.year if today.month < 12 or today.day < 28 else today.year + 1
            ops += [
                PatchOp(op="set", path="dates.check_in", value=date(y, 12, 30).isoformat()),
                PatchOp(op="set", path="dates.check_out", value=date(y + 1, 1, 3).isoformat()),
            ]
            assumed += ["dates.check_in", "dates.check_out"]
        if re.search(r"на (эти |ближайшие )?выходные", t):
            days_to_fri = (4 - today.weekday()) % 7 or 7
            fri = date.fromordinal(today.toordinal() + days_to_fri)
            ops += [
                PatchOp(op="set", path="dates.check_in", value=fri.isoformat()),
                PatchOp(op="set", path="dates.check_out", value=date.fromordinal(fri.toordinal() + 2).isoformat()),
            ]

        # ---- длительность -------------------------------------------------------
        if not any(o.op == "shift_days" for o in ops):
            if re.search(r"на (одну |1 )?недел", t):
                ops.append(PatchOp(op="set", path="dates.nights", value=7))
            elif re.search(r"на (две|2) недел", t):
                ops.append(PatchOp(op="set", path="dates.nights", value=14))
            elif mm := re.search(r"на (\w+) (ноч\w*|дн\w*|сут\w*)", t):
                n = _num(mm.group(1))
                if n:
                    ops.append(PatchOp(op="set", path="dates.nights", value=n))

        # ---- гости --------------------------------------------------------------
        adults = None
        if re.search(r"вдво[её]м|с муж|с жен|с девушк|с парн|с подруг|с друг\b|с другом|пара\b|парой", t):
            adults = 2
        if re.search(r"втро[её]м", t) and not re.search(r"ребен|дет", t):
            adults = 3
        if mm := re.search(r"(\w+) взросл", t):
            adults = _num(mm.group(1)) or adults
        if re.search(r"\b(один|одна|сам|сама|одному|одной)\b", t) and adults is None:
            adults = 1
        if adults:
            ops.append(PatchOp(op="set", path="guests.adults", value=adults))

        kids: list[int] = []
        for mm in re.finditer(r"(?:ребенк\w*|ребен\w*|сын\w*|доч\w*|малыш\w*)\s*(\d{1,2})\s*(?:лет|год)", t):
            kids.append(int(mm.group(1)))
        if mm := re.search(r"дет\w*\s*(\d{1,2})\s*(?:и|,)\s*(\d{1,2})\s*(?:лет|год)", t):
            kids += [int(mm.group(1)), int(mm.group(2))]
        if not kids:
            if mm := re.search(r"(\w+)\s+(?:дет|ребен)", t):
                n = _num(mm.group(1))
                if n and n < 6:
                    kids = [7] * n
                    assumed.append("guests.children_ages")
            elif re.search(r"с ребен|с малыш|с сыном|с дочк|с дочер|с детьми", t):
                kids = [7] if not re.search(r"с детьми", t) else [7, 7]
                assumed.append("guests.children_ages")
        if kids:
            ops.append(PatchOp(op="set", path="guests.children_ages", value=kids))
            if adults is None and state.requirements.guests.adults is None:
                ops.append(PatchOp(op="set", path="guests.adults", value=2))
                assumed.append("guests.adults")

        if re.search(r"без (собак|кош|кот|животн|питомц)", t):
            ops.append(PatchOp(op="set", path="guests.pets", value=[]))
        else:
            if re.search(r"собак|пес\b|псом|щен|\bдог\b", t):
                ops.append(PatchOp(op="add", path="guests.pets", value="dog"))
            if re.search(r"\bкот\b|котом|кошк", t):
                ops.append(PatchOp(op="add", path="guests.pets", value="cat"))

        # ---- бюджет -------------------------------------------------------------
        if mm := re.search(
            r"(?:до|бюджет\w*|за|не дороже|максимум|в пределах|около|примерно|уложиться в)\s*(\d[\d\s]*[\d])?\s*(\d+)?\s*(тыс\w*|т\.?р\.?|к\b|k\b|руб\w*|₽|р\b)?",  # noqa: E501
            t,
        ):
            raw = (mm.group(1) or mm.group(2) or "").replace(" ", "")
            unit = mm.group(3) or ""
            if raw.isdigit():
                amount = int(raw)
                if unit.startswith(("тыс", "т", "к", "k")) or amount < 1000:
                    amount *= 1000
                if 1000 <= amount <= 5_000_000 and unit:
                    ops.append(PatchOp(op="set", path="budget.amount", value=amount))
                    if re.search(r"за ночь|в сутки|за сутки|ночь", t[mm.start() : mm.end() + 15]):
                        ops.append(PatchOp(op="set", path="budget.scope", value="per_night"))
                    elif re.search(r"на отель|за отель|за проживание|на проживание", t):
                        ops.append(PatchOp(op="set", path="budget.scope", value="hotel_total"))
                    if re.search(r"около|примерно|плюс-минус", t):
                        ops.append(PatchOp(op="set", path="budget.flex_pct", value=10))
        if not any(o.path == "budget.amount" for o in ops):
            if re.search(r"бюджет меньше|подешевле|дешевле|экономнее", t) and state.requirements.budget.amount:
                ops.append(PatchOp(op="scale", path="budget.amount", value=0.85))
            elif re.search(r"можно дороже|бюджет больше|подороже", t) and state.requirements.budget.amount:
                ops.append(PatchOp(op="scale", path="budget.amount", value=1.25))

        # ---- районы, транспорт ----------------------------------------------------
        districts = [did for rx, did in DISTRICTS if re.search(rx, t)]
        if districts:
            ops.append(PatchOp(op="set", path="district_ids", value=sorted(set(districts))))
        if re.search(r"прилет|прилеч|самолет|рейс|аэропорт|перелет", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="plane"))
        elif re.search(r"поезд|ласточк|\bжд\b|ж/д|вокзал", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="train"))
        elif re.search(r"на машине|на авто|своим ходом", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="car"))
        if mm := re.search(r"(?:прилет\w*|приезж\w*|прибыва\w*|прилеча\w*)\D{0,12}(\d{1,2})[:.](\d{2})", t):
            ops.append(PatchOp(op="set", path="transport.arrival_time", value=f"{int(mm.group(1)):02d}:{mm.group(2)}"))
        elif mm := re.search(r"(?:прилет\w*|приезж\w*|прибыва\w*)\s+(утром|днем|вечером|ночью)", t):
            ops.append(
                PatchOp(
                    op="set",
                    path="transport.arrival_time",
                    value={"утром": "08:00", "днем": "13:00", "вечером": "19:00", "ночью": "01:00"}[mm.group(1)],
                )
            )
            assumed.append("transport.arrival_time")
        if mm := re.search(r"(?:улета\w*|обратн\w* рейс|уезжа\w*|отъезд\w*)\D{0,12}(\d{1,2})[:.](\d{2})", t):
            ops.append(PatchOp(op="set", path="transport.departure_time", value=f"{int(mm.group(1)):02d}:{mm.group(2)}"))
        if mm := re.search(r"\bиз\s+(москв\w*|питер\w*|санкт-петербург\w*|спб|екатеринбург\w*|казан\w*|новосибирск\w*)", t):
            city = {
                "москв": "Москва",
                "питер": "Санкт-Петербург",
                "санкт": "Санкт-Петербург",
                "спб": "Санкт-Петербург",
                "екате": "Екатеринбург",
                "казан": "Казань",
                "новос": "Новосибирск",
            }
            ops.append(
                PatchOp(
                    op="set", path="transport.origin_city", value=next(v for k, v in city.items() if mm.group(1).startswith(k))
                )
            )

        # ---- предпочтения ---------------------------------------------------------
        seen: set[str] = set()
        for rx, key, prio, val in PREFS:
            if key in seen or not re.search(rx, t):
                continue
            if key == "pool" and "heated_pool" in seen:
                continue
            if key == "near_sea" and ("beach_first_line" in seen or re.search(r"без моря|не на море", t)):
                continue
            if key == "budget_value" and any(o.path == "budget.amount" for o in ops):
                continue
            neg = re.search(r"(?:не нужн\w*|без)\s+(?:" + rx + ")", t)
            if neg:
                ops.append(PatchOp(op="remove", path="preferences", value=key))
                continue
            must = re.search(r"(?:обязательно|только|точно|непременно)[^.,!?]{0,25}(?:" + rx + ")", t) or re.search(
                r"(?:" + rx + r")[^.!?]{0,30}(?:обязательно|обязателен|обязательна|непременно)", t
            )
            ops.append(
                PatchOp(
                    op="add",
                    path="preferences",
                    value={"key": key, "priority": "must" if must else prio, **({"value": val} if val else {})},
                )
            )
            seen.add(key)
        if mm := re.search(r"(\d)\s*(?:звезд|\*|★)", t):
            ops.append(
                PatchOp(op="add", path="preferences", value={"key": "min_stars", "priority": "must", "value": int(mm.group(1))})
            )

        # ---- интент ----------------------------------------------------------------
        if re.search(r"не знаю куда|куда поехать|куда лучше|посоветуй\w* (район|куда)|предложи\w* (район|куда)|где лучше", t):
            return NluResult(intent=Intent.inspire, patch=ops, assumed_paths=assumed, parser="rules")
        if ops:
            return NluResult(
                intent=Intent.modify if has_req else Intent.plan_trip, patch=ops, assumed_paths=assumed, parser="rules"
            )
        if "?" in t or re.search(r"\b(есть ли|можно ли|сколько|какой|какая|какие|далеко ли|где)\b", t):
            return NluResult(intent=Intent.ask_info, info_question=text.strip(), parser="rules")
        if re.search(r"привет|здравств|добрый|спасибо|благодар", t):
            return NluResult(intent=Intent.smalltalk, parser="rules")
        if re.search(r"отел|поездк|отдох|путешеств|сочи|море|гор", t):
            return NluResult(intent=Intent.plan_trip, parser="rules")
        return NluResult(intent=Intent.unclear, confidence=0.2, parser="rules")
