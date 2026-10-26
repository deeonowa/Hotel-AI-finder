"""python -m trip_agent [serve|chat|migrate]

serve — HTTP API (+ собранный фронт), по умолчанию :8000
chat  — диалог с агентом в терминале (удобно ML1/ML2 отлаживать без фронта, БД в памяти)
migrate — накатить миграции схемы Postgres по DATABASE_URL и выйти
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser(prog="trip_agent")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("chat")
    sub.add_parser("migrate")
    args = ap.parse_args()

    if args.cmd == "migrate":
        import psycopg

        from .config import get_settings
        from .storage.migrations import migrate

        with psycopg.connect(get_settings().database_url) as conn:
            applied = migrate(conn)
        print(f"[migrate] применено: {applied or 'ничего, схема актуальна'}")
        return

    if args.cmd == "chat":
        from .config import get_settings
        from .container import build
        from .schemas import TurnRequest

        app = build(get_settings(), database_url="memory://")
        r = app.agent.new_session()
        sid = r.session_id
        _print(r)
        while True:
            try:
                text = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text:
                _print(app.agent.turn(sid, TurnRequest(text=text)))
        return

    import uvicorn

    uvicorn.run("trip_agent.api.app:create_app", factory=True, host=getattr(args, "host", "0.0.0.0"), port=getattr(args, "port", 8000))


def _print(r) -> None:  # noqa: ANN001
    print(f"\n[{r.mode} · {r.stage.value} · v{r.version}]")
    for t in r.trace:
        print(f"  · {t.name}: {t.detail} ({t.ms} ms)")
    for b in r.blocks:
        if b.type == "text":
            print(b.markdown)
        elif b.type == "question":
            print("? " + b.text + "  " + " | ".join(q.label for q in b.quick_replies))
        elif b.type == "variants":
            for v in b.items:
                print(f"  ■ {v.title}: {v.hotel.name} ({v.hotel.district_name}) — {v.total_price} ₽, score {v.score:.2f}")
                for w in v.why:
                    print(f"      + {w}")
                for w in v.tradeoffs:
                    print(f"      − {w}")
                for w in v.fit_warnings + v.issues:
                    print(f"      ! {w.message}")
        elif b.type == "no_results":
            print("  нет вариантов:", json.dumps(b.rejected, ensure_ascii=False))
            for x in b.relaxations:
                print(f"    → {x.label}: {x.results_count}")
        else:
            print(f"  [{b.type}]")
    if r.suggestions:
        print("  кнопки: " + " | ".join(q.label for q in r.suggestions))


if __name__ == "__main__":
    main()
