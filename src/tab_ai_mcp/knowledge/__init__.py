from tab_ai_mcp.knowledge import accounting, unf, erp, zup

# Карта: имя конфигурации (из config_detector) → модуль знаний
KNOWLEDGE_MAP = {
    "1С:Бухгалтерия предприятия":          accounting,
    "1С:Управление нашей фирмой":           unf,
    "1С:ERP Управление предприятием":       erp,
    "1С:Управление торговлей":              erp,
    "1С:Комплексная автоматизация":         erp,
    "1С:Зарплата и управление персоналом":  zup,
}
