"""Версионированные миграции схемы Postgres. Владелец: FS.

Правило: уже выпущенную миграцию НЕ редактируем — добавляем новую в конец MIGRATIONS.
Применяются при старте приложения (PostgresStore) или вручную: `python -m trip_agent migrate`.
Advisory-lock не даёт двум репликам накатывать миграции одновременно.
"""

from __future__ import annotations

import psycopg

LOCK_ID = 7_340_001  # произвольная константа для pg_advisory_xact_lock

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "sessions, carts, orders (JSONB-документы)",
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            body       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS carts (
            id         TEXT PRIMARY KEY,
            body       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS orders (
            id         TEXT PRIMARY KEY,
            body       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS sessions_updated_at_idx ON sessions (updated_at);
        """,
    ),
]


def migrate(conn: psycopg.Connection) -> list[int]:
    """Накатить недостающие миграции. Возвращает номера применённых."""
    applied: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, name, ddl in MIGRATIONS:
            if version in done:
                continue
            conn.execute(ddl)
            conn.execute("INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (version, name))
            applied.append(version)
    return applied
