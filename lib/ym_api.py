"""
lib/ym_api.py — Яндекс Маркет Partner API + настройки подключения.

Base: https://api.partner.market.yandex.ru, авторизация заголовком Api-Key.

Настройки кабинета (один кабинет, один магазин) хранятся в system_state, а не в .env —
их задают в UI /ui/settings/ym:
    ym_api_key, ym_business_id, ym_campaign_id, ym_warehouse_id
    ym_check — JSON последней проверки подключения.

Важно:
- Превышение лимита у Маркета — код 420 (не 429).
- Склады: POST v2/businesses/{id}/warehouses (GET-версия отключается 05.04.2027).
  groupInfo у склада => в кабинете группы складов => остатки слать через
  PUT v2/campaigns/{id}/offers/stocks, иначе POST v3/businesses/{id}/offers/stocks/update.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from lib import db
from lib.logging_setup import get_logger

log = get_logger("ym_api")

BASE = "https://api.partner.market.yandex.ru"
TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF = [2, 4, 8]
RETRY_CODES = {420, 500, 502, 503, 504}

FIELDS = ("api_key", "business_id", "campaign_id", "warehouse_id")


# --- Настройки ---------------------------------------------------------------

def settings() -> dict:
    return {f: db.get_state(f"ym_{f}") or "" for f in FIELDS}


def save_settings(**values: str) -> None:
    for f, v in values.items():
        if f not in FIELDS:
            raise ValueError(f"неизвестная настройка: {f}")
        db.set_state(f"ym_{f}", (v or "").strip())


def mask(key: str) -> str:
    return "••••" + key[-4:] if key else ""


def last_check() -> dict:
    try:
        return json.loads(db.get_state("ym_check") or "{}")
    except ValueError:
        return {}


# --- HTTP --------------------------------------------------------------------

class YMError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _request(method: str, path: str, *, params: dict | None = None,
             json_body: dict | None = None, retries: int = MAX_RETRIES) -> dict:
    key = settings()["api_key"]
    if not key:
        raise YMError("Api-Key не задан (UI: Настройка → Яндекс Маркет)")
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, BASE + path, params=params, json=json_body,
                                    headers={"Api-Key": key}, timeout=TIMEOUT)
        except requests.RequestException as e:
            status, text = 0, str(e)
        else:
            if resp.ok:
                return resp.json() if resp.content else {}
            status, text = resp.status_code, resp.text[:300]
        log.warning("YM %s %s -> %s (попытка %s): %s",
                    method, path, status, attempt + 1, text)
        if status and status not in RETRY_CODES:
            break
        if attempt < retries:
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise YMError(f"YM {method} {path} -> {status or 'нет связи'}: {text}", status)


def _pages(method: str, path: str, items, *, limit: int,
           json_body: dict | None = None, retries: int = MAX_RETRIES) -> list[dict]:
    """Листает pageToken/nextPageToken; items(resp) -> список со страницы."""
    out, token = [], None
    while True:
        params = {"limit": limit, **({"pageToken": token} if token else {})}
        resp = _request(method, path, params=params, json_body=json_body, retries=retries)
        out += items(resp)
        prev, token = token, ((resp.get("result") or resp).get("paging") or {}).get("nextPageToken")
        if not token or token == prev:
            return out


def campaigns(retries: int = MAX_RETRIES) -> list[dict]:
    return _pages("GET", "/v2/campaigns", lambda r: r.get("campaigns", []),
                  limit=100, retries=retries)


def warehouses(business_id, campaign_id=None, retries: int = MAX_RETRIES) -> list[dict]:
    body = {"campaignIds": [int(campaign_id)]} if campaign_id else {}
    return _pages("POST", f"/v2/businesses/{int(business_id)}/warehouses",
                  lambda r: (r.get("result") or {}).get("warehouses", []),
                  limit=30, json_body=body, retries=retries)


def offer_mappings(business_id, retries: int = MAX_RETRIES) -> list[dict]:
    """Весь каталог кабинета (общий на все магазины). Доступ: offers-and-cards-management."""
    return _pages("POST", f"/v2/businesses/{int(business_id)}/offer-mappings",
                  lambda r: (r.get("result") or {}).get("offerMappings", []),
                  limit=200, json_body={}, retries=retries)


def stocks(business_id, warehouse_id, retries: int = MAX_RETRIES) -> list[dict]:
    """Остатки на складе (кабинет без групп складов). Доступ: offers-and-cards-management."""
    return _pages("POST", f"/v3/businesses/{int(business_id)}/offers/stocks",
                  lambda r: (r.get("result") or {}).get("offers", []),
                  limit=100, json_body={"partnerWarehouseId": int(warehouse_id)},
                  retries=retries)


# --- Проверка подключения ----------------------------------------------------

_HINTS = {
    401: "ключ не принят — проверьте Api-Key",
    403: "ключ неверный или у него нет доступа (нужен «Все методы» или "
         "«Обработка заказов и учёт товаров»)",
    404: "кабинет не найден — проверьте businessId",
    420: "превышен лимит запросов Маркета — повторите через минуту",
}


def check() -> dict:
    """
    Проверяет ключ и доступ к кабинету, сохраняет результат в ym_check.
    Если магазин/склад один, а в настройках пусто — подставляет сам.
    Без ретраев: вызывается из UI, ждать минуту на 420 там нельзя.
    """
    res = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "ok": False, "error": "", "warnings": [], "campaigns": [], "warehouses": []}
    try:
        res["campaigns"] = [{
            "id": c.get("id"),
            "name": c.get("domain") or "",
            "business_id": (c.get("business") or {}).get("id"),
            "business_name": (c.get("business") or {}).get("name") or "",
            "placement": c.get("placementType") or "",
            "api": c.get("apiAvailability") or "",
        } for c in campaigns(retries=0)]
        s = settings()
        if not s["campaign_id"] and len(res["campaigns"]) == 1:
            c = res["campaigns"][0]
            save_settings(business_id=str(c["business_id"]), campaign_id=str(c["id"]))
            s = settings()

        camp = next((c for c in res["campaigns"] if str(c["id"]) == s["campaign_id"]), None)
        if s["campaign_id"] and not camp:
            res["warnings"].append(f"магазин {s['campaign_id']} не найден среди доступных ключу")
        if camp and camp["api"] != "AVAILABLE":
            res["warnings"].append(f"API магазина недоступно: {camp['api']}")
        if camp and str(camp["business_id"]) != s["business_id"]:
            res["warnings"].append(
                f"businessId магазина — {camp['business_id']}, а в настройках {s['business_id'] or 'пусто'}")

        if s["business_id"]:
            res["warehouses"] = [{
                "id": w.get("id"),
                "name": w.get("name") or "",
                "campaign_id": w.get("campaignId"),
                "group": (w.get("groupInfo") or {}).get("name") or "",
            } for w in warehouses(s["business_id"], s["campaign_id"] or None, retries=0)]
            if not s["warehouse_id"] and len(res["warehouses"]) == 1:
                save_settings(warehouse_id=str(res["warehouses"][0]["id"]))
                s = settings()
            if s["warehouse_id"] and not any(
                    str(w["id"]) == s["warehouse_id"] for w in res["warehouses"]):
                res["warnings"].append(f"склад {s['warehouse_id']} не найден у магазина")
        res["ok"] = True
    except YMError as e:
        res["error"] = f"{_HINTS[e.status]} ({e})" if e.status in _HINTS else str(e)
    db.set_state("ym_check", json.dumps(res, ensure_ascii=False))
    return res
