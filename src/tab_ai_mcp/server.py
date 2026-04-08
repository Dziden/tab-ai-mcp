"""
tab_mcp — MCP-коннектор к 1С:Предприятие
=========================================
Компонент архитектуры ТАБ:БИИ. Три инструмента:

  read_1c              — чтение данных из 1С через OData
  write_1c             — запись данных в 1С через OData (upsert)
  count_document_marks — подсчёт печатей и подписей в документе

При старте:
  1. Определяет конфигурацию 1С (Бухгалтерия, УНФ, ERP, ЗУП и др.)
  2. Загружает соответствующие знания и промпты
  3. Индексирует метаданные 1С в tab_ss для семантического поиска

Конфигурация (переменные окружения):
  TAB_SS_URL     — URL сервиса tab_ss (credentials 1С + семантический поиск)
  TAB_SS_API_KEY — Ключ доступа к tab_ss (заголовок X-Admin-Key)
  ONEC_ORGANIZATION — организация для индексации метаданных OData в tab_ss
  MCP_TRANSPORT  — stdio (по умолчанию) | streamable-http
  MCP_HOST       — адрес для HTTP транспорта (по умолчанию 0.0.0.0)
  PORT / MCP_PORT — порт для HTTP транспорта (Railway задаёт PORT автоматически)
  LOG_API_KEY    — ключ для /logs эндпоинта (по умолчанию = TAB_SS_API_KEY)

  Fallback для локальной разработки (если tab_ss недоступен):
  ONEC_BASE_URL  — URL базы 1С, например http://server/myapp
  ONEC_USERNAME  — Логин 1С
  ONEC_PASSWORD  — Пароль 1С
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from tab_ai_mcp import odata_client as oc
from tab_ai_mcp import config_detector
from tab_ai_mcp.knowledge import KNOWLEDGE_MAP

logger = logging.getLogger(__name__)

# ── tab_ss Configuration ───────────────────────────────────────────────────────

_TAB_SS_DEFAULT_KEY = "a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
_TAB_SS_DEFAULT_URL = "https://test-docker-2-production.up.railway.app"

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

TAB_SS_MODEL = os.environ.get("TAB_SS_MODEL", "openai")

# TTL метаданных в tab_ss — 7 дней (метаданные меняются редко)
_METADATA_TTL = 7 * 24 * 3600

# ── Кеш credentials 1С ────────────────────────────────────────────────────────
# Ключ: (organization, user_id) → {"odata_base_url", "login", "password", ...}
# TTL 5 минут — не держим пароли долго, но и не долбим tab_ss на каждый запрос
_CONN_CACHE: dict[tuple[str, str], tuple[dict, float]] = {}
_CONN_CACHE_TTL = 300.0

# ── Лог запросов ───────────────────────────────────────────────────────────────

_LOG_API_KEY = os.environ.get("LOG_API_KEY", TAB_SS_KEY)
_LOG_MAX_ENTRIES = int(os.environ.get("LOG_MAX_ENTRIES", "2000"))
_request_log: deque[dict] = deque(maxlen=_LOG_MAX_ENTRIES)


def _log_request(
    tool: str,
    entity_type: str,
    resolved: str,
    params: dict,
    duration_ms: float,
    rows: int | None = None,
    error: str | None = None,
) -> None:
    _request_log.append({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": tool,
        "entity_type": entity_type,
        "resolved": resolved if resolved != entity_type else None,
        "params": {k: v for k, v in params.items() if v is not None},
        "duration_ms": round(duration_ms, 1),
        "rows": rows,
        "error": error,
    })


# Локальный кеш метаданных для fallback-поиска без tab_ss
# {odata_name: description_words_set}
_LOCAL_METADATA_INDEX: dict[str, set[str]] = {}

# Префиксы OData → (внутренний тип 1С, читаемое название)
_PREFIX_MAP = {
    "Catalog_":                       ("Справочник",              "Справочник"),
    "Document_":                      ("Документ",                "Документ"),
    "AccumulationRegister_":          ("РегистрНакопления",       "Регистр накопления"),
    "AccountingRegister_":            ("РегистрБухгалтерии",      "Регистр бухгалтерии"),
    "InformationRegister_":           ("РегистрСведений",         "Регистр сведений"),
    "ChartOfAccounts_":               ("ПланСчетов",              "План счетов"),
    "ChartOfCharacteristicTypes_":    ("ПланВидовХарактеристик",  "План видов характеристик"),
    "ChartOfCalculationTypes_":       ("ПланВидовРасчета",        "План видов расчёта"),
    "BusinessProcess_":               ("БизнесПроцесс",           "Бизнес-процесс"),
    "Task_":                          ("Задача",                  "Задача"),
    "ExchangePlan_":                  ("ПланОбмена",              "План обмена"),
    "Constant_":                      ("Константа",               "Константа"),
}


def _split_camel(name: str) -> str:
    """РеализацияТоваровУслуг → Реализация Товаров Услуг"""
    import re
    result = re.sub(r"(?<=[а-яёa-z0-9])(?=[А-ЯЁA-Z])", " ", name)
    return result


def _local_resolve(query: str) -> str | None:
    """
    Локальный fallback-резолвер: word-overlap по кешированным описаниям.
    Используется когда tab_ss недоступен или precompute не готов.
    """
    if not _LOCAL_METADATA_INDEX:
        return None
    query_words = set(query.lower().split())
    best_name, best_score = None, 0
    for odata_name, desc_words in _LOCAL_METADATA_INDEX.items():
        score = len(query_words & desc_words)
        if score > best_score:
            best_name, best_score = odata_name, score
    if best_score > 0:
        logger.info("Локальный резолв '%s' → '%s' (score=%d)", query, best_name, best_score)
        return best_name
    return None


def _parse_entity(name: str) -> tuple[str, str, str, str]:
    """Разобрать OData имя → (внутренний_тип, читаемое_имя, читаемый_тип, описание)."""
    for prefix, (internal, readable) in _PREFIX_MAP.items():
        if name.startswith(prefix):
            short = name[len(prefix):]
            words = _split_camel(short)
            description = f"{readable}: {words}"
            return internal, short, readable, description
    return "Прочее", name, "Прочее", name


def _looks_like_odata_name(name: str) -> bool:
    """Проверить похоже ли имя на OData тип (Catalog_*, Document_* и т.д.)"""
    return any(name.startswith(prefix) for prefix in _PREFIX_MAP)


def _normalize_entity_for_read(name: str) -> str:
    """
    AccountingRegister_X без суффикса возвращает сгруппированную структуру,
    неудобную для фильтрации. Автоматически добавляем _RecordType.
    Не трогаем bound functions (содержат '/') и уже нормализованные имена.
    """
    if (
        name.startswith("AccountingRegister_")
        and "/" not in name
        and not name.endswith("_RecordType")
    ):
        return name + "_RecordType"
    return name


async def _resolve_entity_type(
    query: str,
    model: str = TAB_SS_MODEL,
    organization: str = "",
    user_id: str = "",
) -> str:
    """
    Если entity_type — не точное OData-имя, найти подходящий тип через tab_ss.
    При недоступности tab_ss или незавершённом precompute — fallback на локальный поиск.
    Возвращает OData-имя или исходный query если не нашли.
    """
    org = _metadata_org(organization or os.environ.get("ONEC_BASE_URL", "default"))
    properties = json.dumps(
        [{"line_no": 0, "property": "Описание", "value": query}],
        ensure_ascii=False,
    )
    search_body: dict = {
        "object_type": "1с_метаданные",
        "organization": org,
        "model": model,
        "properties": properties,
        "top_k": 1,
    }
    uid = _tab_ss_user_id()
    if uid:
        search_body["user_id"] = uid
    try:
        async with _tab_ss_client() as client:
            response = await client.post("/v1/search", json=search_body)
        result = _tab_ss_handle(response)
        hits = result if isinstance(result, list) else result.get("results", [])
        # Если precompute не готов — hits будет пустым, используем fallback
        if hits:
            code = hits[0].get("Код") or hits[0].get("code") or hits[0].get("id", "")
            if code:
                logger.info("tab_ss резолв '%s' → '%s'", query, code)
                return code
        # precompute ещё не готов или нет совпадений — пробуем локальный поиск
        local = _local_resolve(query)
        if local:
            return local
    except Exception as exc:
        logger.warning("tab_ss недоступен для резолва '%s': %s — пробую локальный поиск", query, exc)
        local = _local_resolve(query)
        if local:
            return local
    return query


def _tab_ss_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=TAB_SS_URL,
        headers={"X-Admin-Key": TAB_SS_KEY, "Content-Type": "application/json"},
        timeout=httpx.Timeout(120.0, connect=10.0),
    )


def _tab_ss_handle(response: httpx.Response) -> Any:
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


def _metadata_org(base_url: str) -> str:
    """
    Идентификатор организации в tab_ss для индекса метаданных OData.

    Если задан ONEC_ORGANIZATION (или TAB_SS_ORGANIZATION) — используем его,
    чтобы совпадать с остальными вызовами к tab_ss / документацией ТАБ:БИИ.
    Иначе — стабильный pseudo-org от хэша URL базы 1С (изоляция без ручной настройки).
    """
    explicit = os.environ.get("ONEC_ORGANIZATION") or os.environ.get("TAB_SS_ORGANIZATION")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    return "meta_" + hashlib.md5(base_url.encode()).hexdigest()[:12]


def _tab_ss_user_id() -> str | None:
    """Опциональный user_id для tab_ss (семантический поиск / изоляция данных)."""
    raw = os.environ.get("TAB_SS_USER_ID") or os.environ.get("ONEC_USER_ID")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


async def _fetch_onec_credentials(organization: str, user_id: str) -> dict:
    """
    Получить credentials 1С из tab_ss для данной пары (organization, user_id).
    Результат кешируется на TTL=5 мин.

    Fallback: если tab_ss недоступен и заданы ONEC_BASE_URL/ONEC_USERNAME/ONEC_PASSWORD
    в env — использовать их (для локальной разработки и обратной совместимости).
    """
    cache_key = (organization, user_id)
    cached, ts = _CONN_CACHE.get(cache_key, ({}, 0.0))
    if cached and (time.monotonic() - ts) < _CONN_CACHE_TTL:
        return cached

    try:
        async with _tab_ss_client() as client:
            response = await client.post(
                "/v1/onec/connections/resolve",
                params={"organization": organization, "user_id": user_id},
            )
        conn = _tab_ss_handle(response)
        _CONN_CACHE[cache_key] = (conn, time.monotonic())
        return conn
    except Exception as exc:
        # Fallback на env переменные (локальная разработка)
        base_url = os.environ.get("ONEC_BASE_URL", "")
        if base_url:
            logger.warning(
                "tab_ss недоступен для credentials org=%s uid=%s (%s) — использую ONEC_* env",
                organization, user_id, exc,
            )
            return {
                "odata_base_url": base_url.rstrip("/"),
                "login": os.environ.get("ONEC_USERNAME", ""),
                "password": os.environ.get("ONEC_PASSWORD", ""),
                "verify_ssl": True,
                "timeout_seconds": 120,
            }
        raise RuntimeError(
            f"Не удалось получить credentials 1С для organization='{organization}', "
            f"user_id='{user_id}': {exc}"
        ) from exc


# ── Базовые инструкции ─────────────────────────────────────────────────────────

_BASE_INSTRUCTIONS = (
    "MCP-коннектор к 1С:Предприятие (компонент tab_mcp архитектуры ТАБ:БИИ).\n\n"
    "Три инструмента:\n"
    "  read_1c              — читает данные из 1С через OData\n"
    "  write_1c             — записывает данные в 1С через OData (upsert)\n"
    "  count_document_marks — подсчёт печатей и подписей в документе\n\n"
    "read_1c и write_1c принимают параметр query: точное OData-имя ИЛИ описание "
    "на естественном языке — во втором случае автоматически находят нужный тип.\n\n"
    "Примеры query:\n"
    "  'Catalog_Номенклатура'       — точное OData-имя\n"
    "  'остатки товаров на складе'  — описание, резолвится автоматически\n"
    "  'AccountingRegister_Хозрасчетный/Balance(Period=datetime\\'2025-12-31T00:00:00\\')'  — виртуальная таблица\n\n"
    "Имена OData типов: Catalog_*, Document_*, AccumulationRegister_*, "
    "AccountingRegister_*, ChartOfAccounts_* и др.\n\n"
    "Виртуальные таблицы для регистров (передавать в query напрямую):\n"
    "  AccumulationRegister_X/Balance(Period=...) — остатки на дату\n"
    "  AccumulationRegister_X/Turnovers(StartPeriod=..., EndPeriod=...) — обороты\n"
    "  AccountingRegister_X/Balance(Period=...) — остатки по счетам бухучёта\n"
    "  AccountingRegister_X/Turnovers(StartPeriod=..., EndPeriod=...) — обороты\n"
    "AccountingRegister_X без суффикса автоматически заменяется на _RecordType "
    "(плоские проводки). Для остатков всегда используй /Balance(Period=...).\n"
)


# ── Старт: определение конфигурации + индексация метаданных ───────────────────

async def _load_instructions() -> tuple[str, list[dict]]:
    """Определить конфигурацию 1С и вернуть (instructions, prompts)."""
    try:
        metadata = await oc.get_metadata()
        all_types = (
            metadata.get("catalogs", [])
            + metadata.get("documents", [])
            + metadata.get("registers", [])
            + metadata.get("other", [])
        )
        detected = config_detector.detect(all_types)
        knowledge = KNOWLEDGE_MAP.get(detected.name)

        instructions = _BASE_INSTRUCTIONS + f"Конфигурация: {detected.name}\n"
        prompts: list[dict] = []
        if knowledge:
            instructions += knowledge.INSTRUCTIONS
            prompts = knowledge.PROMPTS

        logger.info("Конфигурация: %s (совпадений: %d)", detected.name, detected.confidence)
        return instructions, prompts

    except Exception as exc:
        logger.warning("Не удалось определить конфигурацию: %s", exc)
        return _BASE_INSTRUCTIONS, []


async def _index_metadata() -> None:
    """
    Загрузить метаданные 1С в tab_ss для семантического поиска.
    Вызывается при старте в фоне — не блокирует запуск сервера.
    """
    try:
        metadata = await oc.get_metadata()
        all_types = (
            metadata.get("catalogs", [])
            + metadata.get("documents", [])
            + metadata.get("registers", [])
            + metadata.get("other", [])
        )

        # Фильтруем служебные типы (TabularSection, RecordType и т.п.)
        skip_suffixes = ("_RecordType", "_RowType")
        types = [t for t in all_types if not any(t.endswith(s) for s in skip_suffixes)]

        # Строим датасет: каждая запись — один тип объекта
        items = []
        for name in types:
            internal_type, short_name, readable_type, description = _parse_entity(name)
            item = {
                "Код": name,                  # OData имя — ключ для read_1c/write_1c
                "Наименование": short_name,   # читаемое имя
                "Тип": readable_type,         # категория объекта
                "Описание": description,      # готовое описание — tab_ss не генерирует, ищет сразу
            }
            items.append(item)
            # Строим локальный индекс для fallback-поиска без tab_ss
            _LOCAL_METADATA_INDEX[name] = set(description.lower().split()) | set(short_name.lower().split())

        org = _metadata_org(os.environ.get("ONEC_BASE_URL", "default"))
        items_json = json.dumps(items, ensure_ascii=False, default=str)

        load_body = {
            "items": items_json,
            "ttl_seconds": _METADATA_TTL,
            "organization": org,
            "object_type": "1с_метаданные",
        }
        async with _tab_ss_client() as client:
            response = await client.post("/v1/datasets/load", json=load_body)
        result = _tab_ss_handle(response)
        logger.info(
            "Метаданные проиндексированы в tab_ss: %d типов, org=%s, результат=%s",
            len(items), org, result,
        )

    except Exception as exc:
        logger.warning("Не удалось проиндексировать метаданные в tab_ss: %s", exc)


# ── Сборка MCP сервера ─────────────────────────────────────────────────────────

def _make_mcp(instructions: str, prompts: list[dict]) -> FastMCP:
    mcp = FastMCP(name="tab-mcp", instructions=instructions)

    # Регистрируем типовые промпты из knowledge-файла
    for p in prompts:
        name = p["name"]
        description = p["description"]
        template = p["template"]

        def make_prompt_fn(tmpl: str, desc: str):
            def prompt_fn(**kwargs: str) -> str:
                try:
                    return tmpl.format(**kwargs)
                except KeyError:
                    return tmpl
            prompt_fn.__doc__ = desc
            return prompt_fn

        mcp.prompt(name=name)(make_prompt_fn(template, description))

    # ── Инструменты ───────────────────────────────────────────────────────────

    @mcp.tool()
    async def read_1c(
        organization: str,
        query: str,
        user_id: str = "",
        model: str = TAB_SS_MODEL,
        filter: Optional[str] = None,
        select: Optional[str] = None,
        expand: Optional[str] = None,
        top: int = 100,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Прочитать данные из 1С через OData.
        Read data from 1C via OData.

        Credentials получаются автоматически из tab_ss по (organization, user_id).

        Args:
            organization: Код организации (ключ подключения к 1С в tab_ss). ОБЯЗАТЕЛЕН.
            query:        ЧТО читать. Три варианта:

                          1. ТОЧНЫЙ OData-путь (если известен):
                             "Catalog_Номенклатура"
                             "Document_РеализацияТоваровУслуг"
                             "ChartOfAccounts_Хозрасчетный"

                          2. ВИРТУАЛЬНАЯ ТАБЛИЦА для остатков/оборотов:
                             Остатки на дату:
                               "AccountingRegister_Хозрасчетный/Balance(Period=datetime'2025-12-31T00:00:00')"
                               Поля ответа: Account_Key, СуммаBalance, ВалютнаяСуммаBalance
                             Обороты за период:
                               "AccountingRegister_Хозрасчетный/Turnovers(StartPeriod=datetime'2025-01-01T00:00:00',EndPeriod=datetime'2025-12-31T00:00:00')"
                             Накопительный регистр — остатки:
                               "AccumulationRegister_ТоварыНаСкладах/Balance(Period=datetime'2025-12-31T00:00:00')"

                          3. ОПИСАНИЕ НА РУССКОМ/АНГЛИЙСКОМ (резолвится автоматически):
                             "остатки товаров на складах"
                             "задолженность покупателей"
                             "список контрагентов"

                          ═══ ОСТАТКИ ПО БАНКУ — ПОШАГОВЫЙ АЛГОРИТМ ════════════
                          Шаг 1. Получить Ref_Key для каждого счёта отдельно
                                 (ChartOfAccounts НЕ поддерживает 'or' в filter!):
                            ref51 = read_1c(org, "ChartOfAccounts_Хозрасчетный",
                                            filter="Code eq '51'")[0]["Ref_Key"]
                            ref52 = read_1c(org, "ChartOfAccounts_Хозрасчетный",
                                            filter="Code eq '52'")[0]["Ref_Key"]
                            ref55 = read_1c(org, "ChartOfAccounts_Хозрасчетный",
                                            filter="Code eq '55'")[0]["Ref_Key"]
                          Шаг 2. Запросить Balance для каждого счёта:
                            read_1c(org,
                              "AccountingRegister_Хозрасчетный/Balance(Period=datetime'2025-12-31T00:00:00')",
                              filter="Account_Key eq guid'<ref51>'")
                            → поле СуммаBalance = рублёвый остаток (51, 55)
                            → поле ВалютнаяСуммаBalance = валютный остаток (52)
                          ═══════════════════════════════════════════════════════

            user_id:      ID пользователя (по умолчанию "").
            model:        Модель семантического поиска.
            filter:       OData $filter. Пример: "Account_Key eq guid'xxx'" или "Code eq '51'".
            select:       Поля через запятую. По умолчанию — все поля.
            expand:       OData $expand для вложенных объектов.
            top:          Количество записей (по умолчанию 100).
            skip:         Сдвиг для пагинации.

        Returns:
            Список объектов в формате JSON.
        """
        conn = await _fetch_onec_credentials(organization, user_id)
        resolved = (
            query if _looks_like_odata_name(query) or "/" in query
            else await _resolve_entity_type(query, model=model, organization=organization, user_id=user_id)
        )
        resolved = _normalize_entity_for_read(resolved)
        t0 = time.monotonic()
        try:
            result = await oc.query(
                resolved, filter=filter, select=select, expand=expand, top=top, skip=skip,
                base_url=conn["odata_base_url"],
                login=conn["login"],
                password=conn["password"],
                verify_ssl=conn.get("verify_ssl", True),
                timeout=conn.get("timeout_seconds", 120),
            )
            _log_request("read_1c", query, resolved,
                         {"org": organization, "filter": filter, "select": select, "top": top, "skip": skip},
                         (time.monotonic() - t0) * 1000, rows=len(result))
            return result
        except Exception as exc:
            _log_request("read_1c", query, resolved,
                         {"org": organization, "filter": filter, "select": select, "top": top, "skip": skip},
                         (time.monotonic() - t0) * 1000, error=str(exc))
            raise

    @mcp.tool()
    async def write_1c(
        organization: str,
        query: str,
        data: dict[str, Any] | list[dict[str, Any]],
        user_id: str = "",
        model: str = TAB_SS_MODEL,
    ) -> dict[str, Any]:
        """
        Записать данные в 1С через OData (upsert).
        Write data to 1C via OData (upsert).

        Credentials получаются автоматически из tab_ss по (organization, user_id).

        Логика upsert:
          - есть Ref_Key в data → PATCH (обновление существующего объекта)
          - нет Ref_Key         → POST  (создание нового объекта)

        Args:
            organization: Код организации. ОБЯЗАТЕЛЕН.
            query:        Точное OData-имя или описание на русском/английском.
                          Примеры: "Catalog_Номенклатура", "Document_РеализацияТоваровУслуг",
                                   "номенклатура", "реализация товаров"
            data:         Объект или список объектов для записи.
            user_id:      ID пользователя (по умолчанию "").
            model:        Модель семантического поиска.

        Returns:
            {"written": N, "items": [...]}
        """
        conn = await _fetch_onec_credentials(organization, user_id)
        resolved = (
            query if _looks_like_odata_name(query) or "/" in query
            else await _resolve_entity_type(query, model=model, organization=organization, user_id=user_id)
        )
        items = data if isinstance(data, list) else [data]
        results = []
        t0 = time.monotonic()
        try:
            for item in items:
                ref_key = item.get("Ref_Key")
                if ref_key:
                    result = await oc.update(
                        resolved, ref_key, item,
                        base_url=conn["odata_base_url"],
                        login=conn["login"],
                        password=conn["password"],
                        verify_ssl=conn.get("verify_ssl", True),
                        timeout=conn.get("timeout_seconds", 120),
                    )
                    results.append({"action": "updated", "Ref_Key": ref_key, "result": result})
                else:
                    result = await oc.create(
                        resolved, item,
                        base_url=conn["odata_base_url"],
                        login=conn["login"],
                        password=conn["password"],
                        verify_ssl=conn.get("verify_ssl", True),
                        timeout=conn.get("timeout_seconds", 120),
                    )
                    results.append({"action": "created", "result": result})
            _log_request("write_1c", query, resolved, {"org": organization, "count": len(items)},
                         (time.monotonic() - t0) * 1000, rows=len(results))
            return {"written": len(results), "items": results}
        except Exception as exc:
            _log_request("write_1c", query, resolved, {"org": organization, "count": len(items)},
                         (time.monotonic() - t0) * 1000, error=str(exc))
            raise

    @mcp.tool()
    async def count_document_marks(
        document_base64: str,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Подсчитать количество печатей и подписей в документе, определить принадлежность контрагентам.
        Count stamps and signatures in a document image; attribute each to a contractor.

        Автоматически:
        - Определяет тип документа: ЭДО (электронные КЭП-подписи) или скан бумажного документа
        - Исправляет поворот страницы
        - Извлекает текст через OCR
        - Определяет контрагентов (продавец, покупатель и т.д.) из текста документа
        - Сопоставляет каждую печать/подпись с контрагентом по ИНН или названию

        Для ЭДО: каждая цифровая печать = +1 печать И +1 подпись одновременно.
        Для сканов: круглые штампы и рукописные подписи считаются отдельно.

        Используй этот инструмент когда пользователь спрашивает:
          «сколько печатей», «кто подписал документ», «есть ли подпись директора»,
          «сколько подписей в документе», «определи печати и подписи».

        Args:
            document_base64: Base64-encoded document image (JPG, PNG) or PDF (first page).
            model: LLM provider for contractor extraction: "openai" | "deepseek" | "qwen" |
                   "yandexgpt" | "gigachat". If omitted, uses TAB_SS_MODEL env variable.

        Returns:
            {
              "document_type": "edo" | "scan",
              "contractors": [
                {"name": "...", "inn": "...", "role": "seller|buyer|other",
                 "stamps": N, "signatures": N}
              ],
              "unmatched_stamps": N,
              "unmatched_signatures": N
            }
        """
        async with _tab_ss_client() as client:
            response = await client.post(
                "/v1/verify/count-marks",
                json={
                    "document_base64": document_base64,
                    "model": model or TAB_SS_MODEL,
                },
            )
        return _tab_ss_handle(response)

    return mcp


# ── /logs endpoint (Starlette) ─────────────────────────────────────────────────

async def _logs_handler(request: Any) -> Any:
    """GET /logs — отдаёт последние N записей лога запросов."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    request = Request(request) if not hasattr(request, "query_params") else request
    api_key = request.query_params.get("api_key") or request.headers.get("x-api-key", "")
    if api_key != _LOG_API_KEY:
        return JSONResponse({"error": "invalid api_key"}, status_code=401)
    last_str = request.query_params.get("last", "")
    last = int(last_str) if last_str.isdigit() else len(_request_log)
    entries = list(_request_log)[-last:]
    return JSONResponse(
        {"count": len(entries), "total": len(_request_log), "logs": entries},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Запуск MCP-сервера.

    Транспорт задаётся переменной окружения MCP_TRANSPORT:
      stdio            — для Claude Desktop / Claude Code (по умолчанию)
      streamable-http  — для вызовов из tab_ss и других сервисов по сети

    Для Railway: PORT задаётся автоматически; /logs доступен на том же порту.
    """
    instructions, prompts = asyncio.run(_load_instructions())
    mcp = _make_mcp(instructions, prompts)

    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    if transport == "streamable-http":
        import uvicorn
        from starlette.routing import Route

        host = os.environ.get("MCP_HOST", "0.0.0.0")
        # Railway задаёт PORT; MCP_PORT как явный override
        port = int(os.environ.get("MCP_PORT") or os.environ.get("PORT") or "8001")

        # stateless_http=True — каждый запрос независим, без session handshake.
        # host="0.0.0.0" — отключает DNS rebinding protection (default host=127.0.0.1
        # автоматически включает защиту и блокирует все внешние хосты с 421).
        mcp_app = mcp.streamable_http_app(stateless_http=True, host=host)
        mcp_app.router.routes.append(Route("/logs", _logs_handler))
        combined_app = mcp_app

        logger.info("MCP + /logs: http://%s:%d  (транспорт: streamable-http)", host, port)

        async def _run_all() -> None:
            await asyncio.gather(
                _index_metadata(),
                uvicorn.Server(uvicorn.Config(combined_app, host=host, port=port, log_level="warning")).serve(),
            )
        asyncio.run(_run_all())
    else:
        # stdio: индексируем метаданные перед запуском
        asyncio.run(_index_metadata())
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
