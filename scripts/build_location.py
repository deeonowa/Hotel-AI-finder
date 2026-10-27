"""Собрать РЕАЛЬНЫЙ снимок локации: отели и POI из OpenStreetMap + вычисленные признаки.

    python scripts/build_location.py --location sochi              # полный прогон (запись в data/)
    python scripts/build_location.py --location sochi --dry-run    # только статистика
    python scripts/build_location.py --location sochi --refresh    # игнорировать кэш сети
    python scripts/build_location.py --location dubai --max-hotels 900   # крупный город: самые информативные объекты

Новая локация: data/locations/<id>/location.json (районы, хабы, сезонность, маршруты, правила въезда) →
этот скрипт → scripts/build_climate.py --location <id>. Код агента не меняется.

Запускается у разработчика (ML2), результат коммитится. Во время работы агента сеть НЕ нужна.
Сырые ответы API кэшируются в scripts/.cache/<location>/ (в git не идёт) — повторный прогон
работает офлайн и детерминированно.

Источники (только открытые API, без скрапинга):
  * OpenStreetMap / Overpass (ODbL) — отели, POI, береговая линия;
  * Open-Meteo Elevation API (CC BY 4.0, Copernicus DEM 90 м) — высоты отелей и пляжей;
  * OSRM demo (данные OSM, ODbL) — время и расстояние по дорогам «хаб → отель».

Что РЕАЛЬНОЕ у отеля: название, тип, координаты, адрес, звёзды (7% объектов), часть удобств,
расстояние до моря (по береговой линии), перепад высот до пляжа → «дорога в гору»,
расстояния до ближайших пляжа/аквапарка/парка/подъёмника/достопримечательности,
время трансфера по дорогам. Что СИНТЕТИЧЕСКОЕ — см. scripts/synth.py и
provenance.synthetic_fields у каждого отеля.

Совместимо с Python 3.10+, только stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from synth import enrich, haversine_m

ROOT = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "trip-agent-hackathon/0.2 (Ostrovok Hackathon 2026, team bisolid)"}
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
ELEVATION = "https://api.open-meteo.com/v1/elevation"
OSRM = "https://router.project-osrm.org/table/v1/driving/"

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
  nwr["tourism"~"^(attraction|museum|viewpoint|zoo|theme_park)$"]["name"]({bbox});
  nwr["aerialway"="station"]({bbox});
  nwr["leisure"="playground"]({bbox});
);
out center tags;
"""
COAST_QUERY = """
[out:json][timeout:180];
way["natural"="coastline"]({bbox});
out geom;
"""

# Какие POI считаем «ближайшими» для отеля (ключ → виды POI).
NEAREST_KINDS = {
    "beach": {"beach"},
    "water_park": {"water_park"},
    "park": {"park"},
    "ski_lift": {"ski_lift"},
    "playground": {"playground"},
    "sight": {"attraction", "museum", "viewpoint", "zoo"},
}
NEAREST_MAX_M = 5000
NOT_FOR_TOURISTS = re.compile(
    r"детск\w*\s+(лагер|центр|оздоров)|оздоровительн\w* лагер|\bдол\b|\bлагерь\b|общежити|служебн|ведомствен|вахт"
)
HILLY_CLIMB_M = 25  # перепад «отель − пляж», начиная с которого дорогу к морю считаем «в гору»
WALK_M_PER_MIN = 75  # ~4.5 км/ч по ровному
CLIMB_MIN_PER_10M = 1.0  # правило Нейсмита: +1 мин на каждые 10 м подъёма


# ----------------------------------------------------------------------------- сеть + кэш
class Cache:
    def __init__(self, root: Path, refresh: bool):
        self.root = root
        self.refresh = refresh
        root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, fetch):  # noqa: ANN001
        path = self.root / f"{key}.json"
        if path.exists() and not self.refresh:
            return json.loads(path.read_text("utf-8"))
        data = fetch()
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return data


