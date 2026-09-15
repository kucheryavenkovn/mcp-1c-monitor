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
docker build -t mcp-1c-monitor:latest .
docker run -d --name mcp_1c_monitor --restart unless-stopped `
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

## Состав

- `app.py` — весь сервис (API + страница в одном файле)
- `Dockerfile`, `requirements.txt`

## Лицензия

MIT
