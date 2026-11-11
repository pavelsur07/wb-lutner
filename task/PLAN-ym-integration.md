# План: интеграция Яндекс Маркета (третья площадка)

Дата: 11.11.2026 · Статус: **этап 1 задеплоен 11.11.2026 (c79c9e0)**

Решения: один кабинет / один магазин; настройки Маркета — в БД (`system_state`),
ключи WB/Ozon остаются в `.env`; вкладка UI «Настройка» общая (график отгрузок + Маркет).

## Этап 1. Подключение и проверка ✅ задеплоено 11.11.2026

- `lib/ym_api.py`: клиент (`Api-Key`, ретраи на 420/5xx, листание `pageToken`),
  настройки `ym_api_key / ym_business_id / ym_campaign_id / ym_warehouse_id`,
  `check()` → `GET v2/campaigns` + `POST v2/businesses/{id}/warehouses`,
  результат в `ym_check`; один магазин/склад подставляется сам.
- UI `/ui/settings/ym`: форма, «Сохранить и проверить», выбор магазина/склада,
  ключ только маской. CSRF как у графика отгрузок.
- `tests/test_ym_api.py`.

Деплой: только код, миграций нет. После — открыть страницу, вставить ключ, проверить.

## Этап 2. Остатки (`sync_stock_to_ym.py`)

- Метод по результату проверки: у складов есть `groupInfo` →
  `PUT v2/campaigns/{campaignId}/offers/stocks`, иначе
  `POST v3/businesses/{businessId}/offers/stocks/update` (+ warehouseId).
- ≤2000 SKU за запрос, ≤100 000 записей/мин. Правила — `lib/stock_rules.py`.
- Сопоставление `offerId` ↔ артикул Lutner (уточнить: совпадает ли offerId с артикулом).
- Первый прогон — на скрытых товарах (`hidden-offers`).

## Этап 3. Заказы (`ym_main.py`)

- Опрос `POST v1/businesses/{businessId}/orders`, фильтр `PROCESSING/STARTED`,
  ≤50 на страницу, диапазон ≤30 дней. Push API Маркета отключается — только опрос.
- Таблица `ym_orders` по образцу `ozon_orders`, создание заказа в Lutner, вкладка UI.
- Дата отгрузки — `lib/shipping.py` (добавить `ym` в `MARKETS`).
