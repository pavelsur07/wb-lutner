"""
lib/db.py — SQLite (WAL) + идемпотентные миграции.

Создание/миграция БД:
    venv/bin/python -m lib.db
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from lib import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    uuid            TEXT PRIMARY KEY,
    article         TEXT,
    barcode         TEXT,
    name            TEXT,
    brand           TEXT,
    package_length  REAL,
    package_width   REAL,
    package_height  REAL,
    weight          REAL,
    active          INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_catalog_article ON catalog(article);
CREATE INDEX IF NOT EXISTS idx_catalog_barcode ON catalog(barcode);

CREATE TABLE IF NOT EXISTS stock (
    uuid          TEXT PRIMARY KEY,
    quantity      INTEGER NOT NULL DEFAULT 0,
    store_spb     INTEGER NOT NULL DEFAULT 0,
    store_ekb     INTEGER NOT NULL DEFAULT 0,
    price_retail  REAL,
    price_diller  REAL,
    price_mp      REAL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mapping (
    wb_barcode     TEXT PRIMARY KEY,
    wb_nmid        INTEGER,
    lutner_uuid    TEXT,
    lutner_article TEXT
);
CREATE INDEX IF NOT EXISTS idx_mapping_uuid ON mapping(lutner_uuid);

CREATE TABLE IF NOT EXISTS orders (
    wb_order_id        INTEGER PRIMARY KEY,
    lutner_order_id    TEXT,
    status             TEXT NOT NULL,
    comment            TEXT,
    items_json         TEXT,
    error_json         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    sent_to_manager_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);

CREATE TABLE IF NOT EXISTS webhook_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ip     TEXT,
    forwarded_for TEXT,
    user_agent    TEXT,
    body_size     INTEGER,
    items_count   INTEGER,
    status        TEXT,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_created ON webhook_log(created_at);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO system_state(key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=datetime('now')",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_state(key: str, default: str | None = None) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    conn = get_conn()
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    print(f"OK  db: {config.DB_PATH}")
    print(f"    journal_mode = {mode}")
    print(f"    tables: {', '.join(tables)}")
