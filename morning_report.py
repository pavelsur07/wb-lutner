"""
morning_report.py — Этап 6. К 7:30 МСК шлёт менеджеру Lutner состав отгрузки
за сутки + этикетки WB. Тема: "{ЮР_ЛИЦО}, {ПЛОЩАДКА}, DD.MM.YYYY".
1 письмо = 1 площадка. Проставляет orders.sent_to_manager_at.

Cron: 30 7 * * * flock -n /tmp/morning.lock -c 'cd /opt/wb-lutner && venv/bin/python morning_report.py'
"""
from __future__ import annotations

import base64
import csv
import io
import sys
from datetime import datetime, timezone, timedelta

from lib import config, db, wb_api
from lib.logging_setup import get_logger
from lib.mailer import send_email, alert

log = get_logger("morning_report")
MSK = timezone(timedelta(hours=3))


def _pending():
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT wb_order_id, lutner_order_id, comment, items_json
           FROM orders
           WHERE status='created' AND sent_to_manager_at IS NULL
             AND created_at > datetime('now','-1 day')
           ORDER BY wb_order_id"""
    ).fetchall()
    conn.close()
    return rows


def _composition_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["WB order", "Lutner order", "Состав", "Комментарий"])
    for r in rows:
        w.writerow([r["wb_order_id"], r["lutner_order_id"] or "",
                    r["items_json"] or "", r["comment"] or ""])
    return buf.getvalue().encode("utf-8-sig")


def _stickers_pdf(order_ids) -> bytes | None:
    """Собирает этикетки WB в один PDF. Нужен reportlab (мягкий импорт)."""
    try:
        resp = wb_api.stickers(order_ids, sticker_type="png", width=58, height=40)
    except Exception as e:  # noqa: BLE001
        log.warning("stickers fetch failed: %s", e)
        return None
    stickers = resp.get("stickers", []) if isinstance(resp, dict) else []
    if not stickers:
        return None
    try:
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except ImportError:
        log.warning("reportlab не установлен — этикетки не приложены")
        return None

    out = io.BytesIO()
    c = canvas.Canvas(out)
    for s in stickers:
        raw = s.get("file") or s.get("image")
        if not raw:
            continue
        img = ImageReader(io.BytesIO(base64.b64decode(raw)))
        c.setPageSize((165, 115))  # ~58x40mm в пунктах
        c.drawImage(img, 5, 5, width=155, height=105, preserveAspectRatio=True)
        c.showPage()
    c.save()
    return out.getvalue()


def run() -> int:
    config.require("MANAGER_TO")
    rows = _pending()
    if not rows:
        log.info("нет заказов к отгрузке — письмо не отправляем")
        return 0

    date_ru = datetime.now(MSK).strftime("%d.%m.%Y")
    subject = f"{config.LEGAL_ENTITY}, {config.MARKETPLACE_NAME}, {date_ru}"
    body = (
        f"Состав отгрузки {config.MARKETPLACE_NAME} за {date_ru}.\n"
        f"Заказов: {len(rows)}.\n\n"
        f"Детали — в приложенном CSV. Этикетки — в PDF."
    )

    order_ids = [r["wb_order_id"] for r in rows]
    attachments = [("sostav.csv", _composition_csv(rows))]
    pdf = _stickers_pdf(order_ids)
    if pdf:
        attachments.append(("stickers.pdf", pdf))

    ok = send_email(config.MANAGER_TO, subject, body, attachments)
    if not ok:
        alert("morning_report: письмо НЕ отправлено", subject)
        return 1

    conn = db.get_conn()
    conn.executemany(
        "UPDATE orders SET sent_to_manager_at=datetime('now') WHERE wb_order_id=?",
        [(oid,) for oid in order_ids],
    )
    conn.commit()
    conn.close()
    log.info("morning report sent: %s orders, subject='%s'", len(rows), subject)
    return 0


if __name__ == "__main__":
    sys.exit(run())
