"""
sync_stock_to_wb.py — Этап 5. Каждые 10 мин пушит store_spb в остатки WB.
Нет записи в stock -> 0. Батчами по 1000.

Cron: */10 * * * * flock -n /tmp/stock-sync.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_stock_to_wb.py'
"""
from __future__ import annotations

import sys

from lib import config, db, wb_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_stock_to_wb")
BATCH = 1000


def run() -> int:
    config.require("WB_WAREHOUSE_ID")
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT m.wb_barcode AS sku, COALESCE(s.store_spb,0) AS amount
           FROM mapping m LEFT JOIN stock s ON s.uuid = m.lutner_uuid"""
    ).fetchall()
    conn.close()

    stocks = [{"sku": r["sku"], "amount": int(r["amount"])} for r in rows]
    if not stocks:
        log.info("mapping пуст — нечего пушить")
        return 0

    pushed = 0
    try:
        for i in range(0, len(stocks), BATCH):
            wb_api.update_stocks(config.WB_WAREHOUSE_ID, stocks[i:i + BATCH])
            pushed += len(stocks[i:i + BATCH])
    except Exception as e:  # noqa: BLE001
        log.exception("stock push failed")
        alert("sync_stock_to_wb FAILED", f"Пуш остатков в WB упал:\n{e}")
        return 1

    log.info("pushed %s stocks to WB warehouse %s", pushed, config.WB_WAREHOUSE_ID)
    db.set_state("last_stock_to_wb", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(run())
