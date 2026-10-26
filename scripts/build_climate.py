"""Собрать климатические нормы по месяцам для локации из Open-Meteo (CC BY 4.0).

Запуск (только stdlib, без зависимостей):
    python scripts/build_climate.py --location sochi

Берёт точки climate_points из data/locations/<id>/location.json, считает средние за
период --start..--end (по умолчанию 2019–2025) и записывает location.json["climate"].
Для прогноза на месяцы вперёд честно используем КЛИМАТ, а не «погоду» — так и говорим пользователю.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def _get(url: str, params: dict, retries: int = 6) -> dict:
    """GET с повтором: бесплатный Open-Meteo отвечает 429 при частых тяжёлых запросах."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(full, timeout=120) as resp:  # noqa: S310 (фиксированный хост)
                return json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError) as e:
            retriable = not isinstance(e, urllib.error.HTTPError) or e.code in (429, 500, 502, 503, 504)
            if not retriable or attempt == retries - 1:
                raise
            wait = 15 * (attempt + 1)
            print(f"[climate] {e}; повтор через {wait}s…")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _years(start: str, end: str) -> list[tuple[str, str]]:
    """Режем период по годам: длинные ответы бесплатный API иногда обрывает."""
    y0, y1 = int(start[:4]), int(end[:4])
    return [(f"{y}-01-01" if y > y0 else start, f"{y}-12-31" if y < y1 else end) for y in range(y0, y1 + 1)]


def _merged(url: str, params: dict, key: str, start: str, end: str) -> dict[str, list]:
    merged: dict[str, list] = defaultdict(list)
    for s, e in _years(start, end):
        part = _get(url, {**params, "start_date": s, "end_date": e})[key]
        for k, v in part.items():
            merged[k].extend(v)
        time.sleep(4)
    return merged


def air_normals(lat: float, lon: float, start: str, end: str) -> dict[int, dict]:
    data = _merged(
        ARCHIVE,
        {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "Europe/Moscow",
        },
        "daily",
        start,
        end,
    )
    by_month: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    years: set[int] = set()
    for day, tmax, tmin, prcp in zip(
        data["time"], data["temperature_2m_max"], data["temperature_2m_min"], data["precipitation_sum"], strict=True
    ):
        y, m = int(day[:4]), int(day[5:7])
        years.add(y)
        if tmax is not None:
            by_month[m]["tmax"].append(tmax)
        if tmin is not None:
            by_month[m]["tmin"].append(tmin)
        if prcp is not None:
            by_month[m]["wet"].append(1.0 if prcp >= 1.0 else 0.0)
    n_years = max(len(years), 1)
    out = {}
    for m in range(1, 13):
        b = by_month[m]
        out[m] = {
            "air_temp_day_c": round(statistics.mean(b["tmax"]), 1) if b["tmax"] else None,
            "air_temp_night_c": round(statistics.mean(b["tmin"]), 1) if b["tmin"] else None,
            # среднее число дней с осадками ≥1 мм в месяце
            "precip_days": round(sum(b["wet"]) / n_years, 1) if b["wet"] else None,
        }
    return out


def sea_normals(lat: float, lon: float, start: str, end: str) -> dict[int, float]:
    data = _merged(
        MARINE,
        {"latitude": lat, "longitude": lon, "hourly": "sea_surface_temperature"},
        "hourly",
        start,
        end,
    )
    by_month: dict[int, list[float]] = defaultdict(list)
    for ts, v in zip(data["time"], data["sea_surface_temperature"], strict=True):
        if v is not None:
            by_month[int(ts[5:7])].append(v)
    return {m: round(statistics.mean(v), 1) for m, v in by_month.items() if v}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="sochi")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--force", action="store_true", help="пересобрать зоны, которые уже есть")
    args = ap.parse_args()

    path = ROOT / "data" / "locations" / args.location / "location.json"
    loc = json.loads(path.read_text(encoding="utf-8"))
    climate = dict(loc.get("climate") or {})
    for zone, pt in loc["climate_points"].items():
        if zone in climate and not args.force:
            print(f"[climate] {zone}: уже есть, пропускаю (--force чтобы пересобрать)")
            continue
        print(f"[climate] {zone}: air {pt['air']}")
        air = air_normals(pt["air"]["lat"], pt["air"]["lon"], args.start, args.end)
        sea = {}
        if pt.get("sea"):
            print(f"[climate] {zone}: sea {pt['sea']}")
            sea = sea_normals(pt["sea"]["lat"], pt["sea"]["lon"], args.start, args.end)
        climate[zone] = [
            {
                "month": m,
                **air[m],
                "sea_temp_c": sea.get(m),
                "source": f"open-meteo {args.start[:4]}–{args.end[:4]} mean",
            }
            for m in range(1, 13)
        ]
        loc["climate"] = climate  # пишем после каждой зоны: лимиты API не съедят уже собранное
        path.write_text(json.dumps(loc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[climate] {zone} written → {path}")


if __name__ == "__main__":
    main()
