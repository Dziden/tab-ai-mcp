# tab-ai-mcp (tab_mcp)

MCP-коннектор к 1С:Предприятие — компонент архитектуры **ТАБ:Библиотека искусственного интеллекта**.

## Архитектура

```
Claude / MCP-клиент
        │
        ▼
    tab_mcp  ──── OData ────► База 1С (+ БИИ)
        │
        └─── HTTP ──────────► tab_ss (семантический поиск, прогнозирование, аномалии)
                                  │
                                  └──────────────► tab_ca (мультиагент LLM)
                                                       │
                                                       └──► LLM провайдеры
                                                            (openai, deepseek, gigachat…)
```

- **tab_mcp** — этот сервер. MCP-коннектор к 1С и к tab_ss
- **tab_ss** — сервис семантического поиска и аналитики (on-prem или облако)
- **tab_ca** — мультиагент LLM, внутри tab_ss
- **БИИ** — ТАБ:Библиотека ИИ, расширение 1С:Предприятие

## Возможности

- **CRUD объектов 1С** — чтение, создание, изменение, удаление через OData REST API
- **Семантический поиск** — найти "молоко пастеризованное" без точного совпадения строк (через tab_ss)
- **Обогащение объектов** — максимально полный объект по стандарту EnterpriseData (XDTO, 822 типа)
- **Прогнозирование** — динамика продаж и других временных рядов (через tab_ss)
- **Проверка документов** — наличие подписи и печати на сканах (через tab_ss)

## Установка

```bash
# Рекомендуется: uvx (не требует установки Python окружения)
uvx tab-ai-mcp

# Или через pip
pip install tab-ai-mcp
tab-ai-mcp
```

> Для `uvx` нужен [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Настройка Claude Desktop

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "1c": {
      "command": "uvx",
      "args": ["tab-ai-mcp"],
      "env": {
        "ONEC_BASE_URL": "http://ваш-сервер/имя-базы",
        "ONEC_USERNAME": "ваш-логин",
        "ONEC_PASSWORD": "ваш-пароль",
        "TAB_SS_API_KEY": "ваш-ключ-tab_ss"
      }
    }
  }
}
```

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `ONEC_BASE_URL` | URL базы 1С, например `http://server/myapp` | — (обязательно) |
| `ONEC_USERNAME` | Логин пользователя 1С | — |
| `ONEC_PASSWORD` | Пароль пользователя 1С | — |
| `TAB_SS_URL` | URL сервиса tab_ss (on-prem или облако) | облако Railway |
| `TAB_SS_API_KEY` | Ключ доступа к tab_ss | встроенный ключ |

> **Обратная совместимость:** принимаются также старые имена `TAB_AI_BASE_URL` и `TAB_AI_API_KEY`.

> **Требование к 1С:** опубликованный OData сервис.
> Конфигуратор → Администрирование → Публикация на веб-сервере → "Опубликовать стандартный интерфейс OData"

## Инструменты (MCP Tools)

### OData → База 1С

| Инструмент | Описание |
|---|---|
| `list_1c_types()` | Список всех типов объектов базы (каталоги, документы, регистры) |
| `query_1c(entity_type, filter, select, top, skip)` | OData запрос с фильтрами |
| `get_1c_object(entity_type, ref_id)` | Объект по GUID с XDTO обогащением |
| `create_1c_object(entity_type, data)` | Создать объект |
| `update_1c_object(entity_type, ref_id, data)` | Обновить объект |
| `delete_1c_object(entity_type, ref_id)` | Удалить объект |
| `get_xdto_schema(type_name)` | Схема полей типа по стандарту EnterpriseData |
| `list_xdto_covered_types()` | Типы, покрытые XDTO обогащением |

### tab_ss — семантический поиск и аналитика

| Инструмент | Описание |
|---|---|
| `sync_1c_to_tab_ss(entity_type, organization, object_type_label)` | Синхронизировать данные 1С → tab_ss |
| `semantic_search_1c(organization, object_type_label, search_text)` | Поиск по смыслу |
| `semantic_search_multi_property(organization, object_type_label, search_properties)` | Поиск по нескольким свойствам |
| `set_tab_ss_provider_token(provider, token)` | Настроить токен LLM провайдера в tab_ss |
| `forecast_1c_data(table_data, months_count)` | Прогноз временных рядов |
| `verify_document_signature(document_base64)` | Проверка подписи и печати |

## Примеры

### "Создай счёт на оплату на молоко"

Claude выполнит автоматически:
1. `sync_1c_to_tab_ss("Catalog_Номенклатура", "org1", "Номенклатура")` — индексирует справочник
2. `semantic_search_1c("org1", "Номенклатура", "молоко")` → находит GUID товара
3. `get_xdto_schema("Document_СчетПокупателю")` → узнаёт обязательные поля
4. `create_1c_object("Document_СчетПокупателю", {...})` → создаёт документ

### "Покажи данные контрагента Ромашка"

```
query_1c("Catalog_Контрагенты", filter="contains(Наименование,'Ромашка')")
→ получаем Ref_Key
get_1c_object("Catalog_Контрагенты", ref_id)
→ полный объект со всеми полями EnterpriseData
```

### OData фильтры

```
"Наименование eq 'Молоко'"
"contains(Наименование,'Мол')"
"ДатаДокумента ge datetime'2024-01-01T00:00:00'"
"Контрагент_Key eq guid'12345678-1234-1234-1234-123456789012'"
```

## XDTO обогащение

При вызове `get_1c_object` сервер:
1. Получает объект из 1С через OData (только заполненные поля)
2. Ищет тип в схеме EnterpriseData (822 типа)
3. Добавляет все стандартные поля с `null` для незаполненных
4. Возвращает объект с `_xdto_schema` — описанием всех полей

Например: OData вернул 5 полей → после обогащения объект содержит 43 поля.

## Связь с ТАБ:БИИ

`tab_mcp` — часть экосистемы [ТАБ:Библиотека искусственного интеллекта](https://tab-ai.ru).
Семантический поиск реализован через тот же backend (`tab_ss`), что и функции
`НайтиПоСмыслуОбъектСУказаннымиСвойствамиАсинх` и `СпрогнозируйДинамикуИзмененияДанных`
внутри самой библиотеки 1С.

## Лицензия

MIT
