# tab-ai-mcp (tab_mcp)

MCP-коннектор к 1С:Предприятие — компонент архитектуры **ТАБ:Библиотека искусственного интеллекта**.

## Архитектура

```
Claude / MCP-клиент
        │
        ▼
    tab_mcp  ──── OData ────► База 1С
        │
        └─── HTTP ──────────► tab_ss (хранит credentials 1С, семантический поиск)
                                  │
                                  └──────────────► tab_ca (мультиагент LLM)
                                                       │
                                                       └──► LLM провайдеры
                                                            (openai, deepseek, gigachat…)
```

- **tab_mcp** — этот сервер. MCP-коннектор к 1С через OData
- **tab_ss** — хранит подключения к 1С (URL + credentials), сервис семантического поиска
- **tab_ca** — мультиагент LLM внутри tab_ss
- **БИИ** — ТАБ:Библиотека ИИ, расширение 1С:Предприятие

## Как это работает

1. Администратор сохраняет подключение к базе 1С в tab_ss один раз (`POST /v1/onec/connections`)
2. ЛЛМ вызывает `read_1c` или `write_1c` с параметрами `organization` и `user_id`
3. tab_mcp автоматически получает credentials из tab_ss и выполняет OData-запрос
4. Логин/пароль 1С **не передаётся в каждом запросе** — хранится в tab_ss, кешируется 5 минут

## Установка

```bash
# Рекомендуется: uvx (не требует установки Python окружения)
uvx tab-ai-mcp

# Или через pip
pip install tab-ai-mcp
tab-ai-mcp
```

> Для `uvx` нужен [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Деплой (Railway / Docker)

```bash
docker run -e MCP_TRANSPORT=streamable-http \
           -e TAB_SS_URL=https://your-tab-ss.up.railway.app \
           -e TAB_SS_API_KEY=your-key \
           -p 8001:8001 \
           ghcr.io/dziden/tab-ai-mcp
```

Или через Railway — подключи репозиторий, Railway подхватит `Dockerfile` автоматически.

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `TAB_SS_URL` | URL сервиса tab_ss | облако Railway |
| `TAB_SS_API_KEY` | Ключ доступа к tab_ss (`X-Admin-Key`) | встроенный ключ |
| `MCP_TRANSPORT` | `stdio` (Claude Desktop) или `streamable-http` (сервер) | `stdio` |
| `MCP_HOST` | Хост для HTTP транспорта | `0.0.0.0` |
| `PORT` / `MCP_PORT` | Порт для HTTP транспорта (Railway задаёт `PORT` автоматически) | `8001` |
| `LOG_API_KEY` | Ключ для `/logs` эндпоинта (по умолчанию = `TAB_SS_API_KEY`) | — |
| `ONEC_ORGANIZATION` | Организация для индексации метаданных OData в tab_ss | — |
| `ONEC_BASE_URL` | Fallback URL 1С для локальной разработки без tab_ss | — |
| `ONEC_USERNAME` | Fallback логин 1С для локальной разработки | — |
| `ONEC_PASSWORD` | Fallback пароль 1С для локальной разработки | — |

> **Обратная совместимость:** принимаются также `TAB_AI_BASE_URL` и `TAB_AI_API_KEY`.

## Настройка подключения к 1С

Перед использованием нужно сохранить подключение к базе 1С в tab_ss:

```http
POST /v1/onec/connections
X-Admin-Key: your-key
Content-Type: application/json

{
  "organization": "my-org",
  "user_id": "",
  "odata_base_url": "http://1c-server/base",
  "username": "login",
  "password": "password"
}
```

- `user_id=""` — общий профиль для всей организации
- Разные `user_id` — разные базы 1С для разных пользователей в одной организации

## Настройка Claude Desktop (локальный режим)

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "1c": {
      "command": "uvx",
      "args": ["tab-ai-mcp"],
      "env": {
        "TAB_SS_URL": "https://your-tab-ss.up.railway.app",
        "TAB_SS_API_KEY": "your-key",
        "ONEC_ORGANIZATION": "my-org"
      }
    }
  }
}
```

## Инструменты (MCP Tools)

### `read_1c` — чтение данных из 1С

```
read_1c(organization, query, user_id?, filter?, select?, expand?, top?, skip?)
```

| Параметр | Описание |
|---|---|
| `organization` | Код организации (ключ в tab_ss). Обязателен. |
| `query` | Что читать: точное OData-имя, виртуальная таблица или описание на русском |
| `user_id` | ID пользователя для изоляции подключений (по умолчанию "") |
| `filter` | OData `$filter` выражение |
| `select` | Поля через запятую |
| `expand` | OData `$expand` для вложенных объектов |
| `top` | Количество записей (по умолчанию 100) |
| `skip` | Сдвиг для пагинации |

Варианты `query`:
- `"Catalog_Номенклатура"` — точное OData-имя
- `"AccountingRegister_Хозрасчетный/Balance(Period=datetime'2025-12-31T00:00:00')"` — виртуальная таблица
- `"остатки товаров на складах"` — описание, резолвится через семантический поиск

### `write_1c` — запись данных в 1С (upsert)

```
write_1c(organization, query, data, user_id?, model?)
```

- Если в `data` есть `Ref_Key` → PATCH (обновление)
- Если нет `Ref_Key` → POST (создание)

### `count_document_marks` — подсчёт печатей и подписей

```
count_document_marks(document_base64)
```

Определяет количество печатей и подписей на скане или ЭДО-документе, атрибутирует каждую контрагенту.

## Примеры запросов

### Остаток по банку на конец 2025

```
# Шаг 1: получить Ref_Key для каждого счёта (ChartOfAccounts не поддерживает 'or')
read_1c("my-org", "ChartOfAccounts_Хозрасчетный", filter="Code eq '51'")
read_1c("my-org", "ChartOfAccounts_Хозрасчетный", filter="Code eq '52'")
read_1c("my-org", "ChartOfAccounts_Хозрасчетный", filter="Code eq '55'")

# Шаг 2: запросить остатки через виртуальную таблицу Balance
read_1c("my-org",
  "AccountingRegister_Хозрасчетный/Balance(Period=datetime'2025-12-31T00:00:00')",
  filter="Account_Key eq guid'<ref_key>'")
# Поле СуммаBalance = рублёвый остаток, ВалютнаяСуммаBalance = валютный
```

### Поиск контрагента

```
read_1c("my-org", "Catalog_Контрагенты", filter="contains(Наименование,'Ромашка')")
```

### Создать документ

```
write_1c("my-org", "Document_СчетПокупателю", {
  "Дата": "2025-12-31T00:00:00",
  "Контрагент_Key": "guid'...'",
  ...
})
```

## Мониторинг

При `MCP_TRANSPORT=streamable-http` доступен эндпоинт `/logs`:

```
GET /logs?api_key=your-key&last=50
```

Возвращает последние N запросов с временем выполнения, параметрами и ошибками.

## Требования к 1С

Опубликованный OData-сервис: Конфигуратор → Администрирование → Публикация на веб-сервере → "Опубликовать стандартный интерфейс OData"

## Лицензия

MIT
