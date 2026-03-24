"""
tab_mcp — MCP-коннектор к 1С:Предприятие
=========================================
Компонент архитектуры ТАБ:БИИ. Подключает Claude и другие MCP-клиенты к базе 1С.

Архитектура:
  tab_mcp → База 1С        (OData REST API — CRUD объектов)
  tab_mcp → tab_ss         (семантический поиск, прогнозирование, аномалии)
  tab_ss  → tab_ca         (мультиагент LLM, внутри tab_ss)
  tab_ss  → LLM провайдеры (openai, deepseek, gigachat и др., через tab_ca)

Установка:
  pip install tab-ai-mcp
  # или (рекомендуется)
  uvx tab-ai-mcp

Конфигурация (переменные окружения):
  ONEC_BASE_URL  — URL базы 1С, например http://server/myapp
  ONEC_USERNAME  — Логин 1С
  ONEC_PASSWORD  — Пароль 1С
  TAB_SS_URL     — URL сервиса tab_ss (по умолчанию облачный Railway)
  TAB_SS_API_KEY — Ключ доступа к tab_ss
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from tab_ai_mcp import odata_client as oc
from tab_ai_mcp import xdto_enricher as xe

# ── tab_ss Configuration ───────────────────────────────────────────────────────
# tab_ss — сервис семантического поиска, прогнозирования и аномалий.
# Может работать on-prem (с GPU) или в облаке (Railway).

_TAB_SS_DEFAULT_KEY = "a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
_TAB_SS_DEFAULT_URL = "https://test-docker-2-production.up.railway.app"

# Поддержка старых имён переменных для обратной совместимости
TAB_SS_KEY = (
    os.environ.get("TAB_SS_API_KEY")
    or os.environ.get("TAB_AI_API_KEY")
    or _TAB_SS_DEFAULT_KEY
)
TAB_SS_URL = (
    os.environ.get("TAB_SS_URL")
    or os.environ.get("TAB_AI_BASE_URL")
    or _TAB_SS_DEFAULT_URL
).rstrip("/")


def _tab_ss_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=TAB_SS_URL,
        headers={"X-Admin-Key": TAB_SS_KEY, "Content-Type": "application/json"},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


def _tab_ss_handle(response: httpx.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        raise RuntimeError(f"HTTP {exc.response.status_code} от tab_ss: {detail}") from exc
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


# ── FastMCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="tab-mcp",
    instructions=(
        "MCP-коннектор к 1С:Предприятие (компонент tab_mcp архитектуры ТАБ:БИИ).\n\n"
        "Два канала работы:\n"
        "1. OData → База 1С: чтение/запись объектов напрямую\n"
        "2. tab_ss: семантический поиск, прогнозирование, аномалии\n\n"
        "Типичные сценарии:\n"
        "- Найти объект по смыслу: sync_1c_to_tab_ss → semantic_search_1c → get_1c_object\n"
        "- Создать документ: get_xdto_schema → create_1c_object\n"
        "- Запросить данные: query_1c с OData $filter\n\n"
        "Имена OData типов: Catalog_*, Document_* (полный список — list_1c_types)"
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# 1С OData ИНСТРУМЕНТЫ
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_1c_types() -> dict[str, Any]:
    """
    Получить список всех типов объектов базы 1С.
    Get list of all object types in the 1C database.

    Обращается к $metadata OData и возвращает категоризированный список:
    справочники (Catalog_*), документы (Document_*), регистры и другие.

    Returns:
        Словарь с ключами catalogs, documents, registers, other, total.
    """
    return await oc.get_metadata()


@mcp.tool()
async def query_1c(
    entity_type: str,
    filter: Optional[str] = None,
    select: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """
    Запросить список объектов из 1С через OData.
    Query 1C objects via OData.

    Args:
        entity_type: OData тип объекта, например "Catalog_Номенклатура" или "Document_СчетПокупателю".
                     OData entity type name.
        filter:      OData $filter выражение для фильтрации.
                     Примеры:
                       "Наименование eq 'Молоко'"
                       "startswith(Наименование,'Мол')"
                       "ДатаДокумента ge datetime'2024-01-01T00:00:00'"
                       "Контрагент_Key eq guid'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'"
        select:      Список полей через запятую, например "Ref_Key,Наименование,Цена".
                     Если не указан — возвращаются все поля.
        top:         Максимальное количество записей (по умолчанию 20).
        skip:        Количество записей для пропуска (для пагинации).

    Returns:
        Список объектов в формате JSON.
    """
    return await oc.query(entity_type, filter=filter, select=select, top=top, skip=skip)


@mcp.tool()
async def get_1c_object(entity_type: str, ref_id: str) -> dict[str, Any]:
    """
    Получить объект 1С по GUID с обогащением по схеме XDTO EnterpriseData.
    Get a 1C object by GUID, enriched with XDTO EnterpriseData schema.

    Возвращает объект с ВСЕМИ полями из стандартной схемы EnterpriseData:
    - Заполненные поля из 1С с реальными значениями
    - Незаполненные стандартные поля с null
    - Метаинформацию о схеме в ключе _xdto_schema

    Args:
        entity_type: OData тип объекта, например "Catalog_Контрагенты".
        ref_id:      GUID объекта (Ref_Key), например "12345678-1234-1234-1234-123456789012".

    Returns:
        Полный объект с XDTO обогащением и схемой.
    """
    obj = await oc.get_one(entity_type, ref_id)
    # Обогатить по XDTO схеме
    xdto_type = xe.find_xdto_type(entity_type)
    if xdto_type:
        obj = xe.enrich_object(obj, xdto_type)
    return obj


@mcp.tool()
async def create_1c_object(
    entity_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Создать новый объект в 1С через OData.
    Create a new 1C object via OData.

    Перед созданием рекомендуется вызвать get_xdto_schema(entity_type)
    чтобы узнать обязательные поля для данного типа объекта.

    Args:
        entity_type: OData тип объекта, например "Document_СчетПокупателю".
        data:        Словарь с полями создаваемого объекта.
                     Пример для счёта:
                     {
                       "Дата": "2024-03-23T00:00:00",
                       "Контрагент_Key": "guid-контрагента",
                       "Организация_Key": "guid-организации",
                       "Товары": [{"НоменклатураRef_Key": "guid", "Количество": 5, "Цена": 100}]
                     }

    Returns:
        Созданный объект с присвоенным GUID (Ref_Key).
    """
    # Проверить обязательные поля из XDTO схемы
    xdto_type = xe.find_xdto_type(entity_type)
    missing_required = []
    if xdto_type:
        fields = xe.get_xdto_fields(xdto_type)
        missing_required = [
            k for k, v in fields.items()
            if v.get("required") and not k.startswith("@") and k not in data
            and k not in ("Ref_Key", "DataVersion", "Ссылка")
        ]

    result = await oc.create(entity_type, data)

    if missing_required:
        result["_xdto_warning"] = (
            f"Объект создан, но не заполнены обязательные поля по схеме EnterpriseData: "
            f"{', '.join(missing_required[:10])}"
        )
    return result


