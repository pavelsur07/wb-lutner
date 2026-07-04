"""
webhook_server.py — Этап 3. Flask-приёмник остатков/цен от Lutner.

- POST /webhook/stock/<секрет> — батч JSON, всегда 200 OK.
- GET  /health — для UptimeRobot / systemd.
- 404 логируются.

Запуск в бою:
  gunicorn --workers 2 --bind 127.0.0.1:8000 --timeout 60 webhook_server:app
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from lib import config, db
from lib.logging_setup import get_logger

log = get_logger("webhook")

config.require("WEBHOOK_SECRET_PATH")
db.init_db()  # идемпотентно — гарантируем схему при старте воркера

app = Flask(__name__)


def _upsert_stock(conn, item: dict) -> None:
    uuid = str(item.get("id") or "").strip()
    if not uuid:
        raise ValueError("item without id")
    conn.execute(
        """
        INSERT INTO stock
            (uuid, quantity, store_spb, store_ekb,
             price_retail, price_diller, price_mp, updated_at)
        VALUES (?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(uuid) DO UPDATE SET
            quantity=excluded.quantity,
            store_spb=excluded.store_spb, store_ekb=excluded.store_ekb,
            price_retail=excluded.price_retail,
            price_diller=excluded.price_diller,
            price_mp=excluded.price_mp,
            updated_at=datetime('now')
        """,
        (
            uuid,
            int(item.get("quantity") or 0),
            int(item.get("store_spb") or 0),
            int(item.get("store_ekb") or 0),
            item.get("price_retail"),
            item.get("price_diller"),
            item.get("price_mp"),
        ),
    )


@app.get("/health")
def health():
    return jsonify(status="ok"), 200


@app.post(f"/webhook/stock/{config.WEBHOOK_SECRET_PATH}")
def webhook_stock():
    status, error, items_count = "ok", None, 0
    body = request.get_data(cache=True)
    try:
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, list):
            raise ValueError("expected JSON array")
        items_count = len(payload)

        conn = db.get_conn()
        try:
            conn.execute("BEGIN")
            bad = 0
            for item in payload:
                try:
                    _upsert_stock(conn, item)
                except Exception as ie:  # один кривой товар не ломает батч
                    bad += 1
                    log.warning("bad item skipped: %s (%s)", item, ie)
            conn.commit()
            if bad:
                status = f"partial ({bad} skipped)"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        db.set_state("last_stock_webhook", "ok")
    except Exception as e:  # noqa: BLE001 — никогда не отдаём не-200
        status, error = "error", str(e)
        log.exception("webhook processing error")
    finally:
        try:
            c = db.get_conn()
            c.execute(
                """INSERT INTO webhook_log
                   (source_ip, forwarded_for, user_agent, body_size,
                    items_count, status, error)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    request.remote_addr,
                    request.headers.get("X-Forwarded-For"),
                    request.headers.get("User-Agent"),
                    len(body or b""),
                    items_count,
                    status,
                    error,
                ),
            )
            c.commit()
            c.close()
        except Exception:
            log.exception("failed to write webhook_log")

    # Всегда 200 — иначе Lutner ретраит и шлёт дубли.
    return jsonify(status="received"), 200


@app.errorhandler(404)
def not_found(_e):
    log.warning("404 %s %s from %s ua=%s",
                request.method, request.path, request.remote_addr,
                request.headers.get("User-Agent"))
    return jsonify(status="not found"), 404


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
