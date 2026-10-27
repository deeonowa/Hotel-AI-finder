"""Конфигурация через переменные окружения / .env. Никаких ключей в коде и в git."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # LLM (любой OpenAI-совместимый провайдер). Нет LLM_MODEL → режим правил, агент всё равно работает.
    llm_base_url: str | None = None  # https://openrouter.ai/api/v1 | http://ollama:11434/v1 | ...
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: float = 30.0
    agent_mode: Literal["auto", "llm", "rules"] = "auto"  # auto = llm, если задан LLM_MODEL
    llm_for_nlg: bool = True  # «очеловечивать» тексты через LLM (с проверкой заземления)

    data_dir: Path = ROOT / "data"
    location_id: str = "sochi"  # направление по умолчанию, если пользователь не назвал город
    location_ids: list[str] = []  # пусто = все города из data/locations/; LOCATION_IDS='["sochi","dubai"]' — только эти
    # postgresql://user:pass@host:5432/db | memory:// (без БД — для eval/экспорта схемы/chat)
    database_url: str = "postgresql://trip:trip@localhost:5432/trip_agent"
    web_dist_dir: Path = ROOT / "web" / "dist"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    log_level: str = "INFO"

    @property
    def llm_enabled(self) -> bool:
        if self.agent_mode == "rules":
            return False
        return bool(self.llm_model)


def get_settings() -> Settings:
    return Settings()
