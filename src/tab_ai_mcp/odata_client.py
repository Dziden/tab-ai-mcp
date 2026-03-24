"""
1С OData HTTP клиент.

Взаимодействует с 1С через стандартный OData REST API:
  {ONEC_BASE_URL}/odata/standard.odata/
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Any, Optional

import httpx

ONEC_BASE_URL = os.environ.get("ONEC_BASE_URL", "").rstrip("/")
ONEC_USERNAME = os.environ.get("ONEC_USERNAME", "")
ONEC_PASSWORD = os.environ.get("ONEC_PASSWORD", "")

_ODATA_NS = "http://docs.oasis-open.org/odata/ns/edm"
_ODATA_META_NS = "http://docs.oasis-open.org/odata/ns/edmx"


def _odata_url() -> str:
    if not ONEC_BASE_URL:
        raise RuntimeError(
            "ONEC_BASE_URL не задан. Укажите URL базы 1С, например: http://server/myapp"
        )
    return f"{ONEC_BASE_URL}/odata/standard.odata"


def _client() -> httpx.AsyncClient:
    auth = httpx.BasicAuth(ONEC_USERNAME, ONEC_PASSWORD) if ONEC_USERNAME else None
    return httpx.AsyncClient(
        base_url=_odata_url(),
        auth=auth,
        headers={
            "Accept": "application/json;odata.metadata=minimal",
            "OData-MaxVersion": "4.0",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


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


# ── XDTO type name mapping ─────────────────────────────────────────────────────

# ── API Methods ────────────────────────────────────────────────────────────────

async def get_metadata() -> dict[str, Any]:
    """
    Получить метаданные базы 1С.
    Возвращает списки каталогов, документов и других типов.
    """
    async with _client() as client:
        response = await client.get("/$metadata", headers={"Accept": "application/xml"})
    response.raise_for_status()
    xml_text = response.text

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Не удалось разобрать $metadata XML: {e}") from e

    # Ищем EntityType элементы (могут быть в разных NS)
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
) -> list[dict]:
    """
    Запрос списка объектов через OData.
    entity: OData имя типа, например Catalog_Номенклатура
    filter: OData $filter выражение, например "Наименование eq 'Молоко'"
    """
    params: dict[str, Any] = {"$top": top, "$skip": skip}
    if filter:
        params["$filter"] = filter
    if select:
        params["$select"] = select
    if expand:
        params["$expand"] = expand

    async with _client() as client:
        response = await client.get(f"/{entity}", params=params)
    data = _handle(response)
    return _extract_value(data) or []


async def get_one(entity: str, ref_id: str) -> dict[str, Any]:
    """
    Получить один объект по GUID.
    """
    # 1С OData: guid в формате guid'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    async with _client() as client:
        response = await client.get(f"/{entity}({key})")
    return _handle(response) or {}


async def create(entity: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Создать новый объект. POST /Entity.
    """
    async with _client() as client:
        response = await client.post(f"/{entity}", json=data)
    return _handle(response) or {}


async def update(entity: str, ref_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Частичное обновление объекта. PATCH /Entity(guid'...').
    """
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    async with _client() as client:
        response = await client.patch(
            f"/{entity}({key})",
            json=data,
            headers={"If-Match": "*"},
        )
    result = _handle(response)
    return result or {"status": "updated", "ref_id": ref_id}


async def delete(entity: str, ref_id: str) -> dict[str, Any]:
    """
    Удалить объект. DELETE /Entity(guid'...').
    """
    key = f"guid'{ref_id}'" if not ref_id.startswith("guid'") else ref_id
    async with _client() as client:
        response = await client.delete(
            f"/{entity}({key})",
            headers={"If-Match": "*"},
        )
    _handle(response)
    return {"status": "deleted", "ref_id": ref_id}
