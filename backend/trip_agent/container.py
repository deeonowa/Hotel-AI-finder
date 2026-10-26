"""Composition root: ЕДИНСТВЕННОЕ место, где выбираются реализации портов.

Хотите заменить демо-цены на реальный API поставщика, правила NLU на LLM, Postgres на другое хранилище —
меняете одну строку здесь. Агент, API и фронт не трогаются. Это и есть «строительный блок».
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .agent.nlg import LlmNlg, TemplateNlg
from .agent.nlu_llm import LlmNlu
from .agent.nlu_rules import RuleNlu
from .agent.orchestrator import Agent
from .booking.service import BookingService
from .catalog.json_catalog import JsonCatalog
from .config import Settings
from .pricing.demo_pricing import DemoPricing
from .ranking.baseline import BaselineRanker
from .storage import open_store
from .transfer.matrix import MatrixTransfer
from .transfer.schedule import DemoSchedule
from .validation.rules import RulesValidator

if TYPE_CHECKING:
    from .storage.memory import MemoryStore
    from .storage.postgres import PostgresStore

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    agent: Agent
    catalog: JsonCatalog
    store: PostgresStore | MemoryStore
    mode: str


def build(settings: Settings, *, today: Callable[[], date] = date.today, database_url: str | None = None) -> App:
    catalog = JsonCatalog(settings.data_dir)
    location = catalog.get_location(settings.location_id)
    pricing = DemoPricing(location)
    transfer = MatrixTransfer(settings.data_dir)  # реальные дороги (OSRM), откат — по прямой
    validator = RulesValidator(today=today)
    ranker = BaselineRanker(location, pricing, transfer, schedule=DemoSchedule(location))
    store = open_store(database_url or settings.database_url)
    booking = BookingService(store, validator)

    nlu, nlg, mode = RuleNlu(), TemplateNlg(), "rules"
    if settings.llm_enabled:
        from .llm.client import OpenAICompatibleClient

        llm = OpenAICompatibleClient(
            settings.llm_base_url or None, settings.llm_api_key or None, settings.llm_model or "", timeout_s=settings.llm_timeout_s
        )
        nlu = LlmNlu(llm, location.name, {d.id: d.name for d in location.districts})
        if settings.llm_for_nlg:
            nlg = LlmNlg(llm)
        mode = "llm"
    log.info("agent mode=%s model=%s hotels=%d", mode, settings.llm_model, len(catalog.list_hotels(location.id)))

    agent = Agent(
        catalog=catalog,
        pricing=pricing,
        transfer=transfer,
        ranker=ranker,
        validator=validator,
        nlu=nlu,
        nlg=nlg,
        store=store,
        booking=booking,
        location_id=location.id,
        mode=mode,
        today=today,
    )
    return App(settings=settings, agent=agent, catalog=catalog, store=store, mode=mode)
