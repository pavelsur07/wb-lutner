"""
lib/wb_api.py — обёртки над Wildberries API.

Marketplace API: https://marketplace-api.wildberries.ru
Content API:     https://content-api.wildberries.ru
Авторизация: заголовок Authorization: <JWT-токен>

Retry/таймауты — аналогично lutner_api. WB отдаёт 429 при превышении лимитов —
на него ретраим с бэкоффом.
"""
from __future__ import annotations

import time

import requests

from lib import config
from lib.lutner_api import RateLimiter
from lib.logging_setup import get_logger

log = get_logger("wb_api")

TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF = [2, 4, 8]
NO_RETRY_CODES = {400, 401, 403, 404}

_limiter = RateLimiter(max_calls=30, period=60)  # консервативно


def _headers() -> dict:
    config.require("WB_API_TOKEN")
    return {"Authorization": config.WB_API_TOKEN, "Content-Type": "application/json"}


def _request(method: str, base: str, path: str, *, params=None, json_body=None):
    url = base.rstrip("/") + path
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        _limiter.acquire()
        try:
            resp = requests.request(
                method, url, headers=_headers(), params=params,
                json=json_body, timeout=TIMEOUT,
            )
            if resp.status_code in NO_RETRY_CODES:
                log.warning("WB %s %s -> %s (no retry): %s",
                            method, path, resp.status_code, resp.text[:300])
                resp.raise_for_status()
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}")
            resp.raise_for_status()
            if not resp.content:
                return {}
            ctype = resp.headers.get("Content-Type", "")
            return resp.json() if "json" in ctype else resp.content
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", 0) in NO_RETRY_CODES:
                raise
            last_exc = e
        except requests.RequestException as e:
            last_exc = e
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF[attempt])
    raise RuntimeError(f"WB {method} {path} failed after retries: {last_exc}")


# --- Marketplace: заказы ---
def orders_new() -> dict:
    return _request("GET", config.WB_API_BASE, "/api/v3/orders/new")


def orders_status(order_ids: list[int]) -> dict:
    return _request("POST", config.WB_API_BASE, "/api/v3/orders/status",
                    json_body={"orders": order_ids})


def confirm_order(order_id: int) -> dict:
    # Модель DBW (Delivery by Wildberries) — с 2025-11 вынесена в отдельный путь.
    # Старый /api/v3/orders/{id}/confirm отключён (404 PLUG-404-20251118).
    return _request("PATCH", config.WB_API_BASE, f"/api/v3/dbw/orders/{order_id}/confirm")


def cancel_order(order_id: int) -> dict:
    return _request("PATCH", config.WB_API_BASE, f"/api/v3/dbw/orders/{order_id}/cancel")


def stickers(order_ids: list[int], sticker_type="png", width=58, height=40):
    """Стикеры-этикетки. type: svg|png|zplv|zplh. Возвращает JSON со стикерами."""
    return _request(
        "POST", config.WB_API_BASE, "/api/v3/orders/stickers",
        params={"type": sticker_type, "width": width, "height": height},
        json_body={"orders": order_ids},
    )


# --- Marketplace: остатки ---
def update_stocks(warehouse_id: str, stocks: list[dict]) -> dict:
    """stocks: [{'sku': barcode, 'amount': n}, ...]"""
    return _request("PUT", config.WB_API_BASE, f"/api/v3/stocks/{warehouse_id}",
                    json_body={"stocks": stocks})


# --- Content: карточки ---
def cards_list(cursor=None, limit=100) -> dict:
    body = {"settings": {"cursor": {"limit": limit}, "filter": {"withPhoto": -1}}}
    if cursor:
        body["settings"]["cursor"].update(cursor)
    return _request("POST", config.WB_CONTENT_API_BASE,
                    "/content/v2/get/cards/list", json_body=body)
