"""Сквозной сценарий через HTTP API: от первого сообщения до оплаченного заказа."""

from __future__ import annotations

from fastapi.testclient import TestClient
from trip_agent.api.app import create_app
from trip_agent.config import Settings


def _client(app) -> TestClient:
    return TestClient(create_app(Settings(agent_mode="rules"), app_ctx=app))


def _types(resp: dict) -> list[str]:
    return [b["type"] for b in resp["blocks"]]


def test_full_journey(app):
    c = _client(app)
    r = c.post("/api/v1/sessions").json()
    sid = r["session_id"]
    assert r["suggestions"], "приветствие с примерами"

    # 1. размытый запрос без дат → ровно один вопрос
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "Хотим с женой и сыном 6 лет к морю, до 120 тысяч"}).json()
    assert "question" in _types(r) and "variants" not in _types(r)

    # 2. ответ → варианты с объяснениями
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "в ноябре на неделю", "expected_version": r["version"]}).json()
    assert "variants" in _types(r) and "assumptions" in _types(r)
    variants = next(b for b in r["blocks"] if b["type"] == "variants")["items"]
    assert 1 <= len(variants) <= 3 and all(v["why"] for v in variants)

    # 3. пересборка: diff, а не поиск с нуля
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "а если на два дня позже?"}).json()
    assert "diff" in _types(r)

    # 4. выбор и корзина через действия UI (без LLM)
    vid = next(b for b in r["blocks"] if b["type"] == "variants")["recommended_id"]
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"action": {"type": "add_to_cart", "variant_id": vid}}).json()
    assert r["stage"] == "in_cart" and "cart" in _types(r)

    # 5. время прилёта → перепроверка корзины, предложение раннего заезда
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "прилетаю в 7:40"}).json()
    assert r["stage"] == "in_cart"
    cart = next(b for b in r["blocks"] if b["type"] == "cart")["cart"]
    if any(i["code"] == "early_arrival" and i.get("fix") for i in cart["issues"]):
        r = c.post(f"/api/v1/sessions/{sid}/turns", json={"action": {"type": "add_extra", "kind": "early_check_in"}}).json()
        cart = next(b for b in r["blocks"] if b["type"] == "cart")["cart"]
        assert any(i["kind"] == "early_check_in" for i in cart["items"])

    # 6. оформление и имитация оплаты
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"action": {"type": "checkout", "guest_name": "Иван"}}).json()
    order = next(b for b in r["blocks"] if b["type"] == "order")["order"]
    assert order["status"] == "awaiting_payment" and order["number"].startswith("OST-")
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"action": {"type": "pay", "order_id": order["id"]}}).json()
    order = next(b for b in r["blocks"] if b["type"] == "order")["order"]
    assert order["status"] == "paid" and order["payment_ref"].startswith("SIM-")
    assert r["stage"] == "ordered"

    # 7. после покупки
    r = c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "можно ранний заезд?"}).json()
    assert "order" in _types(r)


def test_version_conflict_and_404(app):
    c = _client(app)
    sid = c.post("/api/v1/sessions").json()["session_id"]
    assert c.post(f"/api/v1/sessions/{sid}/turns", json={"text": "привет", "expected_version": 42}).status_code == 409
    assert c.post("/api/v1/sessions/nope/turns", json={"text": "привет"}).status_code == 404


def test_health(app):
    h = _client(app).get("/api/v1/health").json()
    assert h["status"] == "ok" and h["hotels_loaded"] > 0 and h["llm_configured"] is False
