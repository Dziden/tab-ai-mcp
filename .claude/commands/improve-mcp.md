# Сессия улучшения tab_mcp по логам

Прочитай логи с сервера, проанализируй проблемы и примени улучшения.

## Данные для подключения

- Сервер: `178.219.166.81:2224`, логин `bda`
- tab_mcp слушает на `127.0.0.1:8090` внутри сервера
- LOG_API_KEY: `a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0`

## Шаг 1 — получить логи

```bash
sshpass -p "KKHcLF4TEPYLTw9jtjpYJyjA" ssh -o StrictHostKeyChecking=no -p 2224 bda@178.219.166.81 \
  "curl -s 'http://127.0.0.1:8090/logs?api_key=a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0&last=200'"
```

## Шаг 2 — анализ логов

Для каждой записи обращай внимание на:

- `error` не null — что пошло не так, это ошибка резолва, OData, или логика?
- `duration_ms` > 5000 — медленные запросы, возможен таймаут
- `resolved` отличается от `entity_type` — правильно ли резолвится запрос?
- `expand` отсутствует в params когда ожидается (Balance-запросы без Субконто)
- Повторяющиеся одинаковые запросы — дубликаты в рамках одной сессии
- Паттерны ошибок: 404 (неверное имя), 400 (неверное поле/фильтр), пустой error (таймаут)

## Шаг 3 — применить улучшения

Файлы для правки:
- `src/tab_ai_mcp/knowledge/accounting.py` — инструкции и промпты для Бухгалтерии
- `src/tab_ai_mcp/knowledge/unf.py` — для УНФ
- `src/tab_ai_mcp/knowledge/erp.py` — для ERP
- `src/tab_ai_mcp/server.py` — описание инструментов read_1c / write_1c

После правок — коммит и пуш в main (деплой ручной).

## Шаг 4 — сутевая проверка запросов

Выполнить прямые вызовы `read_1c` через JSON-RPC (минуя LLM) и проверить корректность ответов.

```bash
KEY="a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
MCP="http://127.0.0.1:8090/mcp"

call() {
  local desc="$1"; local args="$2"
  echo "=== $desc ==="
  curl -s -X POST "$MCP" \
    -H "Content-Type: application/json" -H "X-Admin-Key: $KEY" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"read_1c\",\"arguments\":$args}}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
content=d.get('result',{}).get('content',[{}])
text=content[0].get('text','') if content else ''
try:
    val=json.loads(text)
    rows=val.get('value',[])
    err=val.get('_error')
    entity=val.get('_entity','')
    print('FAIL entity=%s error=%s'%(entity,err) if err else 'OK rows=%d entity=%s'%(len(rows),entity))
    if rows: print('  keys:',list(rows[0].keys())[:6])
except: print('RAW:',text[:300])
"
  echo ""
}

# T1: ChartOfAccounts с select=Ref_Key → OK rows=1, key=['Ref_Key']
call "T1: ChartOfAccounts 51 с select=Ref_Key" \
  '{"organization":"","query":"ChartOfAccounts_Хозрасчетный","filter":"Code eq '"'"'51'"'"'","select":"Ref_Key","top":1}'

# T2: Получить реальный GUID счёта 51
REF51=$(curl -s -X POST "$MCP" -H "Content-Type: application/json" -H "X-Admin-Key: $KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"read_1c","arguments":{"organization":"","query":"ChartOfAccounts_Хозрасчетный","filter":"Code eq '"'"'51'"'"'","select":"Ref_Key","top":1}}}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.loads(d['result']['content'][0]['text'])['value'][0]['Ref_Key'])" 2>/dev/null)
echo "=== Ref_Key счёта 51: $REF51 ==="
echo ""

# T3: Balance на 2024-12-31 с правильным GUID → OK rows>0
call "T3: Balance 2024-12-31 с правильным guid" \
  "{\"organization\":\"\",\"query\":\"AccountingRegister_Хозрасчетный/Balance(Period=datetime'2024-12-31T00:00:00')\",\"filter\":\"Account_Key eq guid'$REF51'\",\"top\":10}"

# T4: Balance с кодом вместо GUID → FAIL HTTP 400 (это ожидаемо — защита работает)
call "T4: Balance с guid='51' (ожидаем HTTP 400)" \
  '{"organization":"","query":"AccountingRegister_Хозрасчетный/Balance(Period=datetime'"'"'2024-12-31T00:00:00'"'"')","filter":"Account_Key eq guid'"'"'51'"'"'","top":1}'

# T5: Вопрос пользователя как query → FAIL _query_is_user_question (защита работает)
call "T5: вопрос пользователя как query (ожидаем ОШИБКУ ВЫЗОВА)" \
  '{"organization":"","query":"Какой остаток по деньгам на конец 2024 года"}'

# T6: Balance счёта 62 на 2024-12-31 → проверка дебиторки
call "T6: Balance счёта 62 (дебиторка) на 2024-12-31" \
  "{\"organization\":\"\",\"query\":\"AccountingRegister_Хозрасчетный/Balance(Period=datetime'2024-12-31T00:00:00')\",\"filter\":\"Account_Key eq guid'$(curl -s -X POST "$MCP" -H "Content-Type: application/json" -H "X-Admin-Key: $KEY" -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"read_1c","arguments":{"organization":"","query":"ChartOfAccounts_Хозрасчетный","filter":"Code eq '"'"'62'"'"'","select":"Ref_Key","top":1}}}' | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.loads(d['result']['content'][0]['text'])['value'][0]['Ref_Key'])" 2>/dev/null)'\",\"top\":5}"
```

**Интерпретация результатов:**
- T1, T3, T6 → должны быть `OK rows>0`
- T4 → должен быть `FAIL HTTP 400` (это норма — значит защита от guid-кода работает)
- T5 → должен быть `FAIL _query_is_user_question` (норма — защита от вопроса работает)
- Если T3 возвращает `OK rows=0` — нет данных на эту дату в базе (не баг)
- Если T1 даёт `FAIL ReadTimeout` — проблема с 1С, а не с MCP

## Шаг 5 — итог

Кратко: что нашли в логах, что изменили, результаты сутевой проверки, что осталось непонятным.
