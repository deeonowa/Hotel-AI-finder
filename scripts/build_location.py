"""Собрать РЕАЛЬНЫЙ снимок отелей и POI из OpenStreetMap (Overpass API) для локации.

    python scripts/build_location.py --location sochi            # перезапишет hotels.json и pois.json
    python scripts/build_location.py --location sochi --dry-run  # только статистика

Запускается ОДИН раз у разработчика (ML2), результат коммитится. Во время работы агента
сеть не нужна — у жюри всё запустится офлайн. Лицензия данных: ODbL (атрибуция в README).

Что реальное: название, тип, координаты, адрес, звёзды (если есть тег), часть удобств
(swimming_pool, internet_access, wheelchair, parking), политика по животным (pets/dog).
Что синтетическое: см. scripts/synth.py (поле provenance.synthetic_fields у каждого отеля).

ВНИМАНИЕ: скрипт написан до проверки на живом Overpass (из песочницы он недоступен).
ML2 — прогнать первым делом, поправить маппинг тегов по факту.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from synth import enrich, haversine_m

ROOT = Path(__file__).resolve().parents[1]
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

HOTEL_QUERY = """
[out:json][timeout:180];
(
  nwr["tourism"~"^(hotel|guest_house|hostel|apartment|motel|chalet)$"]({bbox});
  nwr["leisure"="resort"]["name"]({bbox});
);
out center tags;
"""
POI_QUERY = """
[out:json][timeout:180];
(
  nwr["natural"="beach"]({bbox});
  nwr["leisure"="water_park"]({bbox});
  nwr["leisure"="park"]["name"]({bbox});
  nwr["tourism"~"^(attraction|museum|viewpoint|zoo)$"]["name"]({bbox});
  nwr["aerialway"="station"]["name"]({bbox});
);
out center tags;
"""
COAST_QUERY = """
[out:json][timeout:180];
way["natural"="coastline"]({bbox});
out geom;
"""


def overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for ep in ENDPOINTS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(ep, data=data, headers={"User-Agent": "trip-agent-hackathon/0.1"})
                with urllib.request.urlopen(req, timeout=240) as r:  # noqa: S310
                    return json.loads(r.read().decode())
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                print(f"[osm] {ep} attempt {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Overpass недоступен: {last}")


def _center(el: dict) -> tuple[float, float] | None:
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return None


def _kind(tags: dict) -> str:
    name = (tags.get("name:ru") or tags.get("name") or "").lower()
    if "санатор" in name or tags.get("healthcare") == "sanatorium":
        return "sanatorium"
    if tags.get("leisure") == "resort":
        return "resort"
    t = tags.get("tourism", "hotel")
    if t == "hotel" and ("гостевой дом" in name or "гостиный двор" in name):
        return "guest_house"
    if t == "hotel" and "апарт" in name:
        return "apartment"
    return {
        "hotel": "hotel",
        "guest_house": "guest_house",
        "hostel": "hostel",
        "apartment": "apartment",
        "motel": "motel",
        "chalet": "chalet",
    }.get(t, "hotel")


def _stars(tags: dict) -> int | None:
    raw = (tags.get("stars") or "").strip()[:1]
    return int(raw) if raw.isdigit() and 0 < int(raw) <= 5 else None


def _known_amenities(tags: dict) -> list[str]:
    a = []
    if tags.get("swimming_pool") in ("yes", "outdoor", "indoor"):
        a.append("pool")
    if tags.get("internet_access") in ("wlan", "yes", "wifi"):
        a.append("wifi")
    if tags.get("wheelchair") == "yes":
        a.append("wheelchair_accessible")
    if tags.get("parking") or tags.get("parking:fee"):
        a.append("parking")
    return a


def _pets(tags: dict) -> dict | None:
    v = tags.get("pets") or tags.get("dog")
    if v in ("yes", "leashed"):
        return {"allowed": True, "fee_per_night": None, "max_weight_kg": None}
    if v == "no":
        return {"allowed": False, "fee_per_night": None, "max_weight_kg": None}
    return None


class Coastline:
    """Расстояние до береговой линии: проекция в локальные км и минимум по отрезкам."""

    def __init__(self, ways: list[list[tuple[float, float]]], lat0: float):
        self.k = 111.0 * math.cos(math.radians(lat0))
        self.segments = []
        for pts in ways:
            xy = [(lon * self.k, lat * 111.0) for lat, lon in pts]
            self.segments += list(zip(xy, xy[1:], strict=False))

    def distance_m(self, lat: float, lon: float) -> int | None:
        if not self.segments:
            return None
        px, py = lon * self.k, lat * 111.0
        best = float("inf")
        for (x1, y1), (x2, y2) in self.segments:
            dx, dy = x2 - x1, y2 - y1
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
        return int(best * 1000)


def nearest_district(lat: float, lon: float, districts: list[dict], max_km: float = 12.0) -> dict | None:
    best = min(districts, key=lambda d: haversine_m(lat, lon, d["center"]["lat"], d["center"]["lon"]))
    km = haversine_m(lat, lon, best["center"]["lat"], best["center"]["lon"]) / 1000
    return best if km <= max(max_km, best["radius_km"] * 2.5) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="sochi")
    ap.add_argument("--seed", default="osm-v1")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    loc_dir = ROOT / "data" / "locations" / args.location
    loc = json.loads((loc_dir / "location.json").read_text(encoding="utf-8"))
    b = loc["bbox"]
    bbox = f"{b['south']},{b['west']},{b['north']},{b['east']}"
    districts = {d["id"]: d for d in loc["districts"]}

    print("[osm] coastline…")
    coast_raw = overpass(COAST_QUERY.format(bbox=bbox))
    coast = Coastline(
        [[(p["lat"], p["lon"]) for p in w.get("geometry", [])] for w in coast_raw["elements"]], loc["center"]["lat"]
    )
    print(f"[osm] coastline segments: {len(coast.segments)}")

    print("[osm] hotels…")
    raw = overpass(HOTEL_QUERY.format(bbox=bbox))
    hotels, skipped = [], Counter()
    seen_names: set[tuple[str, str]] = set()
    for el in raw["elements"]:
        tags = el.get("tags", {})
        name = tags.get("name:ru") or tags.get("name")
        pt = _center(el)
        if not name or not pt:
            skipped["no_name_or_point"] += 1
            continue
        d = nearest_district(pt[0], pt[1], loc["districts"])
        if d is None:
            skipped["outside_districts"] += 1
            continue
        key = (name.lower(), d["id"])
        if key in seen_names:
            skipped["duplicate"] += 1
            continue
        seen_names.add(key)
        dist_sea = coast.distance_m(*pt) if d["kind"] == "sea" else None
        addr = " ".join(x for x in [tags.get("addr:street"), tags.get("addr:housenumber")] if x) or None
        base = {
            "id": f"osm-{el['type'][0]}{el['id']}",
            "name": name,
            "kind": _kind(tags),
            "location_id": loc["id"],
            "district_id": d["id"],
            "point": {"lat": round(pt[0], 6), "lon": round(pt[1], 6)},
            "address": addr,
            "stars": _stars(tags),
            "amenities_known": _known_amenities(tags),
            "distance_to_sea_m": dist_sea,
            "distance_to_center_m": int(haversine_m(pt[0], pt[1], loc["center"]["lat"], loc["center"]["lon"])),
            "provenance": {"source": "osm", "source_id": f"{el['type']}/{el['id']}"},
        }
        h = enrich(base, districts[d["id"]], seed=args.seed)
        real_pets = _pets(tags)
        if real_pets:
            h["pets"] = real_pets
            h["provenance"]["synthetic_fields"] = [f for f in h["provenance"]["synthetic_fields"] if f != "pets"]
        hotels.append(h)

    print("[osm] pois…")
    praw = overpass(POI_QUERY.format(bbox=bbox))
    pois = []
    for el in praw["elements"]:
        tags = el.get("tags", {})
        pt = _center(el)
        if not pt:
            continue
        kind = (
            "beach"
            if tags.get("natural") == "beach"
            else "water_park"
            if tags.get("leisure") == "water_park"
            else "park"
            if tags.get("leisure") == "park"
            else "ski_lift"
            if tags.get("aerialway") == "station"
            else tags.get("tourism", "attraction")
        )
        d = nearest_district(pt[0], pt[1], loc["districts"])
        pois.append(
            {
                "id": f"osm-{el['type'][0]}{el['id']}",
                "name": tags.get("name:ru") or tags.get("name") or {"beach": "Пляж"}.get(kind, kind),
                "kind": kind,
                "point": {"lat": round(pt[0], 6), "lon": round(pt[1], 6)},
                "district_id": d["id"] if d else None,
                "tags": [t for t in ("osm",)],
            }
        )

    by_d = Counter(h["district_id"] for h in hotels)
    by_k = Counter(h["kind"] for h in hotels)
    print(f"[osm] hotels: {len(hotels)}  by district: {dict(by_d)}  by kind: {dict(by_k)}  skipped: {dict(skipped)}")
    print(f"[osm] pois: {len(pois)}  by kind: {dict(Counter(p['kind'] for p in pois))}")
    if args.dry_run:
        return
    meta = {
        "source": "osm",
        "license": "ODbL 1.0 © OpenStreetMap contributors",
        "generator": "scripts/build_location.py",
        "seed": args.seed,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(hotels),
    }
    (loc_dir / "hotels.json").write_text(
        json.dumps({"meta": meta, "hotels": hotels}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (loc_dir / "pois.json").write_text(
        json.dumps({"meta": {**meta, "count": len(pois)}, "pois": pois}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"[osm] written → {loc_dir}")


if __name__ == "__main__":
    main()
