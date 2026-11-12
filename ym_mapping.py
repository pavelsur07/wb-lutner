"""
ym_mapping.py — наполняет ym_mapping. Ключ связки — АРТИКУЛ:
offerId товара в Маркете = article в каталоге Lutner (как у Ozon).

Каталог Маркета общий на все магазины кабинета, поэтому в нём есть и чужие
товары (другие бренды) — они остаются с пустым lutner_uuid и в остатки не пойдут.

Сначала всё выкачивается из API, потом таблица целиком заменяется одной
короткой транзакцией. Сбой API или пустой каталог — таблицу не трогаем.
Остатки на Маркет НЕ отправляет (это sync_stock_to_ym.py, этап 2б).

Запуск: venv/bin/python ym_mapping.py   (или кнопка в UI: Настройка → Маппинг Яндекс)
"""
from __future__ import annotations

import sys

from lib import db, ym_api
from lib.logging_setup import get_logger

log = get_logger("ym_mapping")


def _fit(stock_rows: list[dict]) -> int | None:
    """Годный остаток (FIT) из ответа v3/offers/stocks; None — остаток не передавался."""
    counts = {s.get("type"): s.get("count") for s in stock_rows or []}
    return counts.get("FIT", counts.get("AVAILABLE"))


def run(retries: int = ym_api.MAX_RETRIES) -> dict:
    s = ym_api.settings()
    if not (s["business_id"] and s["campaign_id"] and s["warehouse_id"]):
        raise ym_api.YMError("не заданы businessId / campaignId / warehouseId "
                             "(UI: Настройка → Яндекс Маркет)")

    offers = ym_api.offer_mappings(s["business_id"], retries=retries)
    if not offers:
        raise ym_api.YMError("Маркет вернул пустой каталог — маппинг не тронут")
    ym_stock = {o.get("offerId"): _fit(o.get("stocks"))
                for o in ym_api.stocks(s["business_id"], s["warehouse_id"], retries=retries)}

    conn = db.get_conn()
    try:
        articles = {r["article"]: r["uuid"] for r in
                    conn.execute("SELECT uuid, article FROM catalog WHERE article IS NOT NULL")}
        rows = []
        for m in offers:
            o = m.get("offer") or {}
            oid = (o.get("offerId") or "").strip()
            if not oid:
                continue
            status = next((c.get("status") for c in o.get("campaigns") or []
                           if str(c.get("campaignId")) == s["campaign_id"]), None)
            uuid = articles.get(oid)
            rows.append((oid, o.get("name"), o.get("vendor"), status, ym_stock.get(oid),
                         uuid, oid if uuid else None))

        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ym_mapping")
        conn.executemany("INSERT OR REPLACE INTO ym_mapping VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()

    res = {"total": len(rows), "matched": sum(1 for r in rows if r[5])}
    db.set_state("ym_mapping_at", "ok")
    log.info("ym_mapping: товаров %(total)s, сопоставлено %(matched)s", res)
    return res


if __name__ == "__main__":
    try:
        print(run())
    except ym_api.YMError as e:
        log.error("ym_mapping failed: %s", e)
        sys.exit(1)
