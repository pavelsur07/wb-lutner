"""
migrate_ozon.py — добавляет таблицы для Ozon. WB-таблицы НЕ ТРОГАЕТ.

Идемпотентно (CREATE TABLE IF NOT EXISTS) — можно запускать повторно.
Справочники catalog и stock — общие, используются обеими площадками.

Запуск: venv/bin/python migrate_ozon.py
"""
from __future__ import annotations

import sys

from lib import db

SCHEMA = """
-- Связка Ozon <-> Lutner. Ключ связки — артикул (offer_id = catalog.article).
CREATE TABLE IF NOT EXISTS ozon_mapping (
    offer_id       TEXT PRIMARY KEY,   -- артикул продавца в Ozon
    product_id     INTEGER,            -- id товара в Ozon
    sku            INTEGER,            -- sku Ozon (нужен для остатков)
    lutner_uuid    TEXT,
    lutner_article TEXT
);
CREATE INDEX IF NOT EXISTS idx_ozon_mapping_uuid ON ozon_mapping(lutner_uuid);

-- Обработанные отправления Ozon. posting_number — СТРОКА (не число, как в WB).
CREATE TABLE IF NOT EXISTS ozon_orders (
    posting_number     TEXT PRIMARY KEY,
    lutner_order_id    TEXT,
    status             TEXT NOT NULL,
    comment            TEXT,
    items_json         TEXT,           -- в отправлении может быть НЕСКОЛЬКО позиций
    error_json         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    sent_to_manager_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ozon_orders_status  ON ozon_orders(status);
CREATE INDEX IF NOT EXISTS idx_ozon_orders_created ON ozon_orders(created_at);
"""


def main() -> int:
    conn = db.get_conn()
    try:
        before = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.executescript(SCHEMA)
        conn.commit()
        after = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        print("Добавлены таблицы:", ", ".join(sorted(after - before)) or "(уже существовали)")
        print("\nВсе таблицы в БД:")
        for t in sorted(after):
            n = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            print(f"  {t:<16} строк: {n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
