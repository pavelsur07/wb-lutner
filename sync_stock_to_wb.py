"""
sync_stock_to_wb.py — Этап 5. Каждые 10 мин пушит store_spb в остатки WB.

Перед отправкой спрашивает WB, какие sku склад реально знает, и шлёт остатки
ТОЛЬКО по ним. Товары без привязки к складу тихо пропускаются — ручная
чистка mapping не нужна, одна непривязанная позиция не ломает весь push.

Нет записи в stock -> amount 0. Батчами по 1000.

Cron: */10 * * * * flock -n /tmp/stock-sync.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_stock_to_wb.py'
"""
from __future__ import annotations

import argparse
import sys

from lib import config, db, wb_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_stock_to_wb")
BATCH = 1000
MIN_STOCK = int(config._get("MIN_STOCK_THRESHOLD", "3") or 3)

def _sellable(raw) -> int:
    """
    Остаток, который отдаём маркетплейсу.

    - отрицательные значения Lutner обрезаем нулём (WB отклоняет батч);
    - если на складе Lutner меньше MIN_STOCK_THRESHOLD (по умолчанию 3),
      отдаём 0 — страховка от оверселла: 1-2 шт могут уйти в другом канале
      раньше, чем мы успеем оформить отгрузку.
    """
    amount = max(0, int(raw))
    return 0 if amount < MIN_STOCK else amount



def _known_skus(all_skus: list[str]) -> set[str]:
    """Спрашивает WB, какие из sku заведены на складе. Возвращает множество известных."""
    known: set[str] = set()
    for i in range(0, len(all_skus), BATCH):
        chunk = all_skus[i:i + BATCH]
        resp = wb_api._request(
            "POST", config.WB_API_BASE,
            f"/api/v3/stocks/{config.WB_WAREHOUSE_ID}",
            json_body={"skus": chunk},
        )
        for s in (resp.get("stocks") or []):
            if s.get("sku"):
                known.add(s["sku"])
    return known


def run(dry_run: bool = False, only: str | None = None,
        no_filter: bool = False) -> int:
    config.require("WB_WAREHOUSE_ID")
    conn = db.get_conn()
    sql = """SELECT m.wb_barcode AS sku, COALESCE(s.store_spb,0) AS amount
             FROM mapping m LEFT JOIN stock s ON s.uuid = m.lutner_uuid"""
    params: tuple = ()
    if only:
        sql += " WHERE m.lutner_article = ? OR m.wb_barcode = ?"
        params = (only, only)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Lutner иногда присылает отрицательный остаток (напр. -1). WB такое
    # не принимает и отклоняет ВЕСЬ батч (IncorrectRequestBody), поэтому
    # обрезаем снизу нулём.
    stocks = [{"sku": r["sku"], "amount": _sellable(r["amount"])}
              for r in rows if r["sku"]]
    if not stocks:
        log.info("mapping пуст — нечего пушить")
        return 0

    # Фильтр привязки к складу. WB заводит товар на склад при первой удачной
    # отправке остатка, поэтому с --no-filter можно "прогреть" новые товары.
    skipped: list[str] = []
    if not no_filter:
        try:
            known = _known_skus([s["sku"] for s in stocks])
        except Exception as e:  # noqa: BLE001
            log.exception("не удалось проверить привязку sku к складу")
            alert("sync_stock_to_wb FAILED (проверка складов)", str(e))
            return 1

        skipped = [s["sku"] for s in stocks if s["sku"] not in known]
        stocks = [s for s in stocks if s["sku"] in known]
    else:
        log.info("--no-filter: шлём все %s позиций без проверки привязки", len(stocks))
    if skipped:
        log.info("пропущено (не привязаны к складу %s): %s шт %s",
                 config.WB_WAREHOUSE_ID, len(skipped), skipped[:20])
    if not stocks:
        log.info("после фильтра нечего пушить (нет привязанных sku)")
        return 0

    if dry_run:
        log.info("[dry-run] отправил бы %s позиций в WB. Первые 10: %s",
                 len(stocks), stocks[:10])
        return 0

    pushed = 0
    rejected: list[str] = []
    try:
        for i in range(0, len(stocks), BATCH):
            chunk = stocks[i:i + BATCH]
            try:
                wb_api.update_stocks(config.WB_WAREHOUSE_ID, chunk)
                pushed += len(chunk)
            except Exception as batch_err:  # noqa: BLE001
                # WB отвечает 409, если в батче есть sku, которых нет на складе.
                # Падает ВЕСЬ батч, поэтому пробуем по одному: известные пройдут,
                # новые заодно привяжутся к складу.
                log.warning("батч отклонён (%s), пробую по одному: %s позиций",
                            batch_err, len(chunk))
                for item in chunk:
                    try:
                        wb_api.update_stocks(config.WB_WAREHOUSE_ID, [item])
                        pushed += 1
                    except Exception:  # noqa: BLE001
                        rejected.append(item["sku"])
    except Exception as e:  # noqa: BLE001
        log.exception("stock push failed")
        alert("sync_stock_to_wb FAILED", f"Пуш остатков в WB упал:\n{e}")
        return 1

    if rejected:
        log.warning("WB отклонил %s sku: %s", len(rejected), rejected[:20])

    log.info("pushed %s stocks to WB warehouse %s (пропущено %s)",
             pushed, config.WB_WAREHOUSE_ID, len(skipped))
    db.set_state("last_stock_to_wb", "ok")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не пушить в WB, только показать что отправил бы")
    ap.add_argument("--only", metavar="ARTICLE",
                    help="только один товар (артикул Lutner или баркод WB)")
    ap.add_argument("--no-filter", action="store_true",
                    help="не проверять привязку к складу (для новых товаров)")
    args = ap.parse_args()
    sys.exit(run(args.dry_run, args.only, args.no_filter))