def _http(url: str, data: bytes | None = None, timeout: int = 240, retries: int = 5) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — фиксированные хосты
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
            rate_limited = isinstance(e, urllib.error.HTTPError) and e.code == 429
            wait = (20 if rate_limited else 4) * (attempt + 1)
            print(f"    сеть: {e}; повтор через {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"запрос не удался: {url[:80]}… ({last})")


def overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for ep in OVERPASS:
        try:
            return _http(ep, data, retries=3)
        except RuntimeError as e:
            last = e
            print(f"[osm] {ep} недоступен, пробую следующий")
    raise RuntimeError(f"Overpass недоступен: {last}")


def elevations(points: list[tuple[float, float]], cache: Cache) -> list[float | None]:
    """Open-Meteo Elevation API, до 100 точек за запрос."""
    out: list[float | None] = []
    for i in range(0, len(points), 100):
        chunk = points[i : i + 100]
        key = "elev_" + hashlib.sha1(json.dumps(chunk).encode()).hexdigest()[:16]

        def fetch(chunk=chunk) -> dict:
            q = urllib.parse.urlencode(
                {
                    "latitude": ",".join(f"{p[0]:.5f}" for p in chunk),
                    "longitude": ",".join(f"{p[1]:.5f}" for p in chunk),
                }
            )
            time.sleep(2.0)
            return _http(f"{ELEVATION}?{q}", timeout=60)

        out += cache.get(key, fetch)["elevation"]
    return out


def osrm_matrix(hubs: list[dict], hotels: list[dict], cache: Cache, batch: int = 80) -> dict[str, dict[str, dict]]:
    """Матрица «хаб → отель» по дорогам (OSRM /table). Демо-сервер: ≤ 1 запрос/с, малые таблицы."""
    result: dict[str, dict[str, dict]] = defaultdict(dict)
    hub_coords = [f"{h['point']['lon']:.5f},{h['point']['lat']:.5f}" for h in hubs]
    for i in range(0, len(hotels), batch):
        part = hotels[i : i + batch]
        coords = hub_coords + [f"{h['point']['lon']:.5f},{h['point']['lat']:.5f}" for h in part]
        n_h = len(hub_coords)
        key = "osrm_" + hashlib.sha1(";".join(coords).encode()).hexdigest()[:16]

        def fetch(coords=coords, n_h=n_h) -> dict:
            q = urllib.parse.urlencode(
                {
                    "sources": ";".join(str(k) for k in range(n_h)),
                    "destinations": ";".join(str(k) for k in range(n_h, len(coords))),
                    "annotations": "duration,distance",
                }
            )
            time.sleep(1.1)
            return _http(f"{OSRM}{';'.join(coords)}?{q}", timeout=90)

        data = cache.get(key, fetch)
        if data.get("code") != "Ok":
            print(f"[osrm] batch {i}: {data.get('code')} {data.get('message')}")
            continue
        for hi, hub in enumerate(hubs):
            for di, h in enumerate(part):
                dur = data["durations"][hi][di]
                dist = data["distances"][hi][di]
                if dur is not None and dist is not None:
                    result[h["id"]][hub["id"]] = {"min": round(dur / 60, 1), "km": round(dist / 1000, 1)}
        print(f"[osrm] {min(i + batch, len(hotels))}/{len(hotels)}")
    return dict(result)


# ----------------------------------------------------------------------------- разбор OSM
def _center(el: dict) -> tuple[float, float] | None:
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    return None


def _norm_name(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[«»\"'`.,!()\-–—]", " ", s)
    s = re.sub(r"\b(отель|гостиница|гостевой дом|гостевой|дом|hotel|guest house|апартаменты|apartments|мини|mini)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


READABLE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def display_name(tags: dict) -> str | None:
    """Название для русскоязычного гостя: name:ru → name → name:en (берём первое на латинице/кириллице).

    За границей name часто на тайском, арабском или грузинском — такое пользователь не прочтёт и не найдёт.
    """
    for key in ("name:ru", "name", "name:en"):
        v = (tags.get(key) or "").strip()
        if v and READABLE.search(v):
            return v
    return None


def _kind(tags: dict) -> str:
    name = (display_name(tags) or "").lower()
    if "санатор" in name or tags.get("healthcare") == "sanatorium" or "пансионат" in name:
        return "sanatorium"
    if tags.get("leisure") == "resort" or "курорт" in name or "resort" in name:
        return "resort"
    t = tags.get("tourism", "hotel")
    if t == "hotel" and ("гостевой дом" in name or "гостиный двор" in name or "guest house" in name):
        return "guest_house"
    if t == "hotel" and ("апарт" in name or "apart" in name):
        return "apartment"
    if t == "hotel" and ("хостел" in name or "hostel" in name):
        return "hostel"
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


def _poi_kind(tags: dict) -> str:
    if tags.get("natural") == "beach":
        return "beach"
    if tags.get("leisure") == "water_park":
        return "water_park"
    if tags.get("leisure") == "park":
        return "park"
    if tags.get("leisure") == "playground":
        return "playground"
    if tags.get("aerialway") == "station":
        return "ski_lift"
    t = tags.get("tourism", "attraction")
    return "attraction" if t == "theme_park" else t


class Coastline:
    """Расстояние до береговой линии: проекция в локальные км и минимум по отрезкам (с грубым индексом)."""

    def __init__(self, ways: list[list[tuple[float, float]]], lat0: float, cell_km: float = 2.0):
        self.k = 111.0 * math.cos(math.radians(lat0))
        self.cell = cell_km
        self.grid: dict[tuple[int, int], list] = defaultdict(list)
        self.n = 0
        for pts in ways:
            xy = [(lon * self.k, lat * 111.0) for lat, lon in pts]
            for a, b in zip(xy, xy[1:], strict=False):
                self.n += 1
                cx, cy = int(((a[0] + b[0]) / 2) // cell_km), int(((a[1] + b[1]) / 2) // cell_km)
                self.grid[(cx, cy)].append((a, b))

    def distance_m(self, lat: float, lon: float, max_rings: int = 6) -> int | None:
        if not self.n:
            return None
        px, py = lon * self.k, lat * 111.0
        cx, cy = int(px // self.cell), int(py // self.cell)
        best = float("inf")
        for r in range(max_rings + 1):
            for gx in range(cx - r, cx + r + 1):
                for gy in range(cy - r, cy + r + 1):
                    if max(abs(gx - cx), abs(gy - cy)) != r:
                        continue
                    for (x1, y1), (x2, y2) in self.grid.get((gx, gy), ()):
                        dx, dy = x2 - x1, y2 - y1
                        den = dx * dx + dy * dy
                        t = 0.0 if den == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / den))
                        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
            if best <= r * self.cell:  # ближе уже не найдём
                break
        return int(best * 1000) if best != float("inf") else None


def _sea_distance(coast: Coastline, pt: tuple[float, float], district_kind: str) -> int | None:
    """До моря считаем у приморских районов всегда, у городских — если берег в пределах 6 км (иначе «моря нет»)."""
    if district_kind == "mountain":
        return None
    dist = coast.distance_m(*pt)
    if district_kind == "city" and (dist is None or dist > 6000):
        return None
    return dist


def nearest_district(lat: float, lon: float, districts: list[dict], max_km: float = 12.0) -> dict | None:
    best = min(districts, key=lambda d: haversine_m(lat, lon, d["center"]["lat"], d["center"]["lon"]))
    km = haversine_m(lat, lon, best["center"]["lat"], best["center"]["lon"]) / 1000
    # городской район — компактный: не приписываем ему спальные окраины в 10 км от центра
    limit = max(3.0, best["radius_km"] * 2.5) if best["kind"] == "city" else max(max_km, best["radius_km"] * 2.5)
    return best if km <= limit else None


# ----------------------------------------------------------------------------- сборка
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="sochi")
    ap.add_argument("--seed", default="osm-v1")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="перезапросить сеть, игнорируя кэш")
    ap.add_argument("--skip-osrm", action="store_true")
    ap.add_argument("--skip-elevation", action="store_true")
    ap.add_argument("--max-hotels", type=int, default=0, help="ограничить число отелей (0 — все)")
    args = ap.parse_args()

    loc_dir = ROOT / "data" / "locations" / args.location
    loc = json.loads((loc_dir / "location.json").read_text(encoding="utf-8"))
    cache = Cache(ROOT / "scripts" / ".cache" / args.location, args.refresh)
    b = loc["bbox"]
    bbox = f"{b['south']},{b['west']},{b['north']},{b['east']}"
    districts = {d["id"]: d for d in loc["districts"]}

    print("[osm] береговая линия…")
    coast_raw = cache.get("osm_coast", lambda: overpass(COAST_QUERY.format(bbox=bbox)))
    coast = Coastline([[(p["lat"], p["lon"]) for p in w.get("geometry", [])] for w in coast_raw["elements"]], loc["center"]["lat"])
    print(f"[osm] отрезков береговой линии: {coast.n}")

    print("[osm] POI…")
    praw = cache.get("osm_pois", lambda: overpass(POI_QUERY.format(bbox=bbox)))
    pois: list[dict] = []
    for el in praw["elements"]:
        tags = el.get("tags", {})
        pt = _center(el)
        if not pt:
            continue
        kind = _poi_kind(tags)
        name = display_name(tags)
        if kind not in ("beach", "playground", "ski_lift") and not name:
            continue
        d = nearest_district(pt[0], pt[1], loc["districts"])
        pois.append(
            {
                "id": f"osm-{el['type'][0]}{el['id']}",
                "name": name or {"beach": "Пляж", "playground": "Детская площадка", "ski_lift": "Станция подъёмника"}[kind],
                "kind": kind,
                "point": {"lat": round(pt[0], 6), "lon": round(pt[1], 6)},
                "district_id": d["id"] if d else None,
                "tags": ["osm"],
            }
        )

    print("[osm] отели…")
    raw = cache.get("osm_hotels", lambda: overpass(HOTEL_QUERY.format(bbox=bbox)))
    bases: list[dict] = []
    skipped: Counter[str] = Counter()
    seen: list[tuple[str, float, float]] = []
    for el in sorted(raw["elements"], key=lambda e: ({"way": 0, "relation": 1, "node": 2}[e["type"]], e["id"])):
        tags = el.get("tags", {})
        name = display_name(tags)
        pt = _center(el)
        if not name or not pt:
            skipped["no_name_or_point"] += 1
            continue
        if NOT_FOR_TOURISTS.search(name.lower()):
            skipped["not_for_tourists"] += 1  # детские лагеря, ведомственные базы — путешественнику не продаются
            continue
        d = nearest_district(pt[0], pt[1], loc["districts"])
        if d is None:
            skipped["outside_districts"] += 1
            continue
        nn = _norm_name(name)
        if any(n == nn and haversine_m(pt[0], pt[1], la, lo) < 250 for n, la, lo in seen):
            skipped["duplicate"] += 1  # один объект нанесён и точкой, и контуром
            continue
        seen.append((nn, pt[0], pt[1]))
        addr = " ".join(x for x in [tags.get("addr:street"), tags.get("addr:housenumber")] if x) or None
        bases.append(
            {
                "id": f"osm-{el['type'][0]}{el['id']}",
                "name": name.strip(),
                "kind": _kind(tags),
                "location_id": loc["id"],
                "district_id": d["id"],
                "point": {"lat": round(pt[0], 6), "lon": round(pt[1], 6)},
                "address": addr,
                "stars": _stars(tags),
                "amenities_known": _known_amenities(tags),
                "distance_to_sea_m": _sea_distance(coast, pt, d["kind"]),
                "distance_to_center_m": int(haversine_m(pt[0], pt[1], loc["center"]["lat"], loc["center"]["lon"])),
                "website": tags.get("website") or tags.get("contact:website"),
                "_pets": _pets(tags),
                "provenance": {"source": "osm", "source_id": f"{el['type']}/{el['id']}"},
            }
        )

    if args.max_hotels and len(bases) > args.max_hotels:
        # Оставляем объекты, о которых OSM знает больше (звёзды, сайт, удобства), и равномерно по районам.
        def info(b: dict) -> tuple:
            return (-(b["stars"] is not None) - (b["website"] is not None) - len(b["amenities_known"]), b["id"])

        per_d: dict[str, list[dict]] = defaultdict(list)
        for b0 in sorted(bases, key=info):
            per_d[b0["district_id"]].append(b0)
        kept: list[dict] = []
        while len(kept) < args.max_hotels and any(per_d.values()):
            for did in sorted(per_d):
                if per_d[did] and len(kept) < args.max_hotels:
                    kept.append(per_d[did].pop(0))
        skipped["over_limit"] = len(bases) - len(kept)
        bases = sorted(kept, key=lambda b0: b0["id"])

    # ---- ближайшие POI (реальные расстояния) ----
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for p in pois:
        by_kind[p["kind"]].append(p)
    for h in bases:
        la, lo = h["point"]["lat"], h["point"]["lon"]
        near: dict[str, int] = {}
        for key, kinds in NEAREST_KINDS.items():
            best = None
            for k in kinds:
                for p in by_kind.get(k, ()):
                    dist = haversine_m(la, lo, p["point"]["lat"], p["point"]["lon"])
                    if dist <= NEAREST_MAX_M and (best is None or dist < best[0]):
                        best = (dist, p)
            if best:
                near[key] = int(best[0])
                if key == "beach":
                    h["_beach_point"] = best[1]["point"]
        h["nearest_m"] = near
        if h["distance_to_sea_m"] is not None and districts[h["district_id"]]["kind"] == "city":
            # городской район: береговая линия OSM заходит в заливы и каналы (Дубай-Крик), а гостю важен ПЛЯЖ
            beach_m = near.get("beach")
            h["distance_to_sea_m"] = beach_m if beach_m is not None and beach_m <= 6000 else None

    # ---- высоты → «дорога в гору» (реально) ----
    if not args.skip_elevation:
        print("[elev] высоты отелей и пляжей…")
        hp = [(h["point"]["lat"], h["point"]["lon"]) for h in bases]
        bp = [(h["_beach_point"]["lat"], h["_beach_point"]["lon"]) for h in bases if "_beach_point" in h]
        he = elevations(hp, cache)
        be = iter(elevations(bp, cache)) if bp else iter(())
        for h, e in zip(bases, he, strict=False):
            h["elevation_m"] = None if e is None else int(round(e))
            if "_beach_point" in h:
                eb = next(be)
                if e is not None and eb is not None and h["distance_to_sea_m"] is not None:
                    # Copernicus DEM — модель поверхности (с крышами): на плоском берегу «подъём» = высота здания
                    climb = 0 if loc.get("flat_terrain") else max(0, e - max(eb, 0))
                    h["climb_to_hotel_m"] = int(round(climb))
                    h["hilly_walk"] = climb >= HILLY_CLIMB_M
                    walk = min(h["distance_to_sea_m"], h["nearest_m"].get("beach", 10**9))
                    h["beach_walk_minutes"] = max(1, round(walk / WALK_M_PER_MIN + climb / 10 * CLIMB_MIN_PER_10M))

    # ---- финальная запись отелей ----
    hotels = []
    for base in bases:
        d = districts[base["district_id"]]
        real_pets = base.pop("_pets")
        base.pop("_beach_point", None)
        if loc.get("flat_terrain") and base.get("distance_to_sea_m") is not None:
            base.setdefault("hilly_walk", False)  # плоский берег: не выдумываем «дорогу в гору»
        h = enrich(base, d, seed=args.seed, location=loc)
        if real_pets:
            h["pets"] = real_pets
            h["provenance"]["synthetic_fields"] = [f for f in h["provenance"]["synthetic_fields"] if f != "pets"]
        hotels.append(h)

    # ---- трансферы по дорогам (реально) ----
    transfers: dict = {}
    if not args.skip_osrm:
        print("[osrm] матрица хаб → отель…")
        transfers = osrm_matrix(loc["hubs"], hotels, cache)

    by_d = Counter(h["district_id"] for h in hotels)
    by_k = Counter(h["kind"] for h in hotels)
    hilly = Counter(str(h.get("hilly_walk")) for h in hotels if h["distance_to_sea_m"] is not None)
    print(f"[osm] отелей: {len(hotels)}  по районам: {dict(by_d)}")
    print(f"[osm] по типам: {dict(by_k)}  пропущено: {dict(skipped)}")
    print(f"[osm] POI: {len(pois)}  {dict(Counter(p['kind'] for p in pois))}")
    print(f"[elev] дорога к морю в гору: {dict(hilly)}")
    print(f"[osrm] отелей с трансферами: {len(transfers)}")
    if args.dry_run:
        return

    built = time.strftime("%Y-%m-%dT%H:%M:%S")
    lic = "ODbL 1.0 © OpenStreetMap contributors; высоты: Open-Meteo (CC BY 4.0); маршруты: OSRM (данные OSM, ODbL)"
    meta = {
        "source": "osm",
        "license": lic,
        "generator": "scripts/build_location.py",
        "seed": args.seed,
        "built_at": built,
        "count": len(hotels),
    }
    (loc_dir / "hotels.json").write_text(
        json.dumps({"meta": meta, "hotels": hotels}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (loc_dir / "pois.json").write_text(
        json.dumps({"meta": {**meta, "count": len(pois)}, "pois": pois}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if transfers:
        (loc_dir / "transfers.json").write_text(
            json.dumps(
                {"meta": {**meta, "count": len(transfers), "unit": {"min": "минуты без пробок", "km": "км"}}, "transfers": transfers},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"[osm] записано → {loc_dir}")


if __name__ == "__main__":
    main()
