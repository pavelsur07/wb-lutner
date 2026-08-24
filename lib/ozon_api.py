"""
lib/ozon_api.py — обёртки над Ozon Seller API.

Base: https://api-seller.ozon.ru
Авторизация: заголовки Client-Id + Api-Key.
Все методы — POST (у Ozon так принято).

Важно:
- Seller API работает по UTC-0 (даты в ответах — UTC).
- /v2/products/stocks: не более 100 пар «товар-склад» за запрос.
- /v2/warehouse/list: обязательны limit и cursor.
"""
from __future__ import annotations

import time

import requests

from lib import config
from lib.logging_setup import get_logger

log = get_logger("ozon_api")

BASE = "https://api-seller.ozon.ru"
TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF = [2, 4, 8]
NO_RETRY_CODES = {400, 401, 403, 404}
STOCKS_BATCH = 100  # жёсткий лимит Ozon


def _headers() -> dict:
    config.require("OZON_CLIENT_ID", "OZON_API_KEY")
    return {
        "Client-Id": str(config.OZON_CLIENT_ID),
        "Api-Key": config.OZON_API_KEY,
        "Content-Type": "application/json",
    }


def _request(path: str, json_body: dict | None = None) -> dict:
    url = BASE + path
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=_headers(),
                                 json=json_body or {}, timeout=TIMEOUT)
            if resp.status_code in NO_RETRY_CODES:
                log.warning("Ozon %s -> %s (no retry): %s",
                            path, resp.status_code, resp.text[:300])
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}")
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) in NO_RETRY_CODES:
                raise
            last_exc = e
        except requests.RequestException as e:
            last_exc = e
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF[attempt])
    raise RuntimeError(f"Ozon {path} failed after retries: {last_exc}")


# --- Склады ---
def warehouse_list() -> list[dict]:
    return _request("/v2/warehouse/list", {"limit": 100, "cursor": ""}).get("warehouses", [])


# --- Товары ---
def product_list(last_id: str = "", limit: int = 1000) -> dict:
    """Постранично. Возвращает result{items[], total, last_id}."""
    body = {"filter": {"visibility": "ALL"}, "limit": limit}
    if last_id:
        body["last_id"] = last_id
    return _request("/v3/product/list", body).get("result", {})


def iter_products():
    """Итератор по всем товарам кабинета (offer_id, product_id, sku)."""
    last_id = ""
    while True:
        res = product_list(last_id=last_id)
        items = res.get("items", [])
        if not items:
            return
        for it in items:
            yield it
        last_id = res.get("last_id") or ""
        if not last_id or len(items) < 1000:
            return


# --- Остатки ---
def update_stocks(stocks: list[dict]) -> dict:
    """
    stocks: [{'offer_id': str, 'stock': int, 'warehouse_id': int}, ...]
    Не более STOCKS_BATCH за вызов — батчинг делает вызывающий код.
    """
    return _request("/v2/products/stocks", {"stocks": stocks})


def stocks_by_warehouse(offer_ids: list[str]) -> dict:
    """Текущие остатки FBS по складам для указанных offer_id."""
    return _request("/v2/product/info/stocks-by-warehouse/fbs",
                    {"offer_id": offer_ids})


# --- Заказы (отправления FBS) ---
def postings_unfulfilled(limit: int = 100, offset: int = 0) -> dict:
    """Необработанные отправления FBS."""
    body = {
        "dir": "ASC",
        "filter": {},
        "limit": limit,
        "offset": offset,
        "with": {"analytics_data": False, "financial_data": False},
    }
    return _request("/v3/posting/fbs/unfulfilled/list", body).get("result", {})


def posting_get(posting_number: str) -> dict:
    return _request("/v3/posting/fbs/get",
                    {"posting_number": posting_number,
                     "with": {"analytics_data": False,
                              "financial_data": False}}).get("result", {})
