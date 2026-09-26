# Nominatim

Локальный геокодер для муниципальной платформы.

## Запуск

```bash
docker compose -f nominatim/stack.yml up -d --build
```

Первый запуск скачивает OSM PBF и строит поисковую базу. Это длительная операция:
для MVP используется extract Приволжского федерального округа, потому что у Geofabrik
нет отдельного стандартного extract только для Нижегородской области.

После импорта API будет доступен:

- внутри Docker-сети: `http://nominatim:8080`
- с хоста: `http://localhost:8081`

## Данные

По умолчанию используется:

```text
https://download.geofabrik.de/russia/volga-fed-district-latest.osm.pbf
```

Если позже нужен строго областной extract, подготовьте `.osm.pbf` через `osmium`
по polygon/geojson границе Нижегородской области и передайте `NOMINATIM_PBF_URL`
или замените stack на `PBF_PATH`.

## Постоянное хранение

Данные импорта лежат в Docker volumes:

- `municipal_low_code_nominatim_data`
- `municipal_low_code_nominatim_flatnode`

Поэтому пересборка контейнера не удаляет импортированную базу.
