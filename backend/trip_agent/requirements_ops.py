"""Применение PatchOp к Requirements + человекочитаемое описание изменений.

Общее ядро (используют агент и ранжирование для «ослаблений»). Владелец: ML1.
Все операции чистые: на вход старые требования, на выход новые + список изменений.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .schemas import DateWindow, PatchOp, Preference, PrefKey, Priority, Requirements, Source

PATH_LABELS = {
    "location_id": "Направление",
    "district_ids": "Район",
    "dates": "Даты",
    "dates.check_in": "Заезд",
    "dates.check_out": "Выезд",
    "dates.month": "Месяц",
    "dates.year": "Год",
    "dates.nights": "Ночей",
    "dates.flexibility_days": "Гибкость дат",
    "guests.adults": "Взрослых",
    "guests.children_ages": "Дети",
    "guests.pets": "Животные",
    "guests.rooms": "Номеров",
    "budget.amount": "Бюджет",
    "budget.scope": "Бюджет на",
    "budget.flex_pct": "Запас бюджета",
    "preferences": "Пожелания",
    "transport.origin_city": "Откуда",
    "transport.arrival_mode": "Как добираетесь",
    "transport.arrival_hub_id": "Прибытие в",
    "transport.arrival_time": "Время прибытия",
    "transport.departure_time": "Время отъезда",
    "transport.max_stops": "Пересадки",
    "notes": "Заметки",
}


class PatchError(ValueError):
    pass


LOCATION_LABELS: dict[str, str] = {}
"""id локации → название для списка изменений («Направление: Сочи → Дубай»). Заполняет LocationRegistry."""
DISTRICT_LABELS: dict[str, str] = {}
"""id района → название («Район: — → Патонг», а не patong). Заполняет LocationRegistry."""


def _get(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _set(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        data = data[p]
    data[parts[-1]] = value


def apply_patch(
    req: Requirements, ops: list[PatchOp], *, source: Source = Source.user, today: date | None = None
) -> tuple[Requirements, list[str]]:
    """Вернуть (новые требования, список изменений для UI). Невалидная операция → PatchError."""
    data = req.model_dump(mode="python")
    changes: list[str] = []
    provenance = dict(req.provenance)

    for op in ops:
        path = op.path
        label = PATH_LABELS.get(path, path)
        if op.op == "shift_days":
            if path != "dates":
                raise PatchError("shift_days применим только к 'dates'")
            days = int(op.value)
            d = data["dates"]
            for k in ("check_in", "check_out"):
                if isinstance(d[k], str):
                    d[k] = date.fromisoformat(d[k])
            if d["check_in"] and d["check_out"]:
                d["check_in"] = d["check_in"] + timedelta(days=days)
                d["check_out"] = d["check_out"] + timedelta(days=days)
                changes.append(f"Даты сдвинуты на {days:+d} дн.")
            else:
                raise PatchError("нет точных дат для сдвига")
            provenance["dates.check_in"] = provenance["dates.check_out"] = source
            continue

        if path == "preferences":
            prefs: list[dict] = data["preferences"]
            if op.op in ("add", "set"):
                p = op.value if isinstance(op.value, dict) else {"key": op.value}
                new = Preference.model_validate({**p, "source": p.get("source", source)})
                prefs[:] = [x for x in prefs if x["key"] != new.key]
                prefs.append(new.model_dump(mode="python"))
                changes.append(f"+ {PREF_LABELS.get(new.key, new.key)}" + (" (обязательно)" if new.priority == Priority.must else ""))
            elif op.op == "remove":
                key = op.value["key"] if isinstance(op.value, dict) else op.value
                before = len(prefs)
                prefs[:] = [x for x in prefs if x["key"] != key]
                if len(prefs) != before:
                    changes.append(f"− {PREF_LABELS.get(PrefKey(key), key)}")
            else:
                raise PatchError(f"op {op.op} не поддерживается для preferences")
            continue

        if path == "location_id":
            if op.op != "set" or not isinstance(op.value, str) or not op.value:
                raise PatchError("направление можно только задать")
            if op.value != data["location_id"]:
                changes.append(f"{label}: {_fmt(data['location_id'], path)} → {_fmt(op.value, path)}")
                # районы и хаб прибытия принадлежат прежнему городу; если их задали этим же патчем — оставим
                if not any(o.path == "district_ids" for o in ops) and data["district_ids"]:
                    data["district_ids"] = []
                    changes.append("Район: любой")
                    provenance.pop("district_ids", None)
                if not any(o.path == "transport.arrival_hub_id" for o in ops):
                    data["transport"]["arrival_hub_id"] = None
            data["location_id"] = op.value
            provenance[path] = source
            continue

        current = _get(req, path)
        if op.op == "set":
            _set(data, path, op.value)
            if _fmt(current, path) != _fmt(op.value, path):  # «plane → plane» в списке изменений не показываем
                changes.append(f"{label}: {_fmt(current, path)} → {_fmt(op.value, path)}")
        elif op.op == "unset":
            default = [] if isinstance(current, list) else None
            _set(data, path, default)
            if current not in (None, []):
                changes.append(f"{label}: убрано" if path != "transport.max_stops" else "Пересадки: можно")
        elif op.op in ("add", "remove"):
            if not isinstance(current, list):
                raise PatchError(f"{path} не список")
            lst = list(_get_from_dict(data, path))
            if op.op == "add":
                lst.append(op.value)
                changes.append(f"{label}: + {_fmt(op.value)}")
            elif op.value in lst:
                lst.remove(op.value)
                changes.append(f"{label}: − {_fmt(op.value)}")
            _set(data, path, lst)
        elif op.op == "scale":
            if current is None:
                raise PatchError(f"{path} не задан — нечего масштабировать")
            new_val = int(round(current * float(op.value), -2))
            _set(data, path, new_val)
            changes.append(f"{label}: {_fmt(current, path)} → {_fmt(new_val, path)}")
        else:
            raise PatchError(f"неизвестная операция {op.op}")
        provenance[path] = source

    data["provenance"] = provenance
    # сначала валидируем (строки "2026-11-07" → date), потом досчитываем производные поля дат
    try:
        dates = data.pop("dates")
        new_req = Requirements.model_validate({**data, "dates": {**dates, "check_out": None}})
        d = new_req.dates
        co = DateWindow.model_validate({**dates, "check_in": None}).check_out if dates.get("check_out") else None
        if d.check_in and co is None and d.nights:
            co = d.check_in + timedelta(days=d.nights)
        if d.check_in and co:
            d.check_out = co
            d.nights = (co - d.check_in).days
            if d.nights <= 0:
                raise PatchError("дата выезда должна быть позже заезда")
            d.month, d.year = d.check_in.month, d.check_in.year
        elif co and not d.check_in:
            d.check_out = co
    except PatchError:
        raise
    except Exception as e:  # pydantic ValidationError → понятная ошибка
        raise PatchError(str(e)) from e
    return new_req, changes


def _get_from_dict(data: dict, dotted: str) -> Any:
    for p in dotted.split("."):
        data = data[p]
    return data


MONEY_PATHS = {"budget.amount"}


def fmt_rub(v: int) -> str:
    return f"{v:,}".replace(",", "\u00a0") + "\u00a0₽"


def _fmt(v: Any, path: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if path in MONEY_PATHS and isinstance(v, int | float):
        return fmt_rub(int(v))
    if path == "location_id":
        return LOCATION_LABELS.get(str(v), str(v))
    if path == "district_ids" and isinstance(v, list):
        return ", ".join(DISTRICT_LABELS.get(str(x), str(x)) for x in v) if v else "—"
    if path == "transport.max_stops":
        return "только прямые" if v == 0 else f"не больше {v}"
    if path == "transport.arrival_mode":
        return {"plane": "самолёт", "train": "поезд", "car": "машина", "unknown": "не указано"}.get(str(getattr(v, "value", v)), str(v))
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, list):
        return ", ".join(_fmt(x) for x in v) if v else "—"
    return str(getattr(v, "value", v))


PREF_LABELS: dict[PrefKey, str] = {
    PrefKey.near_sea: "Близко к морю",
    PrefKey.beach_first_line: "Первая линия",
    PrefKey.sea_view: "Вид на море",
    PrefKey.mountains: "Горы",
    PrefKey.ski_access: "Рядом с подъёмниками",
    PrefKey.quiet: "Тихо",
    PrefKey.nightlife: "Ночная жизнь",
    PrefKey.city_center: "Центр",
    PrefKey.family_friendly: "Удобно с детьми",
    PrefKey.kids_club: "Детский клуб",
    PrefKey.pool: "Бассейн",
    PrefKey.heated_pool: "Подогреваемый бассейн",
    PrefKey.spa: "СПА",
    PrefKey.romantic: "Романтика",
    PrefKey.breakfast_included: "Завтрак включён",
    PrefKey.all_inclusive: "Всё включено",
    PrefKey.free_cancellation: "Бесплатная отмена",
    PrefKey.pet_friendly: "Можно с животными",
    PrefKey.parking: "Парковка",
    PrefKey.accessible: "Доступная среда",
    PrefKey.short_transfer: "Короткий трансфер",
    PrefKey.high_rating: "Высокий рейтинг",
    PrefKey.min_stars: "Звёздность",
    PrefKey.budget_value: "Подешевле",
    PrefKey.luxury: "Премиум",
    PrefKey.work_friendly: "Можно работать",
    PrefKey.nature: "Природа",
    PrefKey.wellness: "Оздоровление",
}
