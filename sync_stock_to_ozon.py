"""
sync_stock_to_ozon.py — пушит store_spb (склад Lutner СПб) в остатки Ozon.

Ozon заводит связь товар<->склад автоматически при первой отправке остатка,
поэтому фильтр «привязан ли к складу» не нужен (в отличие от WB).

Флаги:
  --dry-run          ничего не отправлять, показать что ушло бы
  --only ARTICLE     работать только с одним товаром (для теста)

Нет записи в stock -> остаток 0. Батч 100 (жёсткий лимит Ozon).

Cron: */10 * * * * flock -n /tmp/ozon-stock.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_stock_to_ozon.py'
"""
from __future__ import annotations

import argparse
import sys

from lib import config, db, ozon_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_stock_to_ozon")
BATCH = 100  # лимит Ozon: 100 пар «товар-склад» за запрос


def run(dry_run: bool = False, only: str | None = None) -> int:
    config.require("OZON_WAREHOUSE_ID")
    warehouse_id = int(config.OZON_WAREHOUSE_ID)

    conn = db.get_conn()
    sql = """SELECT m.offer_id AS offer_id, COALESCE(s.store_spb,0) AS amount
             FROM ozon_mapping m LEFT JOIN stock s ON s.uuid = m.lutner_uuid"""
    params: tuple = ()
    if only:
        sql += " WHERE m.offer_id = ?"
        params = (only,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    stocks = [
        # отрицательные остатки Lutner обрезаем нулём (см. sync_stock_to_wb)
        {"offer_id": r["offer_id"], "stock": max(0, int(r["amount"])),
         "warehouse_id": warehouse_id}
        for r in rows if r["offer_id"]
    ]
    if not stocks:
        log.info("нечего пушить (ozon_mapping пуст%s)",
                 f", фильтр --only {only}" if only else "")
        return 0

    if dry_run:
        log.info("[dry-run] отправил бы %s позиций на склад %s. Первые 10: %s",
                 len(stocks), warehouse_id, stocks[:10])
        return 0

    pushed, failed = 0, []
    try:
        for i in range(0, len(stocks), BATCH):
            chunk = stocks[i:i + BATCH]
            resp = ozon_api.update_stocks(chunk)
            for item in (resp.get("result") or []):
                if item.get("updated"):
                    pushed += 1
                else:
                    failed.append({"offer_id": item.get("offer_id"),
                                   "errors": item.get("errors")})
    except Exception as e:  # noqa: BLE001
        log.exception("ozon stock push failed")
        alert("sync_stock_to_ozon FAILED", f"Пуш остатков в Ozon упал:\n{e}")
        return 1

    log.info("pushed %s stocks to Ozon warehouse %s (ошибок: %s)",
             pushed, warehouse_id, len(failed))
    if failed:
        log.warning("не обновлены: %s", failed[:20])
        alert("sync_stock_to_ozon: часть товаров не обновлена",
              f"Не обновлено {len(failed)} позиций:\n{failed[:50]}")

    db.set_state("last_stock_to_ozon", "ok")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не пушить, только показать что отправил бы")
    ap.add_argument("--only", metavar="ARTICLE",
                    help="работать только с одним offer_id (для теста)")
    args = ap.parse_args()
    sys.exit(run(args.dry_run, args.only))
