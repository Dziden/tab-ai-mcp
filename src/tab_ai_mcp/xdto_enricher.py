"""
XDTO обогащение объектов на основе схемы EnterpriseData (ed_nma_str.xsd).

Парсит XSD файл один раз при загрузке модуля и предоставляет:
- Список типов, покрытых схемой
- Полные схемы полей для каждого типа
- Обогащение OData объектов: добавление отсутствующих полей с null-значениями
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from lxml import etree

SCHEMA_PATH = Path(__file__).parent / "schemas" / "ed_nma_str.xsd"

_XS = "http://www.w3.org/2001/XMLSchema"
_ED_NS_PREFIX = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData"

# Кэш: xdto_type → {field_name: {type, required, description}}
_schema_cache: dict[str, dict[str, dict]] = {}
_loaded = False


def _load_schema() -> None:
    global _loaded
    if _loaded:
        return

    if not SCHEMA_PATH.exists():
        # Если XSD не найден — работаем без обогащения
        _loaded = True
        return

    try:
        tree = etree.parse(str(SCHEMA_PATH))
        root = tree.getroot()
        _parse_complex_types(root)
    except Exception:
        pass  # Если XSD не парсится — работаем без обогащения
    _loaded = True


def _parse_complex_types(root: etree._Element) -> None:
    """Извлечь все ComplexType определения из XSD."""
    ns = {"xs": _XS}

    for ct in root.findall(f".//{{{_XS}}}complexType[@name]"):
        type_name = ct.get("name", "")
        if not type_name:
            continue

        # Интересуют типы вида "Справочник.X", "Документ.X", "КлючевыеСвойства.X"
        if not ("." in type_name or type_name.startswith("Ключевые")):
            continue

        fields: dict[str, dict] = {}
        _extract_fields(ct, fields, root)

        if fields:
            _schema_cache[type_name] = fields


def _extract_fields(
    element: etree._Element,
    fields: dict[str, dict],
    root: etree._Element,
    depth: int = 0,
) -> None:
    """Рекурсивно извлечь поля из complexType."""
    if depth > 5:
        return

    ns_xs = f"{{{_XS}}}"

    for child in element:
        tag = child.tag.replace(f"{{{_XS}}}", "")

        if tag in ("sequence", "choice", "all", "complexContent", "extension"):
            _extract_fields(child, fields, root, depth + 1)

        elif tag == "element":
            name = child.get("name")
            if not name:
                continue
            field_type = child.get("type", "xs:string")
            min_occurs = child.get("minOccurs", "1")
            max_occurs = child.get("maxOccurs", "1")
            annotation = child.find(f"{{{_XS}}}annotation/{{{_XS}}}documentation")
            doc = annotation.text.strip() if annotation is not None and annotation.text else ""

            fields[name] = {
                "type": _simplify_type(field_type),
                "required": min_occurs != "0",
                "multiple": max_occurs == "unbounded" or (max_occurs.isdigit() and int(max_occurs) > 1),
                "description": doc,
            }

        elif tag == "attribute":
            name = child.get("name")
            if name:
                fields[f"@{name}"] = {
                    "type": _simplify_type(child.get("type", "xs:string")),
                    "required": child.get("use") == "required",
                    "multiple": False,
                    "description": "",
                }


def _simplify_type(xsd_type: str) -> str:
    """Упростить XSD тип для читаемости."""
    # Убрать namespace prefix
    if ":" in xsd_type:
        xsd_type = xsd_type.split(":")[-1]
    # Маппинг базовых XSD типов
    _map = {
        "string": "Строка",
        "decimal": "Число",
        "integer": "Целое",
        "boolean": "Булево",
        "dateTime": "ДатаВремя",
        "date": "Дата",
        "base64Binary": "ДвоичныеДанные",
        "anyURI": "URI",
        "int": "Целое",
        "long": "Целое",
        "double": "Число",
        "float": "Число",
    }
    return _map.get(xsd_type, xsd_type)


# ── Public API ─────────────────────────────────────────────────────────────────

def list_covered_types() -> list[str]:
    """Список типов XDTO, покрытых схемой."""
    _load_schema()
    return sorted(_schema_cache.keys())


def get_xdto_fields(xdto_type: str) -> dict[str, dict]:
    """
    Получить описание полей для типа XDTO.
    xdto_type: например "Справочник.Номенклатура"
    Возвращает: {field_name: {type, required, multiple, description}}
    """
    _load_schema()
    return _schema_cache.get(xdto_type, {})


def get_xdto_schema_info(xdto_type: str) -> dict[str, Any]:
    """
    Полная информация о схеме типа для отображения пользователю.
    """
    _load_schema()
    fields = _schema_cache.get(xdto_type)
    if fields is None:
        return {"error": f"Тип '{xdto_type}' не найден в схеме EnterpriseData"}

    required = {k: v for k, v in fields.items() if v.get("required") and not k.startswith("@")}
    optional = {k: v for k, v in fields.items() if not v.get("required") and not k.startswith("@")}

    return {
        "type": xdto_type,
        "total_fields": len(fields),
        "required_fields": required,
        "optional_fields": optional,
        "source": "EnterpriseData XSD (ed_nma_str.xsd)",
    }


def enrich_object(obj: dict[str, Any], xdto_type: str) -> dict[str, Any]:
    """
    Обогатить OData объект полями из XDTO схемы.

    Для каждого поля из XSD:
    - Если поле уже есть в obj — оставить как есть
    - Если поля нет — добавить с значением null

    Возвращает обогащённый объект с ключом _xdto_schema.
    """
    _load_schema()
    fields = _schema_cache.get(xdto_type, {})
    if not fields:
        return obj

    enriched = dict(obj)

    # Добавить отсутствующие поля как null
    for field_name, field_meta in fields.items():
        if field_name.startswith("@"):
            continue
        if field_name not in enriched:
            enriched[field_name] = [] if field_meta.get("multiple") else None

    # Схема для справки
    enriched["_xdto_schema"] = {
        "type": xdto_type,
        "fields": {
            k: v for k, v in fields.items()
            if not k.startswith("@")
        },
    }

    return enriched


def find_xdto_type(odata_name: str) -> Optional[str]:
    """
    Найти XDTO тип по OData имени.
    Пробует прямой маппинг и поиск по части имени.
    """
    _load_schema()

    # Прямой маппинг: Catalog_X → Справочник.X, Document_X → Документ.X
    prefixes = {
        "Catalog_": "Справочник.",
        "Document_": "Документ.",
        "ChartOfAccounts_": "ПланСчетов.",
        "ChartOfCharacteristicTypes_": "ПланВидовХарактеристик.",
        "InformationRegister_": "РегистрСведений.",
        "AccumulationRegister_": "РегистрНакопления.",
    }
    for odata_prefix, xdto_prefix in prefixes.items():
        if odata_name.startswith(odata_prefix):
            candidate = xdto_prefix + odata_name[len(odata_prefix):]
            if candidate in _schema_cache:
                return candidate
            # Попробовать без суффиксов (например _RowType, _RecordType)
            base = candidate.split("_")[0]
            if base in _schema_cache:
                return base

    # Поиск по части имени (без префикса)
    name_part = odata_name.split("_", 1)[-1] if "_" in odata_name else odata_name
    for schema_type in _schema_cache:
        if schema_type.endswith("." + name_part):
            return schema_type

    return None
