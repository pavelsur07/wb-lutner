"""
ozon_mapping.py — наполняет ozon_mapping. Ключ связки — АРТИКУЛ:
offer_id товара в Ozon = article в каталоге Lutner.

Идемпотентно: существующие связки обновляются, новые добавляются.
При смене offer_id у того же product_id устаревшая связка удаляется,
если новый артикул найден в Lutner, а старый отсутствует в выгрузке Ozon.
WB-таблицы не затрагиваются.

Запуск: venv/bin/python ozon_mapping.py
"""
from __future__ import annotations

import sys

from lib import db, ozon_api
from lib.logging_setup import get_logger

log = get_logger("ozon_mapping")


def run() -> int:
    conn = db.get_conn()
    matched = 0
    no_article: list[str] = []
    removed = []
    try:
        # Сначала выкачиваем всё из Ozon: пока идут HTTP-запросы, write-lock не берём.
        items = list(ozon_api.iter_products())
        current_offers = {(item.get("offer_id") or "").strip() for item in items}
        rows = []
        for item in items:
            offer_id = (item.get("offer_id") or "").strip()
            if not offer_id:
                continue

            row = conn.execute(
                "SELECT uuid, article FROM catalog WHERE article=? LIMIT 1",
                (offer_id,),
            ).fetchone()
            if not row:
                no_article.append(offer_id)
                continue

            rows.append((offer_id, item.get("product_id"), item.get("sku"),
                         row["uuid"], row["article"]))
            matched += 1

        # Одна короткая транзакция на запись.
        conn.execute("BEGIN IMMEDIATE")
        replacements = {row[1]: row[0] for row in rows if row[1]}
        for old in conn.execute("SELECT * FROM ozon_mapping").fetchall():
            if (old["product_id"] in replacements
                    and old["offer_id"] not in current_offers):
                removed.append(dict(old))
        conn.executemany("DELETE FROM ozon_mapping WHERE offer_id=?",
                         [(old["offer_id"],) for old in removed])
        conn.executemany(
            """INSERT INTO ozon_mapping
                   (offer_id, product_id, sku, lutner_uuid, lutner_article)
               VALUES (?,?,?,?,?)
               ON CONFLICT(offer_id) DO UPDATE SET
                 product_id=excluded.product_id,
                 sku=excluded.sku,
                 lutner_uuid=excluded.lutner_uuid,
                 lutner_article=excluded.lutner_article""",
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    for old in removed:
        log.info("removed renamed offer: old=%s new=%s previous_mapping=%s",
                 old["offer_id"], replacements[old["product_id"]], old)
    log.info("ozon_mapping: matched=%s no_article=%s", matched, len(no_article))
    if no_article:
        log.warning("артикул Ozon не найден в каталоге Lutner (%s): %s",
                    len(no_article), ", ".join(no_article[:50]))

    print(f"Связано по артикулу: {matched}")
    print(f"Артикул не найден в Lutner: {len(no_article)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
