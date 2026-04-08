# Руководство администратора tab-ai-mcp

## Содержание

1. [Деплой на Railway](#деплой-на-railway)
2. [Подключение tab_ss к tab-ai-mcp](#подключение-tab_ss-к-tab-ai-mcp)
3. [Настройка подключений к 1С](#настройка-подключений-к-1с)
4. [Переменные окружения](#переменные-окружения)
5. [Мониторинг логов](#мониторинг-логов)
6. [Проверка работоспособности](#проверка-работоспособности)

---

## Деплой на Railway

1. Подключи репозиторий `tab-ai-mcp` в Railway — Dockerfile подхватится автоматически.
2. Задай переменные окружения (см. раздел ниже).
3. Деплой запустится автоматически после пуша.

После деплоя Railway автоматически назначает переменную `PORT` — сервис слушает на этом порту.

Чтобы посмотреть присвоенный порт:
**Railway Dashboard → сервис tab-ai-mcp → Variables → PORT**

---

## Подключение tab_ss к tab-ai-mcp

### Внутренняя сеть Railway (рекомендуется)

Если tab_ss и tab-ai-mcp в **одном Railway-проекте**:

1. Включить приватную сеть:
   **Railway Dashboard → сервис tab-ai-mcp → Settings → Networking → Private Network → Enable**

2. URL для tab_ss:
   ```
   http://tab-ai-mcp.railway.internal:<PORT>/mcp
   ```
   Где `<PORT>` — значение переменной `PORT` сервиса tab-ai-mcp (смотри в Variables).

   > ⚠ Обязательно указывать:
   > - **порт** (без него обращение идёт на порт 80, сервис не слышит)
   > - **путь `/mcp`** (FastMCP streamable-http слушает именно на этом пути)

   Пример: `http://tab-ai-mcp.railway.internal:8080/mcp`

   > Railway автоматически назначает `PORT=8080` — именно этот порт нужно указывать в URL.

### Публичный домен (для тестирования)

1. Сгенерировать домен:
   **Railway Dashboard → сервис tab-ai-mcp → Settings → Networking → Generate Domain**

2. URL для tab_ss:
   ```
   https://tab-ai-mcp-xxx.up.railway.app/mcp
   ```

---

## Настройка подключений к 1С

Перед первым использованием нужно сохранить credentials 1С в tab_ss.

### Создать подключение

```http
POST https://<tab_ss_url>/v1/onec/connections
X-Admin-Key: <admin_key>
Content-Type: application/json

{
  "organization": "my-org",
  "user_id": "",
  "odata_base_url": "http://1c-server/base-name",
  "username": "login",
  "password": "password"
}
```

**Поля:**
- `organization` — произвольный идентификатор организации. Именно его ЛЛМ передаёт в параметре `organization` при вызове MCP-инструментов.
- `user_id` — оставить `""` для общего профиля. Если нужна изоляция по пользователям — передавать ID пользователя.
- `odata_base_url` — URL опубликованной базы 1С, например `http://192.168.1.10/accounting`.
- `username` / `password` — логин и пароль пользователя 1С.

### Как опубликовать OData в 1С

Конфигуратор → Администрирование → Публикация на веб-сервере → включить **«Публиковать стандартный интерфейс OData»**

### Проверить подключение

```http
GET https://<tab_ss_url>/v1/onec/connections?organization=my-org
X-Admin-Key: <admin_key>
```

---

## Переменные окружения

Задаются в Railway Dashboard → сервис tab-ai-mcp → Variables.

| Переменная | Обязательная | Описание |
|---|---|---|
| `MCP_TRANSPORT` | Да | Установить `streamable-http` для сетевого режима |
| `TAB_SS_URL` | Да | URL сервиса tab_ss, например `https://tab-ss.up.railway.app` |
| `TAB_SS_API_KEY` | Да | Admin-ключ tab_ss (`X-Admin-Key`) |
| `ONEC_ORGANIZATION` | Нет | Код организации для индексации метаданных 1С в tab_ss при старте |
| `LOG_API_KEY` | Нет | Ключ для эндпоинта `/logs` (по умолчанию совпадает с `TAB_SS_API_KEY`) |
| `MCP_HOST` | Нет | Хост для HTTP (по умолчанию `0.0.0.0`) |
| `PORT` / `MCP_PORT` | Нет | Railway задаёт `PORT` автоматически |

**Для локальной разработки** (без tab_ss) дополнительно:

| Переменная | Описание |
|---|---|
| `ONEC_BASE_URL` | URL базы 1С напрямую, например `http://localhost/base` |
| `ONEC_USERNAME` | Логин 1С |
| `ONEC_PASSWORD` | Пароль 1С |

---

## Мониторинг логов

Все вызовы MCP-инструментов (`read_1c`, `write_1c`) пишутся в память (до 2000 записей).

```
GET https://<tab-ai-mcp-domain>/logs?api_key=<LOG_API_KEY>&last=50
```

Параметры:
- `api_key` — ключ (`LOG_API_KEY` или `TAB_SS_API_KEY`)
- `last` — количество последних записей (по умолчанию все)

Пример ответа:
```json
{
  "count": 3,
  "total": 3,
  "logs": [
    {
      "ts": "2025-04-08T10:23:45Z",
      "tool": "read_1c",
      "entity_type": "остатки по банку",
      "resolved": "AccountingRegister_Хозрасчетный_RecordType",
      "params": {"org": "my-org", "top": 100},
      "duration_ms": 342.1,
      "rows": 15,
      "error": null
    }
  ]
}
```

> ⚠ Логи хранятся в памяти — сбрасываются при перезапуске контейнера.

---

## Проверка работоспособности

### 1. Проверить что сервер отвечает

```bash
curl https://<tab-ai-mcp-domain>/logs?api_key=<key>
# Ожидается: {"count":0,"total":0,"logs":[]}
```

### 2. Проверить подключение к 1С

Сделай любой запрос через tab_ss к 1С (например «покажи список контрагентов»), затем проверь логи:

```bash
curl "https://<tab-ai-mcp-domain>/logs?api_key=<key>&last=5"
```

Если в логах появилась запись — всё работает. Если `error` не null — смотри описание ошибки.

### 3. Частые проблемы

| Проблема | Причина | Решение |
|---|---|---|
| `All connection attempts failed` | Неверный URL или порт в tab_ss | Проверить URL: `http://tab-ai-mcp.railway.internal:<PORT>/mcp` |
| `HTTP 401 от 1С OData` | Неверный логин/пароль в подключении | Обновить credentials через `POST /v1/onec/connections` |
| `HTTP 404 от 1С OData` | Неверный `odata_base_url` или OData не опубликован | Проверить публикацию OData в конфигураторе 1С |
| Логи пустые после запроса | Запрос прошёл, но инструмент не вызывался | Убедиться что ЛЛМ в tab_ss реально вызывает MCP-инструменты |
