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

Запустить Python-скрипт на сервере — он прогоняет реальные алгоритмы и проверяет бизнес-логику результатов:

```bash
sshpass -p "KKHcLF4TEPYLTw9jtjpYJyjA" ssh -o StrictHostKeyChecking=no -p 2224 bda@178.219.166.81 "python3 << 'PYEOF'
import subprocess, json

KEY = 'a7f3b8c9d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0'
MCP = 'http://127.0.0.1:8090/mcp'

def call(query, filter=None, select=None, top=20):
    args = {'organization': '', 'query': query, 'top': top}
    if filter: args['filter'] = filter
    if select: args['select'] = select
    r = subprocess.run(['curl','-s','-X','POST',MCP,
        '-H','Content-Type: application/json',
        '-H','X-Admin-Key: '+KEY,
        '-d',json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
                         'params':{'name':'read_1c','arguments':args}})],
        capture_output=True, text=True)
    val = json.loads(json.loads(r.stdout).get('result',{}).get('content',[{}])[0].get('text','{}'))
    # Возвращаем rows, error, entity
    return val.get('value',[]), val.get('_error'), val.get('_entity','')

def ok(msg): print('  ✓', msg)
def fail(msg): print('  ✗ ПРОБЛЕМА:', msg)

# ── БЛОК 1: Защитные механизмы ───────────────────────────────────────────────
print('=== БЛОК 1: Защитные механизмы ===')

rows, err, entity = call('Какой остаток по деньгам на конец 2024 года')
if entity == '_query_is_user_question':
    ok('Вопрос пользователя как query — заблокирован корректно')
else:
    fail('Вопрос пользователя прошёл как query — защита не работает!')

rows, err, entity = call('AccountingRegister_Хозрасчетный/Balance(Period=datetime\'2024-12-31T00:00:00\')',
                 filter='Account_Key eq guid\'51\'')
if err and '400' in str(err):
    ok('guid=код счёта → HTTP 400 (ожидаемо, пользователь должен использовать реальный UUID)')
else:
    fail('guid=код счёта не вернул ошибку — возможна неверная логика')

# ── БЛОК 2: Остатки по банку (51/52/55) ──────────────────────────────────────
print()
print('=== БЛОК 2: Остатки по банку на 2024-12-31 ===')

refs = {}
for code in ['51','52','55']:
    rows, err, _ = call('ChartOfAccounts_Хозрасчетный', filter=f'Code eq \'{code}\'', select='Ref_Key,Code,Description')
    if err or not rows:
        fail(f'ChartOfAccounts счёт {code}: {err}')
    else:
        refs[code] = {'key': rows[0]['Ref_Key'], 'desc': rows[0].get('Description','?')}
        ok(f'Счёт {code} ({refs[code][\"desc\"]}): {refs[code][\"key\"]}')

rub_total = 0
val_by_currency = {}

for code, acct in refs.items():
    rows, err, _ = call(
        'AccountingRegister_Хозрасчетный/Balance(Period=datetime\'2024-12-31T00:00:00\')',
        filter=f'Account_Key eq guid\'{acct[\"key\"]}\''
    )
    if err:
        fail(f'Balance счёт {code}: {err[:100]}')
        continue

    # Проверка структуры полей
    if rows and 'ExtDimension1' not in rows[0]:
        fail(f'Счёт {code}: нет поля ExtDimension1 — субконто недоступны')
    if rows and 'Субконто1' in rows[0]:
        fail(f'Счёт {code}: обнаружен Субконто1 — этого поля нет в данной конфигурации, инструкция неверна!')

    rub_sum = sum(r.get('СуммаBalance', 0) or 0 for r in rows)
    val_sum = sum(r.get('ВалютнаяСуммаBalance', 0) or 0 for r in rows)

    if code in ('51', '55'):
        if val_sum != 0:
            fail(f'Счёт {code}: ВалютнаяСуммаBalance={val_sum} — неожиданно для рублёвого счёта!')
        rub_total += rub_sum
        ok(f'Счёт {code}: rows={len(rows)} СуммаBalance={rub_sum:,.2f} руб → ВХОДИТ в рублёвый итог')

    elif code == '52':
        # СуммаBalance(52) = рублёвый ЭКВИВАЛЕНТ валюты — НЕ реальные рубли!
        if val_sum == 0 and rub_sum > 0:
            fail(f'Счёт 52: СуммаBalance={rub_sum:,.2f} но ВалютнаяСуммаBalance=0 — аномалия!')
        for row in rows:
            val_k = row.get('Валюта_Key', '')
            val_v = row.get('ВалютнаяСуммаBalance', 0) or 0
            if val_k:
                val_by_currency[val_k] = val_by_currency.get(val_k, 0) + val_v
        ok(f'Счёт 52: rows={len(rows)} рублёвый_эквивалент={rub_sum:,.2f} ВалютнаяСумма={val_sum:,.4f}')
        print(f'         ↳ СуммаBalance(52)={rub_sum:,.2f} НЕ входит в рублёвый итог!')

print()
print(f'  ИТОГ рублёвый остаток (51+55 только): {rub_total:,.2f} руб')
for val_k, val_v in val_by_currency.items():
    print(f'  ИТОГ валюта ({val_k[-12:]}): {val_v:,.4f}')

# ── БЛОК 3: Дебиторская задолженность (62) ───────────────────────────────────
print()
print('=== БЛОК 3: Дебиторка (счёт 62) на 2024-12-31 ===')
rows62, err62, _ = call('ChartOfAccounts_Хозрасчетный', filter='Code eq \'62\'', select='Ref_Key,Description')
if err62 or not rows62:
    fail(f'ChartOfAccounts счёт 62: {err62}')
else:
    ref62 = rows62[0]['Ref_Key']
    rows_b, err_b, _ = call(
        'AccountingRegister_Хозрасчетный/Balance(Period=datetime\'2024-12-31T00:00:00\')',
        filter=f'Account_Key eq guid\'{ref62}\''
    )
    if err_b:
        fail(f'Balance счёт 62: {err_b[:100]}')
    else:
        debit = sum(r.get('СуммаBalanceDr', 0) or 0 for r in rows_b)
        credit = sum(r.get('СуммаBalanceCr', 0) or 0 for r in rows_b)
        ok(f'Счёт 62: rows={len(rows_b)} Дебет={debit:,.2f} Кредит={credit:,.2f}')
        print(f'         ↳ ЭТАЛОН Дебиторка покупателей = {debit - credit:,.2f} руб')

print()
print('Проверка завершена.')
PYEOF
"
```

**Интерпретация результатов:**
- `✓` — норма
- `✗ ПРОБЛЕМА` — нужно разбираться: либо баг в инструкции, либо неожиданные данные в 1С
- Если ИТОГ рублёвого остатка совпадает с тем, что LLM отвечал пользователю — значит ответ был верный
- Если не совпадает — искать расхождение: возможно LLM включил СуммаBalance(52) в рублёвый итог

## Шаг 5 — итог

Кратко: что нашли в логах, что изменили, результаты сутевой проверки, что осталось непонятным.
