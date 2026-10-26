"""LLM-клиент поверх OpenAI-совместимого API. Владелец: ML1.

Один код для OpenRouter / vLLM / Ollama / LM Studio / любого совместимого провайдера —
меняется только .env (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL). Ключи организаторов, наши или
локальная модель — решается конфигом, без правок кода.

Надёжность структурированного вывода:
  1) пробуем response_format=json_schema (строгая схема);
  2) провайдер не умеет → json_object + схема в промпте;
  3) ответ не прошёл Pydantic → 1 повтор с текстом ошибки;
  4) всё равно плохо → LLMError, вызывающий код падает в rules-режим (агент не ломается).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


class LLMError(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = _THINK.sub("", text or "").strip()
    text = _FENCE.sub("", text).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


class OpenAICompatibleClient:
    def __init__(self, base_url: str | None, api_key: str | None, model: str, *, timeout_s: float = 30.0):
        from openai import OpenAI  # импорт здесь: без LLM пакет не обязателен для тестов

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key or "not-needed", timeout=timeout_s, max_retries=1)
        self._json_schema_ok: bool | None = None  # определяем на первом вызове
        self.last_latency_ms = 0

    def _create(self, messages: list[dict], temperature: float, response_format: dict | None, max_tokens: int):
        t0 = time.perf_counter()
        kw = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if response_format:
            kw["response_format"] = response_format
        resp = self._client.chat.completions.create(**kw)
        self.last_latency_ms = int((time.perf_counter() - t0) * 1000)
        return resp.choices[0].message.content or ""

    def complete_json(self, system: str, user: str, schema: type[T], *, temperature: float = 0.0) -> T:
        from openai import BadRequestError

        js = schema.model_json_schema()
        sys = system + "\n\nОтвечай ТОЛЬКО JSON-объектом по схеме:\n" + json.dumps(js, ensure_ascii=False)
        messages = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        last_err: Exception | None = None
        for _attempt in range(2):
            rf = (
                {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": js, "strict": False}}
                if self._json_schema_ok is not False
                else {"type": "json_object"}
            )
            try:
                raw = self._create(messages, temperature, rf, max_tokens=1200)
                if self._json_schema_ok is None:
                    self._json_schema_ok = True
            except BadRequestError as e:
                if self._json_schema_ok is None:
                    log.info("provider has no json_schema support, falling back to json_object: %s", e)
                    self._json_schema_ok = False
                    continue
                raise LLMError(str(e)) from e
            except Exception as e:  # сеть, таймаут, 5xx
                raise LLMError(str(e)) from e
            text = _clean(raw)
            try:
                return schema.model_validate_json(text)
            except ValidationError as e:
                last_err = e
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Ответ не прошёл валидацию: {e.errors()[:5]}. Исправь и верни только JSON."},
                ]
        raise LLMError(f"invalid structured output: {last_err}")

    def complete_text(self, system: str, user: str, *, temperature: float = 0.3, max_tokens: int = 600) -> str:
        try:
            raw = self._create(
                [{"role": "system", "content": system}, {"role": "user", "content": user}], temperature, None, max_tokens
            )
        except Exception as e:
            raise LLMError(str(e)) from e
        return _THINK.sub("", raw).strip()