@mcp.tool()
async def update_1c_object(
    entity_type: str,
    ref_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """
    Обновить существующий объект 1С (частичное обновление).
    Partially update an existing 1C object via OData PATCH.

    Args:
        entity_type: OData тип объекта, например "Catalog_Контрагенты".
        ref_id:      GUID объекта для обновления.
        data:        Словарь с полями для изменения (только изменяемые поля).
                     Неуказанные поля остаются без изменений.

    Returns:
        Подтверждение обновления или обновлённый объект.
    """
    return await oc.update(entity_type, ref_id, data)


@mcp.tool()
async def delete_1c_object(entity_type: str, ref_id: str) -> dict[str, Any]:
    """
    Удалить объект из 1С.
    Delete an object from 1C via OData.

    ВНИМАНИЕ: Операция необратима. Убедитесь, что объект не имеет зависимостей.

    Args:
        entity_type: OData тип объекта.
        ref_id:      GUID объекта для удаления.

    Returns:
        Подтверждение удаления.
    """
    return await oc.delete(entity_type, ref_id)


@mcp.tool()
async def get_xdto_schema(type_name: str) -> dict[str, Any]:
    """
    Получить схему полей объекта из стандарта EnterpriseData (XDTO).
    Get field schema for a 1C object type from the EnterpriseData standard.

    Используйте перед созданием объекта, чтобы знать какие поля обязательны,
    какие типы данных ожидаются, и какова полная структура объекта.

    Args:
        type_name: Имя типа — можно передать как OData имя ("Catalog_Номенклатура")
                   или XDTO имя ("Справочник.Номенклатура").

    Returns:
        Словарь со схемой: обязательные поля, необязательные поля, типы данных.
        Пример:
        {
          "type": "Справочник.Номенклатура",
          "required_fields": {"Наименование": {"type": "Строка", ...}, ...},
          "optional_fields": {"Артикул": {"type": "Строка", ...}, ...}
        }
    """
    # Попробовать найти через OData маппинг
    xdto_type = xe.find_xdto_type(type_name)
    if not xdto_type:
        # Может быть уже XDTO имя
        xdto_type = type_name
    return xe.get_xdto_schema_info(xdto_type)


@mcp.tool()
async def list_xdto_covered_types() -> dict[str, Any]:
    """
    Список типов 1С, покрытых схемой EnterpriseData (XDTO).
    List 1C object types covered by the EnterpriseData schema.

    Типы из этого списка поддерживают обогащение объектов — get_1c_object
    вернёт полную структуру по стандарту 1С EnterpriseData.

    Returns:
        Список XDTO типов с разбивкой по категориям.
    """
    all_types = xe.list_covered_types()
    catalogs = [t for t in all_types if t.startswith("Справочник.")]
    documents = [t for t in all_types if t.startswith("Документ.")]
    other = [t for t in all_types if not t.startswith(("Справочник.", "Документ."))]
    return {
        "catalogs": catalogs,
        "documents": documents,
        "other": other,
        "total": len(all_types),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAB_SS — СЕМАНТИЧЕСКИЙ ПОИСК И АНАЛИТИКА
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def sync_1c_to_tab_ss(
    entity_type: str,
    organization: str,
    object_type_label: str,
    filter: Optional[str] = None,
    select: Optional[str] = None,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    """
    Синхронизировать данные из 1С в tab_ss для семантического поиска.
    Sync 1C data into tab_ss vector cache for semantic search.

    Загружает объекты из 1С через OData и индексирует их в tab_ss для последующего
    семантического поиска (semantic_search_1c).

    Args:
        entity_type:       OData тип, например "Catalog_Номенклатура".
        organization:      Идентификатор организации (произвольная строка, например "DEMO").
                           Используется для изоляции кэша между организациями.
        object_type_label: Метка типа объекта для кэша, например "Номенклатура".
        filter:            OData $filter для ограничения синхронизируемых данных.
        select:            Список полей через запятую (по умолчанию Ref_Key + Наименование + Код).
        ttl_seconds:       Время жизни кэша в секундах (по умолчанию 86400 = 24ч).

    Returns:
        dataset_id, added_count, updated_count — результат индексации.
    """
    # Загрузить все объекты из 1С (постранично)
    all_items: list[dict] = []
    skip = 0
    batch = 500
    while True:
        batch_data = await oc.query(
            entity_type, filter=filter, select=select, top=batch, skip=skip
        )
        if not batch_data:
            break
        all_items.extend(batch_data)
        if len(batch_data) < batch:
            break
        skip += batch

    if not all_items:
        return {"error": "Данные не найдены в 1С по заданным параметрам", "entity_type": entity_type}

    # Преобразовать для TAB AI: нужно поле "Код"
    for item in all_items:
        if "Код" not in item or not item["Код"]:
            # Использовать Ref_Key как Код если нет своего
            item["Код"] = str(item.get("Ref_Key", ""))

    items_json = json.dumps(all_items, ensure_ascii=False, default=str)

    async with _tab_ss_client() as client:
        response = await client.post(
            "/v1/datasets/load",
            json={
                "items": items_json,
                "ttl_seconds": ttl_seconds,
                "organization": organization,
                "object_type": object_type_label,
            },
        )
    result = _tab_ss_handle(response)
    result["synced_count"] = len(all_items)
    result["entity_type"] = entity_type
    return result


@mcp.tool()
async def semantic_search_1c(
    organization: str,
    object_type_label: str,
    search_text: str,
    property_name: str = "Наименование",
    model: str = "openai",
    top_k: int = 10,
    min_score: Optional[float] = None,
) -> dict[str, Any]:
    """
    Семантический поиск объектов 1С по смыслу через tab_ss.
    Semantic search for 1C objects by meaning using tab_ss.

    Находит объекты по смыслу, не требуя точного совпадения строк.
    ВАЖНО: Перед поиском нужно загрузить данные через sync_1c_to_tab_ss.

    Примеры запросов:
    - "молоко пастеризованное" найдёт "Молоко 1% пастеризованное 1л"
    - "токарный станок" найдёт "Станок токарный ТВ-320 б/у"

    Args:
        organization:      Идентификатор организации (как при sync_1c_to_tab_ss).
        object_type_label: Метка типа объекта (как при sync_1c_to_tab_ss).
        search_text:       Текст для поиска (любое описание, запрос на русском/английском).
        property_name:     По какому свойству искать (по умолчанию "Наименование").
        model:             LLM провайдер: "openai", "deepseek", "qwen", "yandexgpt", "gigachat".
        top_k:             Количество результатов (по умолчанию 10).
        min_score:         Минимальный порог схожести от -1.0 до 1.0 (опционально).

    Returns:
        Список найденных объектов с кодами (Ref_Key) и оценками схожести.
        Затем вызовите get_1c_object для получения полных данных.
    """
    properties = json.dumps(
        [{"line_no": 0, "property": property_name, "value": search_text}],
        ensure_ascii=False,
    )

    payload: dict[str, Any] = {
        "object_type": object_type_label,
        "organization": organization,
        "model": model,
        "properties": properties,
        "top_k": top_k,
    }
    if min_score is not None:
        payload["min_score"] = min_score

    async with _tab_ss_client() as client:
        response = await client.post("/v1/search", json=payload)
    return _tab_ss_handle(response)


@mcp.tool()
async def semantic_search_multi_property(
    organization: str,
    object_type_label: str,
    search_properties: list[dict[str, str]],
    model: str = "openai",
    top_k: int = 10,
) -> dict[str, Any]:
    """
    Семантический поиск по нескольким свойствам одновременно.
    Multi-property semantic search for 1C objects.

    Для сложных запросов, когда нужно искать по комбинации свойств.

    Args:
        organization:       Идентификатор организации.
        object_type_label:  Метка типа объекта.
        search_properties:  Список свойств для поиска.
                            Формат: [{"property": "Наименование", "value": "молоко"},
                                     {"property": "Описание", "value": "пастеризованное"}]
        model:              LLM провайдер.
        top_k:              Количество результатов.

    Returns:
        Результаты поиска с оценками по каждому свойству.
    """
    props = [
        {"line_no": i, "property": p.get("property", "Наименование"), "value": p.get("value", "")}
        for i, p in enumerate(search_properties)
    ]
    properties_json = json.dumps(props, ensure_ascii=False)

    async with _tab_ss_client() as client:
        response = await client.post(
            "/v1/search",
            json={
                "object_type": object_type_label,
                "organization": organization,
                "model": model,
                "properties": properties_json,
                "top_k": top_k,
            },
        )
    return _tab_ss_handle(response)


@mcp.tool()
async def set_tab_ss_provider_token(
    provider: str,
    token: str,
    expires_at: Optional[str] = None,
) -> dict[str, Any]:
    """
    Настроить API ключ LLM провайдера в tab_ss.
    Set LLM provider API token in tab_ss (used by tab_ca multi-agent).

    tab_ss передаёт токен в tab_ca, который использует его для вызовов LLM провайдеров
    (openai, deepseek, gigachat и др.) при семантическом поиске и аналитике.

    Args:
        provider:   Провайдер: "openai", "deepseek", "qwen", "yandexgpt", "gigachat".
        token:      API ключ провайдера.
        expires_at: Дата истечения в формате ISO-8601, например "2026-12-31T23:59:59Z".

    Returns:
        Подтверждение сохранения токена.
    """
    body: dict[str, Any] = {"provider": provider, "token": token}
    if expires_at:
        body["expires_at"] = expires_at
    async with _tab_ss_client() as client:
        response = await client.post("/v1/providers/tokens", json=body)
    return _tab_ss_handle(response)


@mcp.tool()
async def forecast_1c_data(
    table_data: list[dict[str, Any]],
    months_count: int = 3,
) -> dict[str, Any]:
    """
    Спрогнозировать динамику данных 1С на основе исторических записей.
    Forecast 1C data dynamics based on historical records.

    Соответствует функции ТАБ:БИИ — СпрогнозируйДинамикуИзмененияДанных.

    Args:
        table_data:   Список записей с историческими данными.
                      Обязательно наличие колонки с датой (Дата, Date, Period и т.п.)
                      и числовых колонок (Продажи, Количество, Сумма и т.п.).
                      Пример: [{"Дата": "2024-01-15", "Продажи": 100, "Заявки": 10}, ...]
                      Рекомендуется 12-48 месяцев истории.
        months_count: Количество месяцев для прогноза (по умолчанию 3).

    Returns:
        Прогнозируемые значения в той же структуре, что входные данные.
    """
    async with _tab_ss_client() as client:
        response = await client.post(
            "/v1/data_dynamics",
            json={
                "ТаблицаДанных": table_data,
                "КоличествоМесяцев": months_count,
            },
        )
    return _tab_ss_handle(response)


@mcp.tool()
async def verify_document_signature(document_base64: str) -> dict[str, Any]:
    """
    Проверить наличие подписи и печати на документе 1С.
    Verify signature and stamp presence on a 1C document scan.

    Соответствует функции ТАБ:БИИ — ЕстьЛиПодписьИПечатьНаДокументе.

    Args:
        document_base64: Документ (скан/PDF) в формате Base64.
                         Поддерживаемые форматы: PDF, JPG.

    Returns:
        {"signature_score": 0-100, "stamp_score": 0-100, "overall_score": 0-100}
    """
    async with _tab_ss_client() as client:
        response = await client.post(
            "/v1/verify/signature-stamp",
            json={"document_base64": document_base64},
        )
    return _tab_ss_handle(response)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Запуск MCP-сервера через stdio транспорт.
    Используется командой tab-ai-mcp и uvx tab-ai-mcp.
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
