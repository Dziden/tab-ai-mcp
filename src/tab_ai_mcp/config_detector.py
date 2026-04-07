"""
Определение конфигурации 1С по составу метаданных OData.

Каждая конфигурация имеет характерные типы объектов — по ним
определяем что именно установлено и загружаем нужные знания.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Сигнатуры конфигураций: набор типов, уникальных для данного продукта.
# Чем больше совпадений — тем выше вероятность. Берём конфигурацию с максимальным счётом.

_SIGNATURES: list[tuple[str, list[str]]] = [
    ("1С:ERP Управление предприятием", [
        "Document_ЗаказНаПроизводство",
        "Document_ВыпускПродукции",
        "AccumulationRegister_ВыпускПродукцииПоПроизводственнымЗаказам",
        "Document_ПланПроизводства",
        "AccumulationRegister_БюджетДвиженияДенежныхСредств",
        "Document_ЗаказМежфилиальнойПередачи",
        "AccumulationRegister_НезавершенноеПроизводствоПоПодразделениям",
    ]),
    ("1С:Управление торговлей", [
        "Document_ЗаказКлиента",
        "Document_ЗаказПоставщику",
        "AccumulationRegister_ДолгКлиентов",
        "AccumulationRegister_ДолгПоставщиков",
        "AccumulationRegister_СвободныеОстатки",
        "Document_ПриходныйОрдерНаТовары",
        "Document_РасходныйОрдерНаТовары",
    ]),
    ("1С:Управление нашей фирмой", [
        "Document_ЗаказПокупателя",
        "Document_ЗаказПоставщику",
        "AccumulationRegister_Продажи",
        "AccumulationRegister_Закупки",
        "Document_СчетНаОплатуПокупателю",
        "AccumulationRegister_ДенежныеСредства",
        "Document_ПриходнаяНакладная",
    ]),
    ("1С:Зарплата и управление персоналом", [
        "Document_НачислениеЗарплатыИВзносов",
        "Document_ВедомостьНаВыплатуЗарплаты",
        "AccumulationRegister_НачисленияУдержанияПоСотрудникам",
        "Catalog_ФизическиеЛица",
        "Catalog_Сотрудники",
        "Document_ПриемНаРаботу",
        "Document_УвольнениеСотрудников",
    ]),
    ("1С:Розница", [
        "Document_ЧекККМ",
        "Document_ВозвратЧекаККМ",
        "AccumulationRegister_ВыручкаИСебестоимостьПродаж",
        "Catalog_Кассы",
        "Document_ОтчетОРозничныхПродажах",
    ]),
    ("1С:Бухгалтерия предприятия", [
        "AccountingRegister_Хозрасчетный",
        "ChartOfAccounts_Хозрасчетный",
        "Document_ПоступлениеТоваровУслуг",
        "Document_РеализацияТоваровУслуг",
        "Document_ПоступлениеНаРасчетныйСчет",
        "Document_СписаниеСРасчетногоСчета",
        "Document_ФормированиеЗаписейКнигиПокупок",
    ]),
    ("1С:Комплексная автоматизация", [
        "AccountingRegister_Хозрасчетный",
        "Document_ЗаказКлиента",
        "Document_ЗаказПоставщику",
        "AccumulationRegister_СвободныеОстатки",
        "Document_ВыпускПродукции",
        "AccumulationRegister_БухгалтерскиеРасчеты",
    ]),
]


@dataclass
class DetectedConfig:
    name: str
    confidence: int   # количество совпавших сигнатур
    all_types: set[str]


def detect(entity_types: list[str]) -> DetectedConfig:
    """Определить конфигурацию по списку OData типов."""
    type_set = set(entity_types)
    best_name = "1С (неизвестная конфигурация)"
    best_score = 0

    for config_name, signatures in _SIGNATURES:
        score = sum(1 for s in signatures if s in type_set)
        if score > best_score:
            best_score = score
            best_name = config_name

    return DetectedConfig(name=best_name, confidence=best_score, all_types=type_set)
