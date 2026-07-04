"""
sync_catalog.py — Этап 2. Скачивает полный прайс Lutner (CSV, UTF-8) и
обновляет справочник `catalog` (штрих-коды, габариты, вес).

Cron: 0 3 * * *  flock -n /tmp/catalog.lock -c 'cd /opt/wb-lutner && venv/bin/python sync_catalog.py'
"""
from __future__ import annotations

import csv
import io
import sys

import requests

from lib import config, db
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("sync_catalog")

# CSV поле -> колонка catalog
FIELD = {
    "uuid": "IE_XML_ID",
    "article": "IP_PROP96",
    "barcode": "IP_PROP95",
    "name": "IE_NAME",
    "brand": "IP_PROP114",
    "package_length": "IP_PROP258",
    "package_width": "IP_PROP259",
    "package_height": "IP_PROP260",
    "weight": "IP_PROP106",
    "discontinued": "IP_PROP140",  # пусто = активен
}


def _to_float(v):
    if v is None:
        return None
    v = str(v).strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _download() -> str:
    config.require("LUTNER_CATALOG_FULL_URL")
    resp = requests.get(config.LUTNER_CATALOG_FULL_URL, timeout=300)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def sync() -> int:
    text = _download()
    reader = csv.DictReader(io.StringIO(text), delimiter=config.CSV_DELIMITER)
    rows = list(reader)

    # Защита от пустого CSV — не обнуляем справочник.
    if not rows:
        raise RuntimeError("CSV пуст (0 строк) — прайс не обновлён, справочник цел")

    conn = db.get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE catalog SET active = 0")
        n = 0
        for row in rows:
            uuid = (row.get(FIELD["uuid"]) or "").strip()
            if not uuid:
                continue
            active = 0 if (row.get(FIELD["discontinued"]) or "").strip() else 1
            conn.execute(
                """
                INSERT INTO catalog
                    (uuid, article, barcode, name, brand,
                     package_length, package_width, package_height, weight,
                     active, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
                ON CONFLICT(uuid) DO UPDATE SET
                    article=excluded.article, barcode=excluded.barcode,
                    name=excluded.name, brand=excluded.brand,
                    package_length=excluded.package_length,
                    package_width=excluded.package_width,
                    package_height=excluded.package_height,
                    weight=excluded.weight, active=excluded.active,
                    updated_at=datetime('now')
                """,
                (
                    uuid,
                    (row.get(FIELD["article"]) or "").strip(),
                    (row.get(FIELD["barcode"]) or "").strip(),
                    (row.get(FIELD["name"]) or "").strip(),
                    (row.get(FIELD["brand"]) or "").strip(),
                    _to_float(row.get(FIELD["package_length"])),
                    _to_float(row.get(FIELD["package_width"])),
                    _to_float(row.get(FIELD["package_height"])),
                    _to_float(row.get(FIELD["weight"])),
                    active,
                ),
            )
            n += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n


def main() -> int:
    try:
        n = sync()
    except Exception as e:  # noqa: BLE001
        log.exception("catalog sync failed")
        alert("sync_catalog FAILED", f"Синхронизация каталога упала:\n{e}")
        db.set_state("last_catalog_sync", f"error: {e}")
        return 1

    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM catalog").fetchone()["c"]
    with_bc = conn.execute(
        "SELECT COUNT(*) c FROM catalog WHERE barcode != ''"
    ).fetchone()["c"]
    active = conn.execute("SELECT COUNT(*) c FROM catalog WHERE active=1").fetchone()["c"]
    conn.close()

    log.info("catalog synced: rows=%s total=%s barcodes=%s active=%s",
             n, total, with_bc, active)
    db.set_state("last_catalog_sync", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
