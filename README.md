# 1C MCP Fleet Monitor

Веб-мониторинг флота MCP-серверов для разработки с ИИ на 1С
([документация серверов](https://docs.onerpa.ru/mcp-servery-1c)).
Один контейнер: опрашивает Docker и MCP-порты, показывает готовность,
прогресс индексации из логов, отвечает за лицензии и даёт кнопки управления.

![стек](https://img.shields.io/badge/python-3.11-blue)
![Flask](https://img.shields.io/badge/Flask-3.1-green)

## Что показывает

Карточка на каждый сервер (HelpSearch, GraphMetadata, CodeMetadata, SSLSearch,
TemplatesSearch, SyntaxCheck, 1CCodeChecker + Neo4j), обновление каждые 5 с:

- бейдж состояния: `ready` / `indexing` (прогресс-бар %) / `starting` /
  `stopped` / `error` / `missing`
- контейнер: статус, Docker health, uptime, счётчик рестартов
- MCP-зонд: настоящий `initialize` + `tools/list` по Streamable HTTP,
  задержка, список инструментов
- лицензия: `ok` / `BAD` (плохой ключ убивает контейнер за секунды,
  так что живой MCP = ключ принят)
- последняя строка прогресса индексации из логов
- кнопки **start / stop / restart**, просмотр логов в браузере

## Быстрый старт

```powershell
# 1. Заполните env-файл ВНЕ репозитория (ключи берутся только оттуда!)
copy .env.monitor.example $env:USERPROFILE\.mcp-secrets\monitor.env

# 2. Монитору нужен доступ к GPU для VRAM-панели — запускайте с --gpus all
docker build -t mcp-1c-monitor:latest .
docker run -d --name mcp_1c_monitor --restart unless-stopped `
  --gpus all --env-file "$env:USERPROFILE\.mcp-secrets\monitor.env" `
  -p 127.0.0.1:8090:8090 `
  -v /var/run/docker.sock:/var/run/docker.sock `
  mcp-1c-monitor:latest
```

Открыть: http://localhost:8090

> На Linux добавьте `--add-host=host.docker.internal:host-gateway`
> (на Docker Desktop это имя резолвится само).

## API

| Метод | Путь | Что делает |
|-------|------|------------|
| `GET` | `/` | веб-интерфейс |
| `GET` | `/api/status` | JSON по всем серверам |
| `GET` | `/api/logs/<container>?tail=80` | хвост логов контейнера |
| `POST` | `/api/control/<container>/<start\|stop\|restart>` | управление контейнером |

## Как определяется готовность

В этих сборках MCP-образов **нет** эндпоинтов `/health`, `/ready`, `/live`
из документации (отвечают `404`), поэтому монитор использует то, что реально есть:

1. **Docker API** — статус, healthcheck, рестарты, uptime, логи
2. **MCP Streamable HTTP** — `POST /mcp` с `initialize`, затем `tools/list`
3. **Парсинг логов** — `code progress: N/M` → процент, `Invalid LICENSE_KEY` → авария

## Видеокарта и переключение GPU/CPU

Панель «Видеокарта»: занято/свободно VRAM (NVML), топ процессов.
Сценарий «проиндексировал на GPU → освободил карту под LLM»:

1. Начальная индексация идёт на `latest`-образах с `--gpus all` (быстро).
2. Когда надо поднять локальную LLM на всю VRAM — на карточке каждого
   сервера жмёте **«на CPU»**: монитор пересоздаёт контейнер без GPU.
   Индексы живут в томах и сохраняются, переиндексация не нужна —
   на CPU просто медленнее каждый отдельный запрос к эмбеддингам.
3. Кнопка **«на GPU»** возвращает ускорение тем же пересозданием.

Переключаются 4 сервера со встроенной моделью: Help, SSL, Templates,
CodeMetadata. SyntaxCheck (без модели), 1CCodeChecker (облачный API) и
Graph в `graph_only` GPU не используют — кнопок у них нет.

Тюнинг индексации CodeMetadata (32 CPU + RTX 4090, замерено: 21.7 →
1400+ файлов/мин): `PARSE_WORKERS=16`, `EMBEDDING_CONCURRENCY=10`,
`EMBED_BATCH_SIZE_LOCAL=128`, `SUB_INDEX_WORKERS=8`. Имена взяты из
`/app/src/main.py` образа. Перезапуск докачивает инкрементально
(`file_tracker`: unchanged пропускаются), полный перестрой — только при
смене модели или `RESET_DATABASE=true`.

## Панель «Данные» (проекты внутри индексов)

Кнопка **«данные»** на карточках показывает, что лежит внутри баз серверов:

- CodeMetadata — инструмент `stats`: счётчики коллекций, модель эмбеддингов,
  фаза индексации; кнопка **переиндексировать** (`reindex`, в фоне)
- Graph-beta — `get_graph_stats` (узлы/связи по видам), `list_graph_projects`
  (проекты, поколения, ошибки), `get_indexing_status` (фоновые задачи);
  кнопки **перечитать слои расширений** (`refresh_extension_layers`, долгая)
  и **удалить граф-проект** (`delete_graph_project`, с подтверждением)
- У всех индексных серверов — размер данных на диске (`du` внутри контейнера)

Удаление — только граф-проект целиком; очистка векторных индексов
делается пересозданием контейнера с `RESET_DATABASE=true` (вне монитора).

## Состав

- `app.py` — весь сервис (API + страница в одном файле)
- `Dockerfile`, `requirements.txt`
- `.env.monitor.example` — шаблон env (без секретов)

## Лицензия

MIT
