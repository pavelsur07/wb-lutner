"""
initial_mapping.py — Этап 5, одноразово. Тянет карточки WB, матчит по
баркоду с catalog, наполняет mapping. Отчёт: сматчилось / не найдено.

Запуск: venv/bin/python initial_mapping.py
"""
from __future__ import annotations

import sys

from lib import db, wb_api
from lib.logging_setup import get_logger

log = get_logger("initial_mapping")


def _iter_cards():
    cursor = None
    while True:
        data = wb_api.cards_list(cursor=cursor, limit=100)
        cards = data.get("cards", [])
        for c in cards:
            yield c
        cur = data.get("cursor") or {}
        total = cur.get("total", 0)
        if total < 100:
            break
        cursor = {"updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}


def run() -> int:
    conn = db.get_conn()
    matched = unmatched = 0
    unmatched_barcodes = []
    try:
        for card in _iter_cards():
            nm_id = card.get("nmID")
            for size in card.get("sizes", []):
                for barcode in size.get("skus", []):
                    row = conn.execute(
                        "SELECT uuid, article FROM catalog WHERE barcode=? LIMIT 1",
                        (barcode,),
                    ).fetchone()
                    if not row:
                        unmatched += 1
                        unmatched_barcodes.append(barcode)
                        continue
                    conn.execute(
                        """INSERT INTO mapping (wb_barcode, wb_nmid, lutner_uuid, lutner_article)
                           VALUES (?,?,?,?)
                           ON CONFLICT(wb_barcode) DO UPDATE SET
                             wb_nmid=excluded.wb_nmid,
                             lutner_uuid=excluded.lutner_uuid,
                             lutner_article=excluded.lutner_article""",
                        (barcode, nm_id, row["uuid"], row["article"]),
                    )
                    matched += 1
        conn.commit()
    finally:
        conn.close()

    log.info("mapping: matched=%s unmatched=%s", matched, unmatched)
    if unmatched_barcodes:
        log.warning("не найдено в catalog (%s): %s",
                    unmatched, ", ".join(unmatched_barcodes[:50]))
    print(f"Сматчилось: {matched}\nНе найдено: {unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
