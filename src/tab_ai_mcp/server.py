"""
tab_mcp — MCP-коннектор к 1С:Предприятие
=========================================
Компонент архитектуры ТАБ:БИИ. Три инструмента:

  read_1c              — чтение данных из 1С через OData
  write_1c             — запись данных в 1С через OData (upsert)
  search_1c_metadata   — семантический поиск по метаданным 1С через tab_ss

При старте:
  1. Определяет конфигурацию 1С (Бухгалтерия, УНФ, ERP, ЗУП и др.)
  2. Загружает соответствующие знания и промпты
  3. Индексирует метаданные 1С в tab_ss для семантического поиска

Конфигурация (переменные окружения):
  ONEC_BASE_URL  — URL базы 1С, например http://server/myapp
  ONEC_USERNAME  — Логин 1С
  ONEC_PASSWORD  — Пароль 1С
  TAB_SS_URL     — URL сервиса tab_ss (semantic search / LLM service)
  TAB_SS_API_KEY — Ключ доступа к tab_ss (заголовок X-Admin-Key)
  ONEC_ORGANIZATION — организация в tab_ss; если задана, ею же помечается индекс метаданных
  TAB_SS_USER_ID / ONEC_USER_ID — опционально, для поиска с разрезом по пользователю
  TAB_SS_MODEL   — Модель для семантического поиска (по умолчанию: openai)
  MCP_TRANSPORT  — stdio (по умолчанию) | streamable-http
  MCP_HOST       — адрес для HTTP транспорта (по умолчанию 0.0.0.0)
  MCP_PORT       — порт для HTTP транспорта (по умолчанию 8001)
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


async def _resolve_entity_type(query: str, model: str = TAB_SS_MODEL) -> str:
    """
    Если entity_type — не точное OData-имя, найти подходящий тип через tab_ss.
    При недоступности tab_ss или незавершённом precompute — fallback на локальный поиск.
    Возвращает OData-имя или исходный query если не нашли.
    """
    org = _metadata_org(os.environ.get("ONEC_BASE_URL", "default"))
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


# ── Базовые инструкции ─────────────────────────────────────────────────────────

_BASE_INSTRUCTIONS = (
    "MCP-коннектор к 1С:Предприятие (компонент tab_mcp архитектуры ТАБ:БИИ).\n\n"
    "Два инструмента:\n"
    "  read_1c  — читает данные из 1С через OData\n"
    "  write_1c — записывает данные в 1С через OData (upsert)\n\n"
    "Оба инструмента принимают entity_type как точное OData-имя ИЛИ описание "
    "на естественном языке — во втором случае автоматически находят нужный тип.\n\n"
    "Примеры:\n"
    "  read_1c('Catalog_Номенклатура')       — точное имя\n"
    "  read_1c('остатки товаров на складе')  — описание, резолвится автоматически\n\n"
    "Имена OData типов: Catalog_*, Document_*, AccumulationRegister_*, "
    "AccountingRegister_*, ChartOfAccounts_* и др.\n\n"
    "Виртуальные таблицы (bound functions) для регистров:\n"
    "  AccumulationRegister_X/Balance(Period=...) — остатки на дату\n"
    "  AccumulationRegister_X/Turnovers(StartPeriod=..., EndPeriod=...) — обороты\n"
    "  AccountingRegister_X/Balance(Period=...) — остатки по счетам\n"
    "  AccountingRegister_X/Turnovers(StartPeriod=..., EndPeriod=...) — обороты\n"
    "  AccountingRegister_X/BalanceAndTurnovers(...) — остатки и обороты\n"
    "Пример: read_1c('AccountingRegister_Хозрасчетный/Balance(Period=datetime\\'2024-12-31T00:00:00\\')')\n"
    "  с filter='Account_Key eq guid\\'xxx...\\''\n\n"
    "AccountingRegister_X без суффикса автоматически заменяется на _RecordType для "
    "получения плоских записей (без группировки по документу).\n"
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
        entity_type: str,
        filter: Optional[str] = None,
        select: Optional[str] = None,
        top: int = 100,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Прочитать данные из 1С через OData.
        Read data from 1C via OData.

        Принимает как точное OData-имя типа, так и описание на естественном языке —
        во втором случае автоматически находит нужный тип через семантический поиск.

        Args:
            entity_type: OData тип объекта или описание на русском/английском.
                         Точное имя: "Catalog_Номенклатура", "AccountingRegister_Хозрасчетный_RecordType"
                         Описание:   "остатки товаров", "задолженность покупателей"
            filter:      OData $filter выражение.
                         Примеры:
                           "Description eq 'Молоко'"
                           "Period ge datetime'2024-01-01T00:00:00'"
                           "AccountDr_Key eq guid'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'"
            select:      Поля через запятую, например "Ref_Key,Description,Code".
                         Если не указан — все поля.
            top:         Количество записей (по умолчанию 100).
            skip:        Сдвиг для пагинации.

        Returns:
            Список объектов в формате JSON.
        """
        resolved = (
            entity_type if _looks_like_odata_name(entity_type)
            else await _resolve_entity_type(entity_type)
        )
        resolved = _normalize_entity_for_read(resolved)
        t0 = time.monotonic()
        try:
            result = await oc.query(resolved, filter=filter, select=select, top=top, skip=skip)
            _log_request("read_1c", entity_type, resolved,
                         {"filter": filter, "select": select, "top": top, "skip": skip},
                         (time.monotonic() - t0) * 1000, rows=len(result))
            return result
        except Exception as exc:
            _log_request("read_1c", entity_type, resolved,
                         {"filter": filter, "select": select, "top": top, "skip": skip},
                         (time.monotonic() - t0) * 1000, error=str(exc))
            raise

    @mcp.tool()
    async def write_1c(
        entity_type: str,
        data: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Записать данные в 1С через OData (upsert).
        Write data to 1C via OData (upsert).

        Принимает как точное OData-имя типа, так и описание на естественном языке.

        Логика upsert для каждого объекта:
          - есть Ref_Key → PATCH (обновление существующего)
          - нет Ref_Key  → POST  (создание нового)

        Args:
            entity_type: OData тип объекта или описание на русском/английском.
            data:        Объект или список объектов для записи.

        Returns:
            Список результатов записи по каждому объекту.
        """
        resolved = (
            entity_type if _looks_like_odata_name(entity_type)
            else await _resolve_entity_type(entity_type)
        )
        items = data if isinstance(data, list) else [data]
        results = []
        t0 = time.monotonic()
        try:
            for item in items:
                ref_key = item.get("Ref_Key")
                if ref_key:
                    result = await oc.update(resolved, ref_key, item)
                    results.append({"action": "updated", "Ref_Key": ref_key, "result": result})
                else:
                    result = await oc.create(resolved, item)
                    results.append({"action": "created", "result": result})
            _log_request("write_1c", entity_type, resolved, {"count": len(items)},
                         (time.monotonic() - t0) * 1000, rows=len(results))
            return {"written": len(results), "items": results}
        except Exception as exc:
            _log_request("write_1c", entity_type, resolved, {"count": len(items)},
                         (time.monotonic() - t0) * 1000, error=str(exc))
            raise

    @mcp.tool()
    async def count_document_marks(document_base64: str) -> dict[str, Any]:
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
                    "model": TAB_SS_MODEL,
                },
            )
        return _tab_ss_handle(response)

    return mcp


# ── HTTP сервер логов ──────────────────────────────────────────────────────────

async def _handle_logs_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Минимальный HTTP-обработчик для GET /logs."""
    try:
        raw = await asyncio.wait_for(reader.read(4096), timeout=5.0)
    except asyncio.TimeoutError:
        writer.close()
        return

    request = raw.decode(errors="replace")
    first_line = request.split("\n")[0]  # GET /logs?last=100&api_key=... HTTP/1.1

    # Парсим путь и query string
    path = first_line.split(" ")[1] if len(first_line.split(" ")) > 1 else "/"
    path_part, _, qs = path.partition("?")

    def _qs_param(qs: str, key: str) -> str | None:
        for part in qs.split("&"):
            k, _, v = part.partition("=")
            if k == key:
                return v
        return None

    # Проверка API ключа (в query string или заголовке X-Api-Key)
    api_key = _qs_param(qs, "api_key")
    if not api_key:
        for line in request.split("\n"):
            if line.lower().startswith("x-api-key:"):
                api_key = line.split(":", 1)[1].strip()
                break

    def _respond(status: str, body: str) -> None:
        body_bytes = body.encode()
        resp = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
        )
        writer.write(resp.encode() + body_bytes)

    if path_part not in ("/logs", "/logs/"):
        _respond("404 Not Found", '{"error":"not found"}')
    elif api_key != _LOG_API_KEY:
        _respond("401 Unauthorized", '{"error":"invalid api_key"}')
    else:
        last = int(_qs_param(qs, "last") or len(_request_log))
        entries = list(_request_log)[-last:]
        body = json.dumps(
            {"count": len(entries), "total": len(_request_log), "logs": entries},
            ensure_ascii=False,
        )
        _respond("200 OK", body)

    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()


