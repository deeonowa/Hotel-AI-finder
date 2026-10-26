"""Composition root: ЕДИНСТВЕННОЕ место, где выбираются реализации портов.

Хотите заменить демо-цены на реальный API поставщика, правила NLU на LLM, SQLite на Postgres —
меняете одну строку здесь. Агент, API и фронт не трогаются. Это и есть «строительный блок».
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .agent.nlg import LlmNlg, TemplateNlg
from .agent.nlu_llm import LlmNlu
from .agent.nlu_rules import RuleNlu
from .agent.orchestrator import Agent
from .booking.service import BookingService
from .catalog.json_catalog import JsonCatalog
from .config import Settings
from .pricing.demo_pricing import DemoPricing
from .ranking.baseline import BaselineRanker
from .storage.sqlite import SqliteStore
from .transfer.estimator import HaversineTransfer
from .validation.rules import RulesValidator

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    agent: Agent
    catalog: JsonCatalog
    store: SqliteStore
    mode: str


def build(settings: Settings, *, today: Callable[[], date] = date.today, db_path: str | None = None) -> App:
    catalog = JsonCatalog(settings.data_dir)
    location = catalog.get_location(settings.location_id)
    pricing = DemoPricing(location)
    transfer = HaversineTransfer()
    validator = RulesValidator(today=today)
    ranker = BaselineRanker(location, pricing, transfer)
    store = SqliteStore(db_path or settings.db_path)
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
