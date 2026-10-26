# Location Pack: Большой Сочи

| Файл | Что внутри | Источник |
|---|---|---|
| `location.json` | районы (7), хабы прибытия, сезонность цен, праздники, климат по месяцам | ручная разметка + **Open-Meteo** (климат, реальный) |
| `hotels.json` | отели | сейчас **фикстуры** (вымышленные, `provenance.source=fixture`); после `scripts/build_location.py` — **OpenStreetMap** |
| `pois.json` | пляжи, парки, подъёмники, достопримечательности | фикстуры → OSM |

## Что реальное, что синтетическое

- **Реальное:** климат (Open-Meteo, средние 2019–2025: воздух днём/ночью, море, дни с осадками);
  после сборки из OSM — название, тип, координаты, адрес, звёзды и часть удобств отелей.
- **Синтетическое (DEMO):** цены, наличие, типы номеров, рейтинги, правила для животных, ранний
  заезд. Генерируется детерминированно (seed), список полей — в `provenance.synthetic_fields`.
- Координаты хабов и центров районов — приблизительные (ручная разметка); уточнить по OSM.

## Пересобрать

```bash
cd scripts
python build_climate.py --location sochi          # климат (Open-Meteo, бесплатно, есть лимиты — скрипт повторяет)
python build_location.py --location sochi         # отели и POI из OSM (Overpass)
python gen_fixtures.py --location sochi           # вернуть фикстуры (если нужно)
```

## Лицензии

- © OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright
- Open-Meteo, CC BY 4.0 — https://open-meteo.com/