async def _serve_logs(host: str, port: int) -> None:
    """Запустить HTTP сервер логов."""
    server = await asyncio.start_server(_handle_logs_http, host, port)
    logger.info("Сервер логов: http://%s:%d/logs?api_key=<key>", host, port)
    async with server:
        await server.serve_forever()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Запуск MCP-сервера.

    Транспорт задаётся переменной окружения MCP_TRANSPORT:
      stdio            — для Claude Desktop / Claude Code (по умолчанию)
      streamable-http  — для вызовов из tab_ss и других сервисов по сети
    """
    instructions, prompts = asyncio.run(_load_instructions())
    mcp = _make_mcp(instructions, prompts)

    # Индексация метаданных в tab_ss — в фоне, не блокирует старт
    async def _background_index() -> None:
        await _index_metadata()

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    log_host = os.environ.get("LOG_HOST", "0.0.0.0")
    log_port = int(os.environ.get("LOG_PORT", "8002"))

    if transport == "streamable-http":
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))

        async def _run_with_index() -> None:
            await asyncio.gather(
                _background_index(),
                _serve_logs(log_host, log_port),
                mcp.run_async(transport="streamable-http", host=host, port=port),
            )
        asyncio.run(_run_with_index())
    else:
        # stdio: индексируем и запускаем лог-сервер в фоне
        async def _start_logs_in_background() -> None:
            await _background_index()
            asyncio.get_event_loop().create_task(_serve_logs(log_host, log_port))

        asyncio.run(_start_logs_in_background())
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
