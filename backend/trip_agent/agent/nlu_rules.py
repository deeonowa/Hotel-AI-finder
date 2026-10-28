"""NLU на правилах (регулярки, русский язык). Владелец: ML1.

Зачем, если есть LLM:
  * режим без ключа — жюри запустит даже без доступа к модели (агент работает, просто попроще);
  * fallback, когда LLM недоступна/ответила мусором;
  * быстрый эталон для eval: LLM-NLU должен быть НЕ хуже правил на тех же наборах (eval/nlu_*.jsonl);
  * guard от инъекций, который срабатывает ДО вызова LLM (см. is_injection).

Устройство: отдельный извлекатель на каждый слот (даты, длительность, гости, бюджет, транспорт,
пожелания), затем выбор намерения. Порядок намерений важен: «беру второй» — это выбор, а не покупка;
«есть там парковка?» — вопрос, а не новое требование; «а если бюджет 150?» — изменение, а не вопрос.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta

from ..schemas import Intent, LocationPack, NluResult, PatchOp, PostPurchaseKind, TripStage, TripState

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "мае": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
MONTH_RE = r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
NUM_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "пару": 2, "пара": 2,
    "три": 3, "трех": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "десять": 10, "десяти": 10,
    "одиннадцать": 11, "двенадцать": 12, "четырнадцать": 14, "пятнадцать": 15, "двадцать": 20,
    "тридцать": 30, "двое": 2, "трое": 3, "четверо": 4, "пятеро": 5, "шестеро": 6, "семеро": 7,
}  # fmt: skip
NUM_TOKEN = r"(\d{1,3}|" + "|".join(sorted(NUM_WORDS, key=len, reverse=True)) + r")"
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
GROUP_WORDS = {"вдвоем": 2, "втроем": 3, "вчетвером": 4, "впятером": 5, "вшестером": 6}
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "1": 1, "2": 2, "3": 3}

# Районы Сочи — запасной словарь, если RuleNlu создан без локаций (тесты). В работе словари городов и
# районов берутся из location.json (nlu_patterns): новый город не требует правок этого файла.
DISTRICTS = [
    (r"лазаревск", "lazarevskoe"),
    (r"дагомыс", "dagomys"),
    (r"центральн\w* (?:район|соч)|центр\w* (?:г\.? ?)?соч|в центре соч", "central"),
    (r"\bхост\w*", "khosta"),
    (r"адлер", "adler"),
    (r"сириус|имеретин|олимпийск\w* парк", "sirius"),
    (r"красн\w* полян|роз\w* хутор|эсто-?садок|горки город|газпром\w* лаур", "krasnaya_polyana"),
]

PREFS: list[tuple[str, str, str]] = [
    (r"перв\w* лини", "beach_first_line", "important"),
    (r"вид\w* на море", "sea_view", "important"),
    (
        r"\bмор[еяюи]\b|у моря|к морю|пляж|покупаться|купаться|ближе к морю|\bsea\b|\bbeach"
        r"|погреться|позагорать|где (?:сейчас )?тепл|теплее",
        "near_sea",
        "important",
    ),
    (r"лыж|сноуборд|горнолыж|склон|ski", "ski_access", "important"),
    (r"\bгор[ыауе]?\b|в горах|горн\w* воздух", "mountains", "important"),
    (r"\bтих|тиш|спокойн|уединен|без шума|от шума|не шумн|перезагруз|quiet", "quiet", "important"),
    (r"тусов|ночн\w* жизн|клуб(?!\w* детск)|вечеринк|бары|ресторан\w* и ночн", "nightlife", "important"),
    (r"в центре(?! соч)|центр города|рядом с центром", "city_center", "important"),
    (r"детск\w* клуб|аниматор|анимаци", "kids_club", "important"),
    (r"подогрев\w* бассейн|крыт\w* бассейн|тепл\w* бассейн", "heated_pool", "important"),
    (r"бассейн", "pool", "important"),
    (r"\bспа\b|\bspa\b|массаж|сауна|баня", "spa", "nice"),
    (r"романт|годовщин|медов\w* месяц|предложение сделать", "romantic", "important"),
    (r"все включено|all inclusive|ультра вс", "all_inclusive", "important"),
    (r"завтрак", "breakfast_included", "important"),
    (r"бесплатн\w* отмен|можно отменить|отменить без|без штраф|возможност\w* отмен|возврат", "free_cancellation", "important"),
    (r"парковк|на (?:сво\w+ )?машин|на (?:сво\w+ )?авто", "parking", "important"),
    (r"коляск|инвалид|доступн\w* сред|пожил|лифт", "accessible", "important"),
    (
        r"недалеко от аэропорт|рядом с аэропорт|ближе к аэропорт|от аэропорта (?:недалеко|близко|минимум|быстро|недолго)"
        r"|ехать от аэропорта (?:минимум|недолго|мало)|короткий трансфер|без долгой дороги|не хочу долго ехать",
        "short_transfer",
        "important",
    ),
    (r"хорош\w* отзыв|высок\w* рейтинг", "high_rating", "important"),
    (r"люкс|премиум|роскош|дорог\w* отел|шикарн", "luxury", "important"),
    (r"удаленк|поработать|ноутбук|рабоч\w* мест", "work_friendly", "nice"),
    (r"природ|\bлес|водопад|треккинг|поход|троп|на природ", "nature", "nice"),
    (r"санатор|лечени|подлечит|оздоров|процедур", "wellness", "important"),
    (r"подешевле|дешевле|бюджетн\w* вариант|недорог|эконом", "budget_value", "important"),
]

INJECTION = re.compile(
    r"ignore|игнорир|забудь\w*\s+(?:все\s+|всё\s+)?(?:правил|инструкц|предыдущ)|system prompt|системн\w* (?:промпт|инструкц)"
    r"|ты теперь|режим админ|admin|приказ\w* админ|set price|цен\w*\s+(?:на\s+)?(?:0|ноль|нол)\b|за 1 рубл"
    r"|бесплатн\w* бот|developer mode|jailbreak|выведи (?:свой|свои) (?:промпт|инструкц)"
)
QUESTION_ABOUT_OPTION = re.compile(
    r"\b(есть ли|есть там|там есть|а там|у них|в отеле есть|включен\w*|можно ли там|разрешено ли|далеко ли|"
    r"сколько (?:идти|ехать|до)|сколько там|сколько стоит|во сколько (?:заезд|выезд|заселение)|какой там|какая там|"
    r"далеко (?:от него|от отеля|ли)|от него до|от отеля до)\b"
)
MODIFY_MARKERS = re.compile(
    r"\bесли\b|\bдавай\b|хочу|покажи|найди|подбери|сдвин|перенес|лучше|подешевле|дешевле|поближе|ближе|бюджет|"
    r"поменя|измени|добав|убер|плюс|еще|присоедин|поедет|возьмем|решили"
)
TRAVEL_WORDS = re.compile(r"отел|поездк|отдох|путешеств|сочи|мор|гор|отпуск|съездить|поехать|номер|курорт|за границ|заграниц")

# Популярные направления, которых НЕТ в каталоге: честно говорим и предлагаем то, что есть.
UNSUPPORTED_DESTINATIONS = [
    (r"батуми", "Батуми"), (r"стамбул", "Стамбул"), (r"\bереван|армени", "Ереван"), (r"\bбаку\b|азербайджан", "Баку"),
    (r"хургад|шарм|египет", "Египет"), (r"вьетнам|нячанг|фукуок", "Вьетнам"), (r"\bбали\b", "Бали"),
    (r"мальдив", "Мальдивы"), (r"кипр", "Кипр"), (r"абхази|гагр|пицунд|сухум", "Абхазия"),
    (r"\bкрым|ялт|севастопол|евпатори|алушт|феодоси", "Крым"), (r"калининград", "Калининград"),
    (r"шри-? ?ланк", "Шри-Ланка"), (r"\bгоа\b|инди[яюи]\b", "Индия"), (r"анап[аеуы]|геленджик", "Анапа/Геленджик"),
    (r"\bв (?:питер|санкт-петербург)\b|\bв москву\b", "Москва/Петербург"),
    (r"сибир", "Сибирь"), (r"алта[йюяе]", "Алтай"), (r"байкал", "Байкал"), (r"карели", "Карелия"),
    (r"камчат", "Камчатка"), (r"мурманск|териберк", "Мурманск"), (r"\bдагестан", "Дагестан"),
    (r"\bв (?:европ|итали|испани|франци|германи|чехи|прагу|париж|рим\b|барселон)", "Европа"),
    (r"греци|черногори|сербии|\bсербию|белград", "Балканы/Греция"), (r"кита[йяю]|пекин|шанха|хайнан|санья", "Китай"),
    (r"япони|токио|\bкоре[юяи]|сеул", "Япония/Корея"), (r"\bкуб[ауе]\b|мексик|доминикан", "Карибы"),
    (r"абу-? ?даби|шардж|рас-? ?эль", "Абу-Даби/Шарджа"), (r"\bузбекистан|самарканд|ташкент", "Узбекистан"),
    (r"казахстан|алмат", "Казахстан"), (r"\bминск|беларус|белорус", "Беларусь"),
]  # fmt: skip
ORIGIN_SPAN = re.compile(r"\bиз\s+[\w\-]+")
# «не в Дубай», «только не Турция», «кроме Пхукета», «не хочу в Анталью» — отказ от направления
NEG_BEFORE = re.compile(
    r"(?:\bне|\bкроме|исключая|\bбез)\s+(?:(?:хочу|хотим|надо|нужно|ехать|лететь|поедем|едем|рассматриваем|предлагай)\s+)*"
    r"(?:(?:в|во|на)\s+)?(?:\w+\s+)?$"
)
NEG_AFTER = re.compile(
    r"^\w*[,\s]*(?:—\s*)?(?:не (?:хочу|хотим|надо|нужно|нужен|нужна|предлагай|рассматриваем|вариант)|отпада|исключ|был[иа]? уже)"
)
MORE_OPTIONS = re.compile(
    r"(?:друг\w*|ин\w*|нов\w*|ещ[её])\s+(?:\w+\s+)?(?:отел|вариант|гостиниц|предложени)|покажи\w* (?:ещ[её]|больше)"
    r"|не нравя?т?ся|что[- ]?(?:то|нибудь) друго|альтернатив|больше вариант"
)
INSPIRE_DISTRICT = re.compile(r"район|в как\w* част|где (?:лучше )?(?:жить|остановиться|поселиться)")
ABROAD = re.compile(r"за ?границ|заграниц|за рубеж|в другую страну|не в росси|не по росси|вне росси|\babroad\b|в другой стран")
DOMESTIC = re.compile(r"по росси|в россии\b|внутри страны|без загранпаспорт|без загран")


def _num(token: str | None) -> int | None:
    if not token:
        return None
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUM_WORDS.get(token)


def _month(word: str) -> int | None:
    w = word.lower()
    return next((m for stem, m in MONTHS.items() if w.startswith(stem)), None)


def _year_for(month: int, day: int, today: date) -> int:
    return today.year + (1 if (month, day) < (today.month, today.day) else 0)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _norm(text: str) -> str:
    t = text.lower().replace("ё", "е").replace("–", "-").replace("—", "-")
    return " " + re.sub(r"\s+", " ", t) + " "


def _negated(text: str, m: re.Match) -> bool:
    """Название стоит после «не/кроме/без» или перед «не хочу/отпадает» — пользователь от него отказывается."""
    return bool(NEG_BEFORE.search(text[max(0, m.start() - 40) : m.start()]) or NEG_AFTER.search(text[m.end() : m.end() + 30]))


def is_injection(text: str) -> bool:
    """Попытка управлять агентом через текст пользователя. Срабатывает и перед LLM (defense-in-depth)."""
    return bool(INJECTION.search(_norm(text)))


class RuleNlu:
    """locations — паки локаций (LocationPack): их nlu_patterns распознают город/страну и районы."""

    def __init__(self, locations: Sequence[LocationPack] | None = None):
        if locations:
            self.loc_rx = [(re.compile(p), lp.id) for lp in locations for p in lp.nlu_patterns]
            self.district_rx = [(re.compile(p), d.id, lp.id) for lp in locations for d in lp.districts for p in d.nlu_patterns]
        else:
            self.loc_rx = [(re.compile(r"\bсочи\b"), "sochi")]
            self.district_rx = [(re.compile(rx), did, "sochi") for rx, did in DISTRICTS]

    def parse(self, text: str, state: TripState, today: date) -> NluResult:
        t = _norm(text)
        req = state.requirements
        has_plan = state.turn_count > 0 and bool(state.variants or req.dates.month or req.dates.check_in)

        # ---- служебные намерения (порядок важен) ------------------------------------------
        if INJECTION.search(t):
            return NluResult(intent=Intent.off_topic, confidence=0.95, parser="rules")
        if re.search(r"начать заново|начнем сначала|новый поиск|сбрось|забудь вс", t):
            return NluResult(intent=Intent.reset, parser="rules")
        if state.stage == TripStage.ordered or state.order_ids:
            pp = self._post_purchase(t)
            if pp:
                return NluResult(intent=Intent.post_purchase, post_purchase_kind=pp, parser="rules")
        if state.variants:
            sel = re.search(
                r"\b(перв|втор|трет|четв|последн)\w*\b(?!\s+лини)|\b([123])(?:-?[йиео]\w*)?\s*(?:вариант|отел)"
                r"|номер\s+(один|два|три|1|2|3)\b",
                t,
            )
            if sel and re.search(r"бер|возьм|выбира|выбер|давай|нрав|подход|хочу|остановимся|вариант|отел", t):
                key = sel.group(1) or sel.group(2) or sel.group(3)
                n = len(state.variants) if key.startswith("последн") else ORDINALS.get(key[:4]) or ORDINALS.get(key) or _num(key)
                return NluResult(intent=Intent.select_variant, variant_ordinal=n, parser="rules")
        if re.search(r"сравн|чем (?:они )?отлича|в ч[её]м разниц|разниц\w* между|что лучше", t):
            return NluResult(intent=Intent.compare, parser="rules")
        if re.search(r"оформ|забронир|бронир|в корзину|оплат|\bберу\b|\bберем\b", t):
            return NluResult(intent=Intent.book, parser="rules")

        ops, assumed = self._extract(t, text, state, today)
        if MORE_OPTIONS.search(t):
            # «предложи другие отели» — новые варианты при тех же условиях (плюс то, что изменили в этой же фразе)
            return NluResult(
                intent=Intent.modify if has_plan else Intent.plan_trip,
                patch=ops,
                assumed_paths=assumed,
                more_options=True,
                parser="rules",
            )
        places = self._destinations(t)[0]
        many_places = len(places) > 1
        unknown = None if any(o.path == "location_id" for o in ops) else self._unknown_destination(t)

        if has_plan and state.variants and ("?" in t or QUESTION_ABOUT_OPTION.search(t)):
            about_option = QUESTION_ABOUT_OPTION.search(t)
            if about_option and not MODIFY_MARKERS.search(t):
                return NluResult(intent=Intent.ask_info, info_question=text.strip(), parser="rules")
            if not ops:
                return NluResult(intent=Intent.ask_info, info_question=text.strip(), parser="rules")
        if unknown:
            return NluResult(
                intent=Intent.modify if has_plan else Intent.plan_trip,
                patch=ops,
                assumed_paths=assumed,
                unknown_destination=unknown,
                parser="rules",
            )
        if many_places or re.search(
            r"не знаю куда|куда поехать|куда лучше|куда (?:можно )?(?:слетать|съездить)|посоветуй\w* (?:район|куда|страну|направлен)"
            r"|предложи\w* (?:район|куда|страну|направлен)|где лучше|где (?:сейчас )?(?:теплее|тепло)|куда (?:сейчас )?(?:теплее|тепло)",
            t,
        ):
            return NluResult(
                intent=Intent.inspire,
                patch=ops,
                assumed_paths=assumed,
                candidate_location_ids=sorted(places) if many_places else [],
                inspire_level="district" if INSPIRE_DISTRICT.search(t) else "location",
                parser="rules",
            )
        if ops:
            return NluResult(intent=Intent.modify if has_plan else Intent.plan_trip, patch=ops, assumed_paths=assumed, parser="rules")
        if re.search(r"привет|здравств|добр\w* (?:день|вечер|утро)|что ты (?:умеешь|можешь)|кто ты|спасибо|благодар", t):
            return NluResult(intent=Intent.smalltalk, parser="rules")
        if "?" in t or re.search(r"\b(есть ли|можно ли|сколько|какой|какая|какие|далеко ли|где)\b", t):
            return NluResult(intent=Intent.ask_info, info_question=text.strip(), parser="rules")
        if TRAVEL_WORDS.search(t):
            return NluResult(intent=Intent.plan_trip, parser="rules")
        return NluResult(intent=Intent.unclear, confidence=0.2, parser="rules")

    # ================================================================ слоты
    def _extract(self, t: str, raw: str, state: TripState, today: date) -> tuple[list[PatchOp], list[str]]:
        ops: list[PatchOp] = []
        assumed: list[str] = []
        self._dates(t, state, today, ops, assumed)
        self._guests(t, state, ops, assumed)
        self._budget(t, state, ops)
        self._transport(t, raw, ops, assumed)
        locs, districts, excluded = self._destinations(t)
        current = state.requirements.location_id
        if ABROAD.search(t):
            ops.append(PatchOp(op="set", path="geo_scope", value="abroad"))
        elif DOMESTIC.search(t):
            ops.append(PatchOp(op="set", path="geo_scope", value="domestic"))
        for lid in sorted(excluded):
            ops.append(PatchOp(op="add", path="excluded_location_ids", value=lid))
        if len(locs) == 1:
            (loc,) = locs
            ops.append(PatchOp(op="set", path="location_id", value=loc))
            districts = {d: lid for d, lid in districts.items() if lid == loc}
        elif len(locs) > 1:
            districts = {}  # «Сочи или Анталья?» — сравним города, районы не выбираем
        else:
            districts = {d: lid for d, lid in districts.items() if lid == current}
        if districts:
            ops.append(PatchOp(op="set", path="district_ids", value=sorted(districts)))
        self._prefs(t, ops)
        return ops, assumed

    def _destinations(self, t: str) -> tuple[set[str], dict[str, str], set[str]]:
        """Какие города/страны и районы названы — и от каких отказались («но не в Дубай»).

        «Из Москвы» — откуда, это не направление."""
        dest = ORIGIN_SPAN.sub(" ", t)
        excluded: set[str] = set()
        locs: set[str] = set()
        for rx, lid in self.loc_rx:
            for m in rx.finditer(dest):
                (excluded if _negated(dest, m) else locs).add(lid)
        districts: dict[str, str] = {}
        for rx, did, lid in self.district_rx:
            for m in rx.finditer(dest):
                if _negated(dest, m):
                    continue
                districts[did] = lid
        locs -= excluded
        districts = {d: lid for d, lid in districts.items() if lid not in excluded}
        owners = set(districts.values())
        if not locs and len(owners) == 1:
            locs = owners  # «в Патонг» → Пхукет; «старый город» есть у нескольких городов — это не выбор города
        return locs, districts, excluded

    @staticmethod
    def _unknown_destination(t: str) -> str | None:
        dest = ORIGIN_SPAN.sub(" ", t)
        for rx, name in UNSUPPORTED_DESTINATIONS:
            m = re.search(rx, dest)
            if m and not _negated(dest, m):  # «только не в Египет» — это отказ, а не запрос
                return name
        return None

    # ---------------------------------------------------------------- даты
    def _dates(self, t: str, state: TripState, today: date, ops: list[PatchOp], assumed: list[str]) -> None:
        d = state.requirements.dates
        exact = d.check_in is not None and d.check_out is not None

        # сдвиг («на 3 дня раньше», «на недельку позже», «на день позже»)
        sm = re.search(NUM_TOKEN + r"?\s*(дн\w*|ден\w*|сут\w*|недел\w*)\s+(позже|раньше|вперед|назад)", t)
        if sm and re.search(r"\bна\s", t[max(0, sm.start() - 4) : sm.start() + 1]):
            n = _num(sm.group(1)) or 1
            days = n * (7 if sm.group(2).startswith("недел") else 1) * (1 if sm.group(3) in ("позже", "вперед") else -1)
            ops.append(PatchOp(op="shift_days", path="dates", value=days))
            return

        # изменение длительности («на 2 ночи меньше», «на день дольше»)
        if exact and (dm := re.search(r"на " + NUM_TOKEN + r"?\s*(ноч\w*|дн\w*|ден\w*|сут\w*)\s+(меньше|короче|больше|дольше)", t)):
            n = _num(dm.group(1)) or 1
            sign = -1 if dm.group(3) in ("меньше", "короче") else 1
            ops.append(PatchOp(op="set", path="dates.check_out", value=(d.check_out + timedelta(days=sign * n)).isoformat()))
            return

        if exact and (pm := re.search(r"продл\w*\s+(?:на\s+)?" + NUM_TOKEN + r"?\s*(ноч\w*|дн\w*|ден\w*|сут\w*)", t)):
            n = _num(pm.group(1)) or 1
            ops.append(PatchOp(op="set", path="dates.check_out", value=(d.check_out + timedelta(days=n)).isoformat()))
            return

        ci = co = None
        # диапазон «10-17 ноября», «с 10 по 17 ноября»
        if m := re.search(r"(\d{1,2})\s*(?:-|по|до)\s*(\d{1,2})\s+" + MONTH_RE, t):
            mon = _month(m.group(3))
            if mon:
                y = _year_for(mon, int(m.group(1)), today)
                ci, co = _safe_date(y, mon, int(m.group(1))), _safe_date(y, mon, int(m.group(2)))
        elif m := re.search(r"(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?\s*(?:-|по|до)\s*(\d{1,2})\.(\d{1,2})", t):
            d1, m1, d2, m2 = map(int, m.groups())
            y = _year_for(m1, d1, today)
            ci, co = _safe_date(y, m1, d1), _safe_date(y if m2 >= m1 else y + 1, m2, d2)
        elif re.search(r"\b\d{1,2}\.\d{1,2}\b", t):
            # «заезд 3.12, выезд 7.12» — числовые даты с якорями
            for m in re.finditer(r"(заезд\w*|с|выезд\w*|до|по|обратно)?\s*\b(\d{1,2})\.(\d{1,2})\b(?!\.\d|:)", t):
                dd, mm_ = int(m.group(2)), int(m.group(3))
                dt = _safe_date(_year_for(mm_, dd, today), mm_, dd) if 1 <= mm_ <= 12 else None
                if not dt:
                    continue
                if (m.group(1) or "").startswith(("выезд", "до", "по", "обратно")):
                    co = dt
                elif ci is None:
                    ci = dt
                elif co is None:
                    co = dt
        else:
            # одиночная дата «14 ноября», «приедем 12 декабря»: до/обратно/улетаем → выезд, иначе заезд
            for m in re.finditer(r"(\d{1,2})(?:-?го)?\s+" + MONTH_RE, t):
                mon = _month(m.group(2))
                if not mon:
                    continue
                before = t[max(0, m.start() - 14) : m.start()]
                dt = _safe_date(_year_for(mon, int(m.group(1)), today), mon, int(m.group(1)))
                if re.search(r"\b(?:до|по|обратно|улета\w*|уезжа\w*|выезд\w*)\s*$", before):
                    co = dt
                elif ci is None:
                    ci = dt
            # день без месяца «обратно 9-го», «улетаем 25-го», «заехать 11-го», «заезд на 12-е»
            for m in re.finditer(
                r"(обратно|улета\w*|уезжа\w*|выезд\w*|до|заезд\w*|заехать|заселени\w*|заселиться|с)\s+"
                r"(?:на\s+)?(\d{1,2})-?(?:го|е|ое)\b",
                t,
            ):
                anchor = ci or d.check_in
                base_month = anchor.month if anchor else today.month
                base_year = anchor.year if anchor else today.year
                day = int(m.group(2))
                dt = _safe_date(base_year, base_month, day)
                if dt and anchor and dt <= anchor and m.group(1) not in ("заезд", "заехать", "с"):
                    nm = base_month % 12 + 1
                    dt = _safe_date(base_year + (base_month == 12), nm, day)
                if m.group(1).startswith(("обратно", "улета", "уезжа", "выезд", "до")):
                    co = dt
                else:
                    ci = dt
        if ci and co and co <= ci:
            co = None
        if ci:
            ops.append(PatchOp(op="set", path="dates.check_in", value=ci.isoformat()))
        if co:
            ops.append(PatchOp(op="set", path="dates.check_out", value=co.isoformat()))

        if not (ci or co):
            if re.search(r"на новогодн|на новый год|новогодн\w* праздник", t):
                y = today.year if (today.month, today.day) <= (12, 28) else today.year + 1
                ops += [
                    PatchOp(op="set", path="dates.check_in", value=date(y, 12, 30).isoformat()),
                    PatchOp(op="set", path="dates.check_out", value=date(y + 1, 1, 3).isoformat()),
                ]
                assumed += ["dates.check_in", "dates.check_out"]
            elif re.search(r"майск", t):
                y = today.year + (1 if (today.month, today.day) > (5, 1) else 0)
                ops += [
                    PatchOp(op="set", path="dates.check_in", value=date(y, 5, 1).isoformat()),
                    PatchOp(op="set", path="dates.check_out", value=date(y, 5, 5).isoformat()),
                ]
                assumed += ["dates.check_in", "dates.check_out"]
            elif re.search(r"на (?:эти |ближайшие )?выходные|в (?:эти |ближайшие )?выходные", t):
                fri = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
                ops += [
                    PatchOp(op="set", path="dates.check_in", value=fri.isoformat()),
                    PatchOp(op="set", path="dates.check_out", value=(fri + timedelta(days=2)).isoformat()),
                ]
            elif m := re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b", t):
                ops.append(PatchOp(op="set", path="dates.month", value=EN_MONTHS.index(m.group(1)) + 1))
            elif m := re.search(r"\b(?:в|на|начале|середине|конце)\s+" + MONTH_RE, t) or re.search(r"\b" + MONTH_RE + r"\b", t):
                mon = _month(m.group(1))
                if mon:
                    ops.append(PatchOp(op="set", path="dates.month", value=mon))

        # длительность (если нет обеих дат)
        if ci and co:
            return
        n = self._nights(t)
        if n:
            ops.append(PatchOp(op="set", path="dates.nights", value=n))

    @staticmethod
    def _nights(t: str) -> int | None:
        if m := re.search(NUM_TOKEN + r"\s*(?:ноч\w*|сут\w*)", t):
            return _num(m.group(1))
        if m := re.search(r"(?:дн\w*|ден\w*|ноч\w*)\s+(?:на\s+)?" + NUM_TOKEN + r"\b(?!\s*(?:лет|год|звезд|взросл))", t):
            return _num(m.group(1))
        if m := re.search(r"(\d{1,2})\s*nights?", t):
            return int(m.group(1))
        if m := re.search(NUM_TOKEN + r"\s+(?:дн\w*|ден\w*)", t):
            n = _num(m.group(1))
            if n and n <= 60 and not re.search(r"\b(?:за|через)\s*$", t[max(0, m.start() - 7) : m.start()]):
                return n
        if re.search(r"полтор\w* недел", t):
            return 10
        if m := re.search(r"\b(две|2|пару|три|3)\s+недел|недел\w*\s+на\s+(две|три)", t):
            return {"две": 14, "2": 14, "пару": 14, "три": 21, "3": 21}[m.group(1) or m.group(2)]
        if re.search(r"\bнедел(?:ю|ька|ьку|я)\b|на недел", t) and not re.search(r"через недел|на этой недел|на следующ\w* недел", t):
            return 7
        if re.search(r"\b(?:два|2|пару)\s+месяц|на пару месяц", t):
            return 60  # дольше 60 ночей не бронируем — это уже аренда
        if re.search(r"полтор\w* месяц", t):
            return 45
        if re.search(r"\bмесяц\b|на месяц", t) and not re.search(r"медов\w* месяц|в этом месяце|в следующем месяце", t):
            return 30
        return None

    # ---------------------------------------------------------------- гости
    def _guests(self, t: str, state: TripState, ops: list[PatchOp], assumed: list[str]) -> None:
        cur = state.requirements.guests
        adults: int | None = None
        relative = 0
        if m := re.search(NUM_TOKEN + r"\s+взросл", t):
            if not re.search(r"(?:еще|плюс)\s*$", t[max(0, m.start() - 6) : m.start()]):
                adults = _num(m.group(1))
        plus = re.search(r"\((\d)\s*\+\s*(\d)\)|\b(\d)\s*\+\s*(\d)\b(?!\s*(?:ноч|дн))", t)  # «нас 4 (2+2)»
        if adults is None and plus:
            adults = int(plus.group(1) or plus.group(3))
        if adults is None and (m := re.search(NUM_TOKEN + r"\s+(?:пенсионер\w*|человек\w*|чел\b|туриста?|adults?)", t)):
            adults = _num(m.group(1))
        if adults is None and (m := re.search(r"\bнас\s+" + NUM_TOKEN + r"\b", t)):
            adults = _num(m.group(1))
        if adults is None:
            adults = next((n for w, n in GROUP_WORDS.items() if w in t), None)
        if adults is None and re.search(
            r"с жен|с муж|с девушк|с парн|с подруг|с другом|\bпар[аеуы]\b(?!\s+(?:недел|дн|ноч|сут|час))|парой|молодожен"
            r"|медов\w* месяц|годовщин|мы с (?:женой|мужем)",
            t,
        ):
            adults = 2
        if adults is None and (m := re.search(r"\b(двое|трое|четверо|пятеро|шестеро)\b(?!\s+(?:дет|ребят|внук))", t)):
            adults = NUM_WORDS[m.group(1)]
        single_parent = re.search(r"\bя (?:и|с) (?:сын|доч|ребен|детьми|малыш)", t)
        if adults is None and (single_parent or re.search(r"\b(?:один|одна|сам|сама|одному|одной)\b(?!\s+взросл)", t)):
            if not re.search(r"еще (?:один|одна)", t):
                adults = 1
        # относительное: «ещё один взрослый», «с нами поедет ещё мама», «присоединится ещё двое»
        if rm := re.search(
            r"(?:еще|плюс|присоедин\w*)\s+"
            + NUM_TOKEN
            + r"?\s*(взросл\w*|человек\w*|мама|папа|бабушк\w*|дедушк\w*|друг\w*|подруг\w*|коллег\w*)",
            t,
        ):  # noqa: E501
            relative = _num(rm.group(1)) or 1
        elif re.search(r"(?:поедет|едет|присоедин\w*)\s+(?:с нами\s+)?еще\s+(?:мама|папа|бабушк|дедушк|друг|подруг|один|одна)", t):
            relative = 1
        if relative and adults is None:
            adults = (cur.adults or 2) + relative
        if adults:
            ops.append(PatchOp(op="set", path="guests.adults", value=adults))
        if m := re.search(NUM_TOKEN + r"\s+номер(?:а|ов)?\b", t):
            if (n := _num(m.group(1))) and 1 < n <= 5:
                ops.append(PatchOp(op="set", path="guests.rooms", value=n))

        kids: list[int] = []
        for m in re.finditer(r"(?:ребенк\w*|ребен\w*|сын\w*|доч\w*|малыш\w*|внук\w*|внучк\w*)\s*(\d{1,2})\s*(?:лет|год)", t):
            kids.append(int(m.group(1)))
        if not kids and (m := re.search(r"(?:дет\w*|ребят\w*)\s*\(?\s*((?:\d{1,2}\s*(?:,|и)?\s*){1,5})\s*(?:лет|год)", t)):
            kids = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
        if not kids and plus:
            kids = [7] * int(plus.group(2) or plus.group(4))
            assumed.append("guests.children_ages")
        if not kids:
            m = re.search(r"\b(\w+)\s+(?:дет|ребен|ребят)", t)
            n = _num(m.group(1)) if m else None
            if m and n is None and m.group(1) in ("двумя", "тремя"):
                n = {"двумя": 2, "тремя": 3}[m.group(1)]
            if n and n < 6:
                kids = [7] * n
                assumed.append("guests.children_ages")
            elif re.search(r"младен|грудн|малыш", t):
                kids = [1]
                assumed.append("guests.children_ages")
            elif re.search(r"с ребен|с сыном|с дочк|с дочер|с детьми|с ребятами|с внук|с внучк|\bребенок\b|семь[яеи] с", t):
                kids = [7, 7] if re.search(r"с детьми|с ребятами|с внуками", t) else [7]
                assumed.append("guests.children_ages")
        if kids:
            if cur.children_ages and re.search(r"\b(?:еще|плюс)\b", t):
                kids = [*cur.children_ages, *kids]
            ops.append(PatchOp(op="set", path="guests.children_ages", value=kids))
            if adults is None and cur.adults is None:
                ops.append(PatchOp(op="set", path="guests.adults", value=1 if single_parent else 2))
                assumed.append("guests.adults")

        if re.search(r"без (?:собак|кош|кот|животн|питомц)|(?:собак|кошк|кот|питомц)\w*\s+(?:оставим|не берем|не поедет|не едет)", t):
            ops.append(PatchOp(op="set", path="guests.pets", value=[]))
        else:
            if re.search(r"собак|\bпес\b|\bпсом|щен|лабрадор|овчарк|шпиц|такс", t):
                ops.append(PatchOp(op="add", path="guests.pets", value="dog"))
            if re.search(r"\bкот\b|котом|кошк", t):
                ops.append(PatchOp(op="add", path="guests.pets", value="cat"))

    # ---------------------------------------------------------------- бюджет
    def _budget(self, t: str, state: TripState, ops: list[PatchOp]) -> None:
        amount: int | None = None
        span = None
        thousands = r"(?:тыс\w*|тыщ\w*|т\.?\s?р\.?|к|k)"
        not_units = r"(?!\s*(?:ноч|дн|ден|сут|недел|лет|год|звезд|\*|взросл|человек|мин|час|[:.]\d))"
        if m := re.search(r"(\d+(?:[.,]\d)?)\s*" + thousands + r"(?![а-я])", t):
            amount, span = int(float(m.group(1).replace(",", ".")) * 1000), m
        elif m := re.search(r"тысяч\w*\s+(\d{1,4})\b", t):
            amount, span = int(m.group(1)) * 1000, m
        elif m := re.search(r"(\d{1,3}(?:[  ]\d{3})+|\d{4,7})\s*(?:руб\w*|₽|р\b)?", t):
            ctx = t[max(0, m.start() - 25) : m.end() + 15]
            if re.search(
                r"бюджет|до|не дороже|максимум|в пределах|около|примерно|уложиться|за все|на все|на всех|руб|₽|в сутки|за ночь", ctx
            ):  # noqa: E501
                amount, span = int(re.sub(r"\D", "", m.group(1))), m
        elif m := re.search(r"(?:бюджет\w*|до|не дороже|максимум|в пределах|уложиться в)\s*(?:до\s+)?(\d{2,3})\b" + not_units, t):
            if re.search(r"бюджет|не дороже|максимум|уложиться|в пределах", t):
                amount, span = int(m.group(1)) * 1000, m  # «бюджет 150», «до 130?» — в тысячах
        if amount and 1000 <= amount <= 10_000_000:
            ops.append(PatchOp(op="set", path="budget.amount", value=amount))
            tail = t[span.start() : span.end() + 20] if span else ""
            if re.search(r"за ночь|в сутки|за сутки|/ночь|в ночь", tail):
                ops.append(PatchOp(op="set", path="budget.scope", value="per_night"))
            elif re.search(r"на отель|за отель|за проживание|на проживание", t):
                ops.append(PatchOp(op="set", path="budget.scope", value="hotel_total"))
            if re.search(r"около|примерно|плюс-минус|в районе", t):
                ops.append(PatchOp(op="set", path="budget.flex_pct", value=10))
            return
        if state.requirements.budget.amount:
            if re.search(r"бюджет меньше|подешевле|дешевле|экономнее|сократ\w* бюджет", t):
                ops.append(PatchOp(op="scale", path="budget.amount", value=0.85))
            elif re.search(r"можно дороже|бюджет больше|подороже|увелич\w* бюджет", t):
                ops.append(PatchOp(op="scale", path="budget.amount", value=1.25))

    # ---------------------------------------------------------------- транспорт
    def _transport(self, t: str, raw: str, ops: list[PatchOp], assumed: list[str]) -> None:
        if re.search(r"без пересад|прям\w* рейс|прямым|только прям|direct", t):
            ops.append(PatchOp(op="set", path="transport.max_stops", value=0))
        elif re.search(r"(?:можно|пусть|ладно)\s+(?:и\s+)?с пересадк|пересадк\w* (?:не страшн|допустим|можно)", t):
            ops.append(PatchOp(op="unset", path="transport.max_stops"))
        if re.search(r"прилет|прилеч|самолет|рейс|перелет|вылет|приземл|лет(?:им|им из|аем)", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="plane"))
        elif re.search(r"поезд|ласточк|\bжд\b|ж/д|вокзал|электричк", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="train"))
        elif re.search(r"на (?:сво\w+ )?(?:машин|авто)|своим ходом", t):
            ops.append(PatchOp(op="set", path="transport.arrival_mode", value="car"))

        def find_time(anchor: str) -> tuple[str | None, bool]:
            a = re.search(anchor, t)
            if not a:
                return None, False
            window = t[a.end() : a.end() + 60]
            if m := re.search(r"\bв\s*(\d{1,2})(?:[:.](\d{2}))?\s*(утра|утром|вечера|вечером|ночи|ночью|дня|час\w*)?", window):
                h, mi, part = int(m.group(1)), int(m.group(2) or 0), m.group(3) or ""
                if part.startswith("вечер") and h < 12 or part == "дня" and h < 12:
                    h += 12
                if part.startswith("ноч") and h == 12:
                    h = 0
                if h <= 23 and mi <= 59 and (m.group(2) or part):
                    return f"{h:02d}:{mi:02d}", False
            if m := re.search(r"\b(рано утром|утром|днем|вечером|поздно вечером|ночью)\b", window):
                return {
                    "рано утром": "06:00",
                    "утром": "08:00",
                    "днем": "13:00",
                    "вечером": "19:00",
                    "поздно вечером": "22:00",
                    "ночью": "01:00",
                }[m.group(1)], True
            return None, False

        at, guess = find_time(r"прилет\w*|прилеч\w*|приезж\w*|приед\w*|прибыва\w*|прибыт\w*|садится|приземл\w*|посадк\w*")
        if at:
            ops.append(PatchOp(op="set", path="transport.arrival_time", value=at))
            if guess:
                assumed.append("transport.arrival_time")
        dt, _ = find_time(r"улета\w*|улетим|обратн\w*|уезжа\w*|уедем|отъезд\w*")
        if dt:
            ops.append(PatchOp(op="set", path="transport.departure_time", value=dt))

        if m := re.search(r"\bиз\s+([А-ЯЁ][а-яё\-]+(?:[ -][А-ЯЁ][а-яё]+)?)", raw):
            ops.append(PatchOp(op="set", path="transport.origin_city", value=_city(m.group(1))))

    # ---------------------------------------------------------------- пожелания
    @staticmethod
    def _prefs(t: str, ops: list[PatchOp]) -> None:
        seen: set[str] = set()
        neg_after = r"\w*\s+(?:не нужен|не нужна|не нужно|не нужны|не обязател\w*|необязател\w*|не важ\w*|не принципиал\w*)"
        for rx, key, prio in PREFS:
            if key in seen or not re.search(rx, t):
                continue
            if key == "pool" and "heated_pool" in seen:
                continue
            if key == "near_sea" and ("beach_first_line" in seen or re.search(r"без моря|не на море", t)):
                continue
            if key == "budget_value" and any(o.path == "budget.amount" for o in ops):
                continue
            if key == "parking" and re.search(r"без машин", t):
                continue
            if (
                re.search(r"(?:не нужн\w*|без|обойдемся без|можно без)\s+(?:" + rx + ")", t)
                or re.search(r"(?:убер\w*|удали\w*|не учитыва\w*|забудь\w*)[^.!?]{0,25}(?:" + rx + ")", t)
                or re.search(r"(?:" + rx + ")" + neg_after, t)
            ):
                ops.append(PatchOp(op="remove", path="preferences", value=key))
                seen.add(key)
                continue
            must = re.search(r"(?:обязательно|только|точно|непременно|строго|именно)[^.,!?]{0,25}(?:" + rx + ")", t) or re.search(
                r"(?:" + rx + r")[^.!?]{0,30}(?:обязательно|обязателен|обязательна|непременно)", t
            )
            ops.append(PatchOp(op="add", path="preferences", value={"key": key, "priority": "must" if must else prio}))
            seen.add(key)
        if m := re.search(r"(\d)\s*(?:звезд|\*|★)|(пяти|четырех|трех)звезд", t):
            stars = int(m.group(1)) if m.group(1) else {"пяти": 5, "четырех": 4, "трех": 3}[m.group(2)]
            ops.append(PatchOp(op="add", path="preferences", value={"key": "min_stars", "priority": "must", "value": stars}))

    @staticmethod
    def _post_purchase(t: str) -> PostPurchaseKind | None:
        if re.search(r"ранн\w* заезд|заселиться раньше", t):
            return PostPurchaseKind.early_check_in
        if re.search(r"поздн\w* выезд", t):
            return PostPurchaseKind.late_check_out
        if re.search(r"трансфер", t):
            return PostPurchaseKind.add_transfer
        if re.search(r"напомн|напомин", t):
            return PostPurchaseKind.reminder
        if re.search(r"отмени\w* брон|отменить заказ|отмените", t):
            return PostPurchaseKind.cancel
        if re.search(r"перенес|сдвин|другие даты|позже|раньше", t):
            return PostPurchaseKind.change_dates
        return None


CITY_FORMS = {
    "москвы": "Москва", "питера": "Санкт-Петербург", "петербурга": "Санкт-Петербург", "сочи": "Сочи", "тбилиси": "Тбилиси",
    "дубая": "Дубай", "антальи": "Анталья", "анталии": "Анталья", "баку": "Баку", "еревана": "Ереван", "стамбула": "Стамбул",
    "санкт-петербурга": "Санкт-Петербург", "спб": "Санкт-Петербург",
    "екатеринбурга": "Екатеринбург", "казани": "Казань", "новосибирска": "Новосибирск", "краснодара": "Краснодар",
    "ростова": "Ростов-на-Дону", "ростова-на-дону": "Ростов-на-Дону", "самары": "Самара", "перми": "Пермь",
    "воронежа": "Воронеж", "уфы": "Уфа", "нижнего новгорода": "Нижний Новгород", "минска": "Минск",
}  # fmt: skip


def _city(word: str) -> str:
    w = word.lower()
    if w in CITY_FORMS:
        return CITY_FORMS[w]
    for suffix, repl in (("ы", "а"), ("и", "ь"), ("а", "")):
        if w.endswith(suffix) and len(w) > 4:
            return (w[: -len(suffix)] + repl).capitalize()
    return word
