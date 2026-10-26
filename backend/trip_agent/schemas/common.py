"""Базовые типы. Деньги — целые рубли (int): без float-ошибок и без лишней сложности Money-объекта."""

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    """Базовая модель проекта: запрещаем неизвестные поля, чтобы опечатки ловились сразу."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class GeoPoint(Model):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class BBox(Model):
    south: float
    west: float
    north: float
    east: float


Rub = int  # алиас для читаемости сигнатур: сумма в рублях
