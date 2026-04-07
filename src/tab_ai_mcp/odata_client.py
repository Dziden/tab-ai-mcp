"""
1С OData HTTP клиент.

Взаимодействует с 1С через стандартный OData REST API:
  {base_url}/odata/standard.odata/

Credentials передаются per-call (base_url, login, password) —
не хранятся как глобальные переменные.
Для обратной совместимости и локальной разработки читаются из env как fallback.
"""

from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Optional

import httpx

# Fallback для локальной разработки (env переменные)
_ENV_BASE_URL = os.environ.get("ONEC_BASE_URL", "").rstrip("/")
_ENV_USERNAME = os.environ.get("ONEC_USERNAME", "")
_ENV_PASSWORD = os.environ.get("ONEC_PASSWORD", "")

_ODATA_NS = "http://docs.oasis-open.org/odata/ns/edm"
_ODATA_META_NS = "http://docs.oasis-open.org/odata/ns/edmx"


def _make_client(
    base_url: str,
    login: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 120,
) -> httpx.AsyncClient:
    odata_url = f"{base_url.rstrip('/')}/odata/standard.odata"
    auth = httpx.BasicAuth(login, password) if login else None
    return httpx.AsyncClient(
        base_url=odata_url,
        auth=auth,
        headers={
            "Accept": "application/json;odata.metadata=minimal",
            "OData-MaxVersion": "4.0",
            "Content-Type": "application/json",
        },
        verify=verify_ssl,
        timeout=httpx.Timeout(float(timeout), connect=10.0),
    )


def _env_client() -> httpx.AsyncClient:
    """Клиент на основе env-переменных (fallback для локальной разработки)."""
    if not _ENV_BASE_URL:
        raise RuntimeError(
            "ONEC_BASE_URL не задан. Укажите URL базы 1С или передайте credentials в запросе."
        )
    return _make_client(_ENV_BASE_URL, _ENV_USERNAME, _ENV_PASSWORD)


def _handle(response: httpx.Response) -> Any:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        raise RuntimeError(
            f"HTTP {exc.response.status_code} от 1С OData: {detail}"
        ) from exc
    if response.status_code == 204:
        return None
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


def _extract_value(data: Any) -> Any:
    """Извлечь value из OData ответа {"value": [...]} или вернуть как есть."""
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


# ── API Methods ────────────────────────────────────────────────────────────────

async def get_metadata(
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Получить метаданные базы 1С ($metadata).
    """
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.get("/$metadata", headers={"Accept": "application/xml"})
    response.raise_for_status()
    xml_text = response.text

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Не удалось разобрать $metadata XML: {e}") from e

    entity_types: list[str] = []
    for elem in root.iter():
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local == "EntityType":
            name = elem.get("Name", "")
            if name:
                entity_types.append(name)

    catalogs = sorted([t for t in entity_types if t.startswith("Catalog_")])
    documents = sorted([t for t in entity_types if t.startswith("Document_")])
    registers = sorted([t for t in entity_types if "Register_" in t])
    other = sorted([
        t for t in entity_types
        if not t.startswith("Catalog_")
        and not t.startswith("Document_")
        and "Register_" not in t
    ])

    return {
        "catalogs": catalogs,
        "documents": documents,
        "registers": registers,
        "other": other,
        "total": len(entity_types),
    }


async def query(
    entity: str,
    filter: Optional[str] = None,
    select: Optional[str] = None,
    expand: Optional[str] = None,
    top: int = 20,
    skip: int = 0,
    orderby: Optional[str] = None,
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> list[dict]:
    """
    Запрос списка объектов через OData.
    """
    # $skip=0 заставляет 1C добавлять AUTOORDER, что ломает фильтры на ряде сущностей
    params: dict[str, Any] = {"$top": top}
    if skip:
        params["$skip"] = skip
    if orderby:
        params["$orderby"] = orderby
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand

    # 1С OData не принимает '+' (form encoding) — нужен '%20' (percent encoding)
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.get(f"/{entity}?{qs}")
    data = _handle(response)
    return _extract_value(data) or []


async def get_one(
    entity: str,
    ref_id: str,
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Получить один объект по GUID."""
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.get(f"/{entity}({key})")
    return _handle(response) or {}


async def create(
    entity: str,
    data: dict[str, Any],
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Создать новый объект. POST /Entity."""
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.post(f"/{entity}", json=data)
    return _handle(response) or {}


async def update(
    entity: str,
    ref_id: str,
    data: dict[str, Any],
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Частичное обновление объекта. PATCH /Entity(guid'...')."""
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.patch(
            f"/{entity}({key})",
            json=data,
            headers={"If-Match": "*"},
        )
    result = _handle(response)
    return result or {"status": "updated", "ref_id": ref_id}


async def delete(
    entity: str,
    ref_id: str,
    base_url: str = "",
    login: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout: int = 120,
) -> dict[str, Any]:
    """Удалить объект. DELETE /Entity(guid'...')."""
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    client_ctx = (
        _make_client(base_url, login, password, verify_ssl, timeout)
        if base_url else _env_client()
    )
    async with client_ctx as client:
        response = await client.delete(
            f"/{entity}({key})",
            headers={"If-Match": "*"},
        )
    _handle(response)
    return {"status": "deleted", "ref_id": ref_id}
