"""
main.py — Этап 5. Забирает новые FBS-задания WB и создаёт заказы в Lutner.

Идемпотентность: PRIMARY KEY orders.wb_order_id. Уже обработанный — пропускаем.
Флаг --dry-run: делает всё, кроме реального POST /order/ в Lutner.

Cron: */5 * * * * flock -n /tmp/main.lock -c 'cd /opt/wb-lutner && venv/bin/python main.py'
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

from lib import config, db, wb_api, lutner_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("main")
MSK = timezone(timedelta(hours=3))


def _shipment_date() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def _lookup(conn, barcode: str):
    """barcode WB -> (lutner_article, lutner_uuid, store_spb, store_ekb) или None."""
    row = conn.execute(
        """
        SELECT m.lutner_article AS article, m.lutner_uuid AS uuid,
               COALESCE(s.store_spb,0) AS spb, COALESCE(s.store_ekb,0) AS ekb
        FROM mapping m
        LEFT JOIN stock s ON s.uuid = m.lutner_uuid
        WHERE m.wb_barcode = ?
        """,
        (barcode,),
    ).fetchone()
    return row


def _save(conn, wb_id, status, *, lutner_id=None, comment=None, items=None, error=None):
    conn.execute(
        """
        INSERT INTO orders
            (wb_order_id, lutner_order_id, status, comment, items_json, error_json, created_at)
        VALUES (?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(wb_order_id) DO UPDATE SET
            lutner_order_id=excluded.lutner_order_id, status=excluded.status,
            comment=excluded.comment, items_json=excluded.items_json,
            error_json=excluded.error_json
        """,
        (
            wb_id, lutner_id, status, comment,
            json.dumps(items, ensure_ascii=False) if items is not None else None,
            json.dumps(error, ensure_ascii=False) if error is not None else None,
        ),
    )
    conn.commit()


def process_order(order: dict, dry_run: bool) -> str:
    wb_id = order["id"]
    skus = order.get("skus") or []
    barcode = skus[0] if skus else None
    qty = 1  # WB FBS: как правило 1 позиция/заказ

    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM orders WHERE wb_order_id=?", (wb_id,)).fetchone():
            return "skip"

        if not barcode:
            _save(conn, wb_id, "failed", error={"reason": "no sku in WB order"})
            alert("WB order без баркода", f"order {wb_id}: {order}")
            return "failed"

        m = _lookup(conn, barcode)
        if not m:
            _save(conn, wb_id, "no_mapping",
                  items={"barcode": barcode}, error={"reason": "barcode not in mapping"})
            alert("Неизвестный баркод", f"order {wb_id}, barcode {barcode} нет в mapping")
            return "no_mapping"

        comment = (
            f"{config.MARKETPLACE_NAME}, отгрузка {_shipment_date()}, "
            f"WB order {wb_id}"
        )
        items = {"article": m["article"], "qty": qty, "barcode": barcode}

        if m["spb"] < qty:
            reason = "ekb_only" if m["ekb"] >= qty else "no_stock"
            _save(conn, wb_id, reason, items=items,
                  error={"spb": m["spb"], "ekb": m["ekb"], "need": qty})
            where = "только в EKB" if reason == "ekb_only" else "нигде"
            alert(f"Нет остатка в SPB ({reason})",
                  f"order {wb_id}, {m['article']}: SPB={m['spb']} EKB={m['ekb']}, "
                  f"нужно {qty}. Наличие: {where}. Ручной разбор.")
            return reason

        if dry_run:
            _save(conn, wb_id, "dry_run", comment=comment, items=items)
            log.info("[dry-run] would create Lutner order for WB %s (%s x%s)",
                     wb_id, m["article"], qty)
            return "dry_run"

        payload = {
            "profile": int(config.LUTNER_PROFILE_ID) if config.LUTNER_PROFILE_ID else None,
            "warehouse": config.LUTNER_WAREHOUSE,
            "dropshipping": True,
            "dropshipping_count": 1,
            "items": {m["article"]: qty},
            "comment": comment,
        }
        resp = lutner_api.create_order(payload)
        errors = resp.get("errors") if isinstance(resp, dict) else None
        if errors:
            _save(conn, wb_id, "failed", comment=comment, items=items, error=errors)
            alert("Lutner отклонил заказ", f"order {wb_id}: {errors}")
            return "failed"

        lutner_id = str(resp.get("order_id") or resp.get("id") or "")
        _save(conn, wb_id, "created", lutner_id=lutner_id, comment=comment, items=items)
        log.info("created Lutner order %s for WB %s", lutner_id, wb_id)
        return "created"

    except Exception as e:  # noqa: BLE001
        try:
            _save(conn, wb_id, "failed", error={"exc": str(e)})
        except Exception:
            log.exception("failed to persist error for %s", wb_id)
        log.exception("process_order %s failed", wb_id)
        alert("main.py: ошибка обработки заказа", f"order {wb_id}: {e}")
        return "failed"
    finally:
        conn.close()


def run(dry_run: bool) -> int:
    try:
        data = wb_api.orders_new()
    except Exception as e:  # noqa: BLE001
        log.exception("orders_new failed")
        alert("main.py: WB orders_new упал", str(e))
        return 1

    orders = data.get("orders") or []
    stats: dict[str, int] = {}
    for o in orders:
        r = process_order(o, dry_run)
        stats[r] = stats.get(r, 0) + 1
    log.info("cycle done: fetched=%s %s", len(orders), stats)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не отправлять реальный POST /order/ в Lutner")
    args = ap.parse_args()
    sys.exit(run(args.dry_run))
