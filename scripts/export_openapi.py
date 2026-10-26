"""Выгрузить OpenAPI-схему API в web/openapi.json → из неё генерируются TS-типы фронта.

    python scripts/export_openapi.py && (cd web && npm run gen:types)

Запускать после ЛЮБОГО изменения backend/trip_agent/schemas — иначе фронт и бэк разъедутся.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from trip_agent.api.app import create_app  # noqa: E402
from trip_agent.config import Settings  # noqa: E402

if __name__ == "__main__":
    api = create_app(Settings(agent_mode="rules", db_path=":memory:"))
    out = ROOT / "web" / "openapi.json"
    out.write_text(json.dumps(api.openapi(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"[openapi] → {out}")
