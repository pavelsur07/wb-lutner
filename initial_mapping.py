"""
initial_mapping.py — наполняет mapping. Ключ связки — АРТИКУЛ:
берём vendorCode карточки WB (= article Lutner), находим товар в catalog,
и записываем в mapping баркод карточки WB (sku) + uuid Lutner.

Так остатки уедут в WB по правильному sku (WB понимает только баркод),
а связь строится по артикулу, как задумано.

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


def _first_barcode(card: dict) -> str | None:
    """Один товар = один баркод: берём первый sku из первого размера."""
    for size in card.get("sizes", []):
        for sku in size.get("skus", []):
            if sku:
                return sku
    return None


def run() -> int:
    conn = db.get_conn()
    matched = 0
    no_article = []   # карточки WB без пары в каталоге Lutner
    no_barcode = []   # карточки WB без баркода (нечего слать в WB)
    try:
        for card in _iter_cards():
            vendor_code = (card.get("vendorCode") or "").strip()
            nm_id = card.get("nmID")
            if not vendor_code:
                continue

            row = conn.execute(
                "SELECT uuid, article FROM catalog WHERE article=? LIMIT 1",
                (vendor_code,),
            ).fetchone()
            if not row:
                no_article.append(vendor_code)
                continue

            wb_barcode = _first_barcode(card)
            if not wb_barcode:
                no_barcode.append(vendor_code)
                continue

            conn.execute(
                """INSERT INTO mapping (wb_barcode, wb_nmid, lutner_uuid, lutner_article)
                   VALUES (?,?,?,?)
                   ON CONFLICT(wb_barcode) DO UPDATE SET
                     wb_nmid=excluded.wb_nmid,
                     lutner_uuid=excluded.lutner_uuid,
                     lutner_article=excluded.lutner_article""",
                (wb_barcode, nm_id, row["uuid"], row["article"]),
            )
            matched += 1
        conn.commit()
    finally:
        conn.close()

    log.info("mapping: matched=%s no_article=%s no_barcode=%s",
             matched, len(no_article), len(no_barcode))
    if no_article:
        log.warning("артикул WB не найден в каталоге Lutner (%s): %s",
                    len(no_article), ", ".join(no_article[:50]))
    if no_barcode:
        log.warning("у карточки WB нет баркода (%s): %s",
                    len(no_barcode), ", ".join(no_barcode[:50]))

    print(f"Связано по артикулу: {matched}")
    print(f"Артикул не найден в Lutner: {len(no_article)}")
    print(f"Карточка WB без баркода: {len(no_barcode)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
