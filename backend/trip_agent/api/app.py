"""HTTP API агента (FastAPI). Владелец: FS.

Контракт: /api/v1/... (версионирован — встраиваемость в чужую систему). Фронт собирается в
web/dist и раздаётся этим же процессом → у жюри один контейнер и одна команда запуска.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..agent.orchestrator import SessionNotFound, VersionConflict
from ..config import Settings, get_settings
from ..container import App, build
from ..schemas import (
    Cart,
    CreateSessionRequest,
    HealthResponse,
    Hotel,
    LocationPack,
    Order,
    TripState,
    TurnRequest,
    TurnResponse,
)


def create_app(settings: Settings | None = None, app_ctx: App | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)
    ctx = app_ctx or build(settings)
    agent = ctx.agent

    api = FastAPI(
        title="Trip Agent API",
        version="0.1.0",
        description="AI-агент: одно сообщение путешественника → готовый сценарий поездки (трек «О!дин запрос»).",
    )
    api.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @api.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(
            llm_configured=ctx.mode == "llm",
            llm_model=settings.llm_model if ctx.mode == "llm" else None,
            location_id=settings.location_id,
            hotels_loaded=len(ctx.catalog.list_hotels(settings.location_id)),
            data_source=ctx.catalog.data_source(settings.location_id),
        )

    @api.post("/api/v1/sessions", response_model=TurnResponse, tags=["agent"])
    def create_session(body: CreateSessionRequest | None = None) -> TurnResponse:
        return agent.new_session(body.user_id if body else None)

    @api.post("/api/v1/sessions/{session_id}/turns", response_model=TurnResponse, tags=["agent"])
    def turn(session_id: str, body: TurnRequest) -> TurnResponse:
        try:
            return agent.turn(session_id, body)
        except SessionNotFound as e:
            raise HTTPException(404, "session not found") from e
        except VersionConflict as e:
            raise HTTPException(409, str(e)) from e

    @api.get("/api/v1/sessions/{session_id}", response_model=TripState, tags=["agent"])
    def get_session(session_id: str) -> TripState:
        try:
            return agent.get_state(session_id)
        except SessionNotFound as e:
            raise HTTPException(404, "session not found") from e

    @api.get("/api/v1/carts/{cart_id}", response_model=Cart, tags=["booking"])
    def get_cart(cart_id: str) -> Cart:
        cart = ctx.store.get_cart(cart_id)
        if cart is None:
            raise HTTPException(404, "cart not found")
        return cart

    @api.get("/api/v1/orders/{order_id}", response_model=Order, tags=["booking"])
    def get_order(order_id: str) -> Order:
        order = ctx.store.get_order(order_id)
        if order is None:
            raise HTTPException(404, "order not found")
        return order

    @api.get("/api/v1/locations/{location_id}", response_model=LocationPack, tags=["catalog"])
    def get_location(location_id: str) -> LocationPack:
        try:
            return ctx.catalog.get_location(location_id)
        except KeyError as e:
            raise HTTPException(404, "location not found") from e

    @api.get("/api/v1/hotels/{hotel_id}", response_model=Hotel, tags=["catalog"])
    def get_hotel(hotel_id: str) -> Hotel:
        h = ctx.catalog.get_hotel(hotel_id)
        if h is None:
            raise HTTPException(404, "hotel not found")
        return h

    _mount_web(api, settings.web_dist_dir)
    return api


def _mount_web(api: FastAPI, dist: Path) -> None:
    """Собранный фронт (web/dist) раздаём с корня; без сборки — только API."""
    if not (dist / "index.html").exists():
        return
    api.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @api.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        f = dist / path
        return FileResponse(f if path and f.is_file() else dist / "index.html")
