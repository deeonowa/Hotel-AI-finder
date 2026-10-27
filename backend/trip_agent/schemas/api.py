"""Контракт HTTP API агента. Из него генерируется openapi.json → TypeScript-типы фронта."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .actions import Action, QuickReply
from .common import Model
from .requirements import Requirements
from .ui import UIBlock
from .vocab import TripStage


class TurnRequest(Model):
    """Ровно одно из: text (свободный ввод) или action (клик в UI)."""

    text: str | None = Field(default=None, max_length=4000)
    action: Action | None = None
    expected_version: int | None = Field(default=None, description="оптимистичная блокировка: версия состояния, которую видел клиент")

    @model_validator(mode="after")
    def _one_of(self) -> TurnRequest:
        if (self.text is None) == (self.action is None):
            raise ValueError("exactly one of `text` or `action` must be set")
        return self


class TraceStep(Model):
    """Шаг работы агента — показываем в UI («что я сделал») и используем для метрик."""

    name: str
    detail: str = ""
    ms: int = 0


class LocationSummary(Model):
    """Направление в каталоге — для фронта (переключатель города) и проверки сборки."""

    id: str
    name: str
    country: str
    abroad: bool
    hotels: int
    tagline: str = ""


class TurnResponse(Model):
    session_id: str
    version: int
    stage: TripStage
    blocks: list[UIBlock]
    suggestions: list[QuickReply] = Field(default_factory=list)
    requirements: Requirements
    trace: list[TraceStep] = Field(default_factory=list)
    mode: Literal["llm", "rules"] = "rules"
    location: LocationSummary | None = Field(default=None, description="куда сейчас подбираем (для путевого листа)")


class CreateSessionRequest(Model):
    user_id: str | None = Field(default=None, description="для памяти между сессиями (опционально)")


class HealthResponse(Model):
    status: Literal["ok"] = "ok"
    llm_configured: bool
    llm_model: str | None = None
    location_id: str = Field(description="направление по умолчанию")
    hotels_loaded: int = Field(description="отелей во всех направлениях")
    data_source: str
    locations: list[LocationSummary] = Field(default_factory=list)
