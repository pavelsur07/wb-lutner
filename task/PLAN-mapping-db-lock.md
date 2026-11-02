# План: убрать `database is locked` из-за initial_mapping / ozon_mapping

Дата: 02.11.2026 · Статус: **задеплоено 02.11.2026 18:05 (4165db2), идёт приёмка 24 ч**

## 1. Проблема

На проде во всех скриптах регулярно появляется `sqlite3.OperationalError: database is locked`:
webhook, `sync_stock_to_wb`, `sync_stock_to_ozon`, `main`, `ozon_main`, `sync_catalog`.

Последствия за 02.11:
- 17:47 — webhook Lutner откатил обновление остатков, gunicorn-воркер перезапущен;
- 12:30 и 17:50 — `sync_stock_to_wb` упал на `set_state`, сломан heartbeat-маркер;
- 17:50 — WB-заказ 6181118483 создан в Lutner с задержкой (`main` ждал блокировку).

## 2. Причина

`initial_mapping.py:50-79` (и так же `ozon_mapping.py:25-51`):

```python
for card in _iter_cards():          # генератор: на каждой странице HTTP к WB
    conn.execute("INSERT ...")      # первый INSERT неявно открывает write-транзакцию
conn.commit()                       # блокировка снимается только после последней страницы
```

Пока скрипт листает API, write-lock держится всё это время: обычно ~1,5 мин, 02.11 в 17:45 — ~5,5 мин.
У всех остальных `busy_timeout=30s`, поэтому они падают.
WAL и `busy_timeout` уже включены (`lib/db.py:86-91`), значит дело не в настройках.

Подтверждение по логам прода: каждая блокировка приходится на окно
«старт `initial_mapping` (кратно 15 мин) → его лог `mapping: matched=…`».

## 3. Решение

Разделить **сбор** и **запись**:

1. Выкачать все карточки/товары из API в память. Между HTTP-запросами БД не трогаем,
   разрешено только чтение из `catalog` (чтение в WAL не блокирует писателей).
2. Посчитать список строк для upsert.
3. Записать их одной короткой транзакцией (`executemany` + `commit`): миллисекунды.

Сбой API на любой странице — исключение до записи, mapping не меняется (как и сейчас).
Объём небольшой: ~1 200 карточек WB, ~700 товаров Ozon, память не проблема.

**Вне рамок задачи** (отдельные тикеты):
- Ozon `K65 — Product not found` каждые 10 мин;
- webhook `bad item skipped` (товар `2cb3d164-…`);
- ретрай записи в webhook при блокировке, `gunicorn --timeout 60`;
- ротация `logs/cron_*.log` (до 11 МБ).

## 4. Шаги (TDD)

| # | Шаг | Результат |
|---|-----|-----------|
| 1 | **RED.** `tests/test_mapping_lock.py`: в момент запроса 2-й страницы API другое соединение пытается взять `BEGIN IMMEDIATE` с `timeout=0` | ✅ сделано: 2 теста на блокировку падают, 5 регрессионных проходят |
| 2 | **GREEN.** `initial_mapping.run()`: сначала `cards = list(_iter_cards())`, затем цикл сопоставления, затем один upsert и `commit` | тесты WB зелёные |
| 3 | **GREEN.** То же в `ozon_mapping.run()` с `list(ozon_api.iter_products())` | тесты Ozon зелёные |
| 4 | **REFACTOR.** Не менять логи, `print` и коды возврата: `daily_summary` и UI на них опираются | все 7 тестов зелёные |
| 5 | `venv/bin/python -m py_compile initial_mapping.py ozon_mapping.py` | без ошибок |
| 6 | Коммит, деплой `./deploy.sh wb-prod` (rsync + рестарт webhook) | `systemctl is-active` = active |
| 7 | Наблюдение на проде 24 ч | см. критерии ниже |

## 5. Тесты

Файл: `tests/test_mapping_lock.py`, только stdlib `unittest`, без новых зависимостей.
Запуск:

```bash
venv/bin/python -m unittest tests.test_mapping_lock -v
```

| Тест | Что проверяет | До фикса | После |
|------|---------------|---------|-------|
| `WbMappingTest.test_no_write_lock_while_fetching_next_page` | во время HTTP за 2-й страницей WB БД свободна на запись | ❌ FAIL | ✅ |
| `OzonMappingTest.test_no_write_lock_while_fetching_next_page` | то же для Ozon | ❌ FAIL | ✅ |
| `WbMappingTest.test_mapping_content` | связка по артикулу; пропуск карточек без артикула, без баркода и с артикулом не из каталога | ✅ | ✅ |
| `WbMappingTest.test_upsert_updates_existing` | повторный прогон обновляет связку, а не дублирует | ✅ | ✅ |
| `WbMappingTest.test_api_failure_midway_keeps_mapping_untouched` | сбой WB на 2-й странице: mapping не меняется частично | ✅ | ✅ |
| `OzonMappingTest.test_mapping_content` | связка по offer_id, пропуск пустых и чужих артикулов | ✅ | ✅ |
| `OzonMappingTest.test_api_failure_midway_keeps_mapping_untouched` | атомарность для Ozon | ✅ | ✅ |

Тесты используют временную SQLite (WAL, реальная схема из `lib/db.py` и `migrate_ozon.py`),
API замоканы на уровне `wb_api.cards_list` / `ozon_api.product_list`, так что реальная пагинация тоже проверяется.

## 6. Чек-лист готовности (Definition of Done)

**Код**
- [x] `initial_mapping.py`: нет записи в БД между вызовами `wb_api.cards_list`
- [x] `ozon_mapping.py`: нет записи в БД между вызовами `ozon_api.product_list`
- [x] Запись mapping — одна транзакция, `commit` после неё
- [x] Логи (`mapping: matched=… no_article=… no_barcode=…`), `print` и код возврата не изменились
- [x] Новых зависимостей нет

**Тесты**
- [x] Тесты написаны и падают по нужной причине (RED)
- [x] `venv/bin/python -m unittest tests.test_mapping_lock -v`: 7/7 OK
- [x] Повторный запуск тестов стабилен (без flaky)

**Деплой**
- [x] Коммит в `master` (4165db2)
- [x] `./deploy.sh wb-prod` прошёл, `wb-lutner-webhook` active
- [x] md5 `initial_mapping.py` / `ozon_mapping.py` на проде = локальным
- [x] Ручной прогон на проде от `wblutner`: `venv/bin/python initial_mapping.py` → matched ≈ 370–390, как раньше (18:08: WB 370, Ozon 324; тесты на проде 7/7 OK)

**Приёмка на проде (24 ч после деплоя)**
- [ ] `database is locked` = 0 во всех `logs/*.log` с момента деплоя
- [ ] `webhook processing error` = 0
- [ ] `initial_mapping`: число `matched` не упало относительно прошлых суток
- [ ] Остатки уходят в WB и Ozon каждые 10 мин (`pushed … stocks`)
- [ ] Новые заказы WB/Ozon создаются в Lutner без задержки на блокировку

Команда проверки:

```bash
ssh wb-prod 'cd /opt/wb-lutner/logs && awk "/^20[0-9][0-9]-/{d=\$1\" \"\$2} /database is locked/{print d}" *.log | sort | tail'
```

## 7. Откат

`git revert <commit>` → `./deploy.sh wb-prod`. Схема БД не меняется, данные не мигрируются, откат безопасен.
