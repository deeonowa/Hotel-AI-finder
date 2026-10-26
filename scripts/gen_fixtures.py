"""Сгенерировать ФИКСТУРНЫЙ каталог (вымышленные отели) — чтобы все трое работали с первого часа.

    python scripts/gen_fixtures.py --location sochi --per-district 14

Пишет data/locations/<id>/hotels.json и pois.json с provenance.source = "fixture".
Как только ML2 соберёт реальный снимок (scripts/build_location.py, OSM), он перезапишет эти файлы.
Названия вымышленные, координаты — разбросаны вокруг центров районов вглубь суши от берега.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from synth import enrich, haversine_m

ROOT = Path(__file__).resolve().parents[1]

# Направление «вглубь суши» для побережья Большого Сочи: берег идёт с СЗ на ЮВ, море на ЮЗ.
COAST_DIR = (-0.748, 0.664)  # (север, восток) в км — вдоль берега на ЮВ
INLAND_DIR = (0.664, 0.748)  # перпендикуляр — на СВ, от моря

SEA_WORDS = [
    "Магнолия",
    "Кипарис",
    "Лазурь",
    "Бриз",
    "Чайка",
    "Самшит",
    "Эвкалипт",
    "Прибой",
    "Альбатрос",
    "Олива",
    "Волна",
    "Пальмира",
    "Жемчужина",
    "Маяк",
    "Акватория",
    "Лагуна",
    "Парус",
    "Гранат",
    "Мимоза",
    "Инжир",
    "Платан",
    "Дельфин",
    "Бухта",
    "Ракушка",
    "Морской конёк",
    "Сирень",
]
MOUNTAIN_WORDS = [
    "Эдельвейс",
    "Пихта",
    "Кедр",
    "Снежный барс",
    "Высота",
    "Горный ручей",
    "Альпика",
    "Каштан",
    "Перевал",
    "Тур",
    "Рододендрон",
    "Облака",
    "Вершина",
    "Шале Ласточка",
]
KIND_LABEL = {
    "hotel": "Отель «{}»",
    "resort": "Курорт «{}»",
    "sanatorium": "Санаторий «{}»",
    "guest_house": "Гостевой дом «{}»",
    "apartment": "Апартаменты «{}»",
    "hostel": "Хостел «{}»",
    "chalet": "Шале «{}»",
    "motel": "Мотель «{}»",
}
SEA_KINDS = ["hotel"] * 5 + ["guest_house"] * 4 + ["apartment"] * 2 + ["resort", "sanatorium", "hostel"]
MOUNTAIN_KINDS = ["hotel"] * 6 + ["chalet"] * 3 + ["apartment"] * 2 + ["guest_house", "hostel", "resort"]


def offset(lat: float, lon: float, north_km: float, east_km: float) -> tuple[float, float]:
    return lat + north_km / 111.0, lon + east_km / (111.0 * math.cos(math.radians(lat)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="sochi")
    ap.add_argument("--per-district", type=int, default=14)
    ap.add_argument("--seed", default="fixture-v1")
    args = ap.parse_args()

    loc_dir = ROOT / "data" / "locations" / args.location
    loc = json.loads((loc_dir / "location.json").read_text(encoding="utf-8"))
    center = loc["center"]
    hotels, pois = [], []
    used_names: set[str] = set()

    for d in loc["districts"]:
        rng = random.Random(f"{args.seed}:{d['id']}")
        mountain = d["kind"] == "mountain"
        words = MOUNTAIN_WORDS if mountain else SEA_WORDS
        kinds = MOUNTAIN_KINDS if mountain else SEA_KINDS
        for i in range(args.per_district):
            kind = kinds[i % len(kinds)]
            for _ in range(50):
                name = KIND_LABEL[kind].format(rng.choice(words))
                if name not in used_names:
                    break
            used_names.add(name)
            c = d["center"]
            if mountain:
                ang, r = rng.uniform(0, 2 * math.pi), rng.uniform(0.2, d["radius_km"])
                lat, lon = offset(c["lat"], c["lon"], r * math.sin(ang), r * math.cos(ang))
                dist_sea = None
            else:
                along = rng.uniform(-d["radius_km"], d["radius_km"])
                inland_km = rng.choice([0.05, 0.1, 0.2, 0.35, 0.5, 0.8, 1.2, 1.8, 2.5])
                n = along * COAST_DIR[0] + inland_km * INLAND_DIR[0]
                e = along * COAST_DIR[1] + inland_km * INLAND_DIR[1]
                lat, lon = offset(c["lat"], c["lon"], n, e)
                dist_sea = int(inland_km * 1000)
            hid = f"fx-{d['id']}-{i:02d}"
            base = {
                "id": hid,
                "name": name,
                "kind": kind,
                "location_id": loc["id"],
                "district_id": d["id"],
                "point": {"lat": round(lat, 6), "lon": round(lon, 6)},
                "distance_to_sea_m": dist_sea,
                "distance_to_center_m": int(haversine_m(lat, lon, center["lat"], center["lon"])),
                "description": f"{name.split('«')[0].strip()} в районе {d['name']}. Демо-объект для разработки.",
                "provenance": {"source": "fixture", "source_id": None},
            }
            hotels.append(enrich(base, d, seed=args.seed))

        # POI-фикстуры: пляж у центра морского района, парк/подъёмник в горах.
        c = d["center"]
        if mountain:
            pois.append(
                {
                    "id": f"poi-{d['id']}-lift",
                    "name": "Канатная дорога (демо-точка)",
                    "kind": "ski_lift",
                    "point": c,
                    "district_id": d["id"],
                    "tags": ["fixture"],
                }
            )
        else:
            pois.append(
                {
                    "id": f"poi-{d['id']}-beach",
                    "name": f"Городской пляж — {d['name']}",
                    "kind": "beach",
                    "point": c,
                    "district_id": d["id"],
                    "tags": ["fixture"],
                }
            )
            plat, plon = offset(c["lat"], c["lon"], 0.6 * INLAND_DIR[0], 0.6 * INLAND_DIR[1])
            pois.append(
                {
                    "id": f"poi-{d['id']}-park",
                    "name": f"Парк — {d['name']}",
                    "kind": "park",
                    "point": {"lat": round(plat, 6), "lon": round(plon, 6)},
                    "district_id": d["id"],
                    "tags": ["fixture"],
                }
            )

    meta = {"source": "fixture", "generator": "scripts/gen_fixtures.py", "seed": args.seed, "count": len(hotels)}
    (loc_dir / "hotels.json").write_text(
        json.dumps({"meta": meta, "hotels": hotels}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (loc_dir / "pois.json").write_text(
        json.dumps({"meta": {**meta, "count": len(pois)}, "pois": pois}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"[fixtures] {len(hotels)} hotels, {len(pois)} pois → {loc_dir}")


if __name__ == "__main__":
    main()
