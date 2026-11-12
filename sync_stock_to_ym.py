"""
sync_stock_to_ym.py — пушит store_spb (склад Lutner СПб) в остатки Яндекс Маркета.

Шлём ТОЛЬКО сопоставленные товары из ym_mapping (offerId = артикул Lutner).
Каталог кабинета общий на несколько магазинов — чужие товары не трогаем даже нулём.
Остаток — как у WB/Ozon: lib/stock_rules.sellable (<MIN -> 0, >MAX -> MAX).
Нет записи в stock -> 0. Метод v3 (кабинет без групп складов), батч 2000.

Флаги:
  --dry-run          ничего не отправлять, показать что ушло бы
  --only ARTICLE     работать только с одним товаром (для теста)

Cron: */10 * * * * flock -n /tmp/ym-stock.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_stock_to_ym.py'
"""
from __future__ import annotations

import argparse
import sys

from lib import db, ym_api
from lib.logging_setup import get_logger
from lib.mailer import alert
from lib.stock_rules import sellable

log = get_logger("sync_stock_to_ym")


def run(dry_run: bool = False, only: str | None = None) -> int:
    s = ym_api.settings()
    if not (s["business_id"] and s["warehouse_id"]):
        log.error("не заданы businessId / warehouseId (UI: Настройка → Яндекс Маркет)")
        return 1
    warehouse_id = int(s["warehouse_id"])

    conn = db.get_conn()
    sql = """SELECT m.offer_id AS offer_id, COALESCE(st.store_spb, 0) AS amount
             FROM ym_mapping m LEFT JOIN stock st ON st.uuid = m.lutner_uuid
             WHERE m.lutner_uuid IS NOT NULL"""
    params: tuple = ()
    if only:
        sql += " AND m.offer_id = ?"
        params = (only,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = [{"sku": r["offer_id"], "partnerWarehouseId": warehouse_id,
              "count": sellable(r["amount"])} for r in rows]
    if not items:
        log.info("нечего пушить (ym_mapping без сопоставленных%s)",
                 f", фильтр --only {only}" if only else "")
        return 0

    if dry_run:
        log.info("[dry-run] отправил бы %s позиций на склад %s. Первые 10: %s",
                 len(items), warehouse_id, items[:10])
        return 0

    try:
        for i in range(0, len(items), ym_api.STOCKS_BATCH):
            ym_api.update_stocks(s["business_id"], items[i:i + ym_api.STOCKS_BATCH])
    except Exception as e:  # noqa: BLE001
        log.exception("ym stock push failed")
        alert("sync_stock_to_ym FAILED", f"Пуш остатков в Яндекс Маркет упал:\n{e}")
        return 1

    log.info("pushed %s stocks to YM warehouse %s", len(items), warehouse_id)
    if not only:
        db.set_state("last_stock_to_ym", "ok")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не пушить, только показать что отправил бы")
    ap.add_argument("--only", metavar="ARTICLE",
                    help="работать только с одним offerId (для теста)")
    args = ap.parse_args()
    sys.exit(run(args.dry_run, args.only))
