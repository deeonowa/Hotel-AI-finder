"""Composition root: ЕДИНСТВЕННОЕ место, где выбираются реализации портов.

Хотите заменить демо-цены на реальный API поставщика, правила NLU на LLM, Postgres на другое хранилище,
добавить город (папка data/locations/<id>/) —
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
from .locations import LocationRegistry
from .pricing.demo_pricing import DemoPricing
from .ranking.baseline import BaselineRanker
from .storage import open_store
from .transfer.matrix import MatrixTransfer
from .validation.rules import RulesValidator

if TYPE_CHECKING:
    from .ports import LLMClient
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
    locations: LocationRegistry


def build(
    settings: Settings,
    *,
    today: Callable[[], date] = date.today,
    database_url: str | None = None,
    llm: LLMClient | None = None,
) -> App:
    """llm — готовый клиент (тесты подставляют поддельную модель); иначе создаётся по LLM_* из настроек."""
    catalog = JsonCatalog(settings.data_dir)
    transfer = MatrixTransfer(settings.data_dir)  # реальные дороги (OSRM), откат — по прямой
    # Все города из data/locations/ (или только перечисленные в LOCATION_IDS): цены, расписание и ранжирование — свои у каждого.
    locations = LocationRegistry(
        catalog,
        transfer,
        default_id=settings.location_id,
        ids=settings.location_ids or None,
        pricing_factory=DemoPricing,
        ranker_factory=lambda loc, pricing, tr, schedule: BaselineRanker(loc, pricing, tr, schedule=schedule),
    )
    packs = locations.packs()
    validator = RulesValidator(today=today, location_of=catalog.get_location, multi_location=len(packs) > 1)
    store = open_store(database_url or settings.database_url)
    booking = BookingService(store, validator)

    nlu, nlg, mode = RuleNlu(packs), TemplateNlg(), "rules"
    default = locations.get(settings.location_id).pack
    if llm is None and settings.llm_enabled:
        from .llm.client import OpenAICompatibleClient

        llm = OpenAICompatibleClient(
            settings.llm_base_url or None,
            settings.llm_api_key or None,
            settings.llm_model or "",
            timeout_s=settings.llm_timeout_s,
            cooldown_s=settings.llm_cooldown_s,
        )
    if llm is not None:
        nlu = LlmNlu(
            llm,
            default.name,
            {d.id: d.name for d in default.districts},
            RuleNlu(packs),
            locations=packs,
            min_confidence=settings.llm_min_confidence,
        )
        if settings.llm_for_nlg:
            nlg = LlmNlg(llm)
        mode = "llm"
    log.info(
        "agent mode=%s model=%s locations=%s",
        mode,
        settings.llm_model,
        ", ".join(f"{lp.id}:{len(catalog.list_hotels(lp.id))}" for lp in packs),
    )

    agent = Agent(
        catalog=catalog,
        transfer=transfer,
        locations=locations,
        validator=validator,
        nlu=nlu,
        nlg=nlg,
        store=store,
        booking=booking,
        mode=mode,
        today=today,
    )
    return App(settings=settings, agent=agent, catalog=catalog, store=store, mode=mode, locations=locations)
