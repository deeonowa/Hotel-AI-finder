"""UI-блоки — ответ агента. Агент НЕ возвращает «просто текст»: он возвращает типизированные блоки,
а любой клиент (веб, Telegram, виджет Островка) их рисует. Это и делает агента переиспользуемым.

Новый тип блока = PR, который видят все трое (меняется и бэк, и фронт).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .actions import QuickReply
from .booking import Cart, Order
from .catalog import ClimateMonth
from .common import Model
from .trip import Relaxation, SearchStats, Variant
from .vocab import DistrictKind, Source


class TextBlock(Model):
    type: Literal["text"] = "text"
    markdown: str


class AssumptionChip(Model):
    path: str = Field(description="путь в Requirements, напр. 'dates.nights'")
    label: str = Field(description="«Ночей»")
    value_label: str = Field(description="«7»")
    source: Source
    options: list[QuickReply] = Field(default_factory=list, description="быстрые альтернативы")


class AssumptionsBlock(Model):
    type: Literal["assumptions"] = "assumptions"
    title: str = "Я понял так"
    items: list[AssumptionChip]


class QuestionBlock(Model):
    type: Literal["question"] = "question"
    text: str
    paths: list[str] = Field(default_factory=list, description="какие поля закрывает вопрос")
    quick_replies: list[QuickReply] = Field(default_factory=list)
    allow_free_text: bool = True


class DestinationCard(Model):
    district_id: str
    name: str
    kind: DistrictKind
    why: list[str] = Field(default_factory=list)
    price_from_per_night: int | None = None
    hotels_count: int = 0
    transfer_minutes_from_airport: int | None = None
    climate: ClimateMonth | None = None
    choose: QuickReply


class DestinationsBlock(Model):
    type: Literal["destinations"] = "destinations"
    title: str = "Куда можно поехать"
    items: list[DestinationCard]


class VariantsBlock(Model):
    type: Literal["variants"] = "variants"
    items: list[Variant]
    recommended_id: str | None = None
    stats: SearchStats | None = None


class ComparisonRow(Model):
    label: str
    cells: list[str]
    best_index: list[int] = Field(default_factory=list, description="какие колонки лучшие по строке")


class ComparisonBlock(Model):
    type: Literal["comparison"] = "comparison"
    variant_ids: list[str]
    headers: list[str]
    rows: list[ComparisonRow]


class DiffItem(Model):
    label: str
    before: str | None = None
    after: str | None = None
    impact: Literal["better", "worse", "neutral"] = "neutral"


class DiffBlock(Model):
    type: Literal["diff"] = "diff"
    summary: str
    requirement_changes: list[str] = Field(default_factory=list, description="что поменялось в условиях")
    items: list[DiffItem] = Field(default_factory=list, description="что поменялось в плане")
    kept: list[str] = Field(default_factory=list, description="что удалось сохранить")


class NoResultsBlock(Model):
    type: Literal["no_results"] = "no_results"
    reason: str
    rejected: dict[str, int] = Field(default_factory=dict)
    relaxations: list[Relaxation] = Field(default_factory=list)


class CartBlock(Model):
    type: Literal["cart"] = "cart"
    cart: Cart


class OrderBlock(Model):
    type: Literal["order"] = "order"
    order: Order


UIBlock = Annotated[
    TextBlock
    | AssumptionsBlock
    | QuestionBlock
    | DestinationsBlock
    | VariantsBlock
    | ComparisonBlock
    | DiffBlock
    | NoResultsBlock
    | CartBlock
    | OrderBlock,
    Field(discriminator="type"),
]
