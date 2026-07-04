"""
daily_summary.py — Этап 6. В 20:00 короткий отчёт себе на ALERT_TO.

Cron: 0 20 * * * flock -n /tmp/summary.lock -c 'cd /opt/wb-lutner && venv/bin/python daily_summary.py'
"""
from __future__ import annotations

import sys

from lib import db
from lib.logging_setup import get_logger
from lib.mailer import send_email
from lib import config

log = get_logger("daily_summary")


def run() -> int:
    conn = db.get_conn()
    by_status = conn.execute(
        """SELECT status, COUNT(*) c FROM orders
           WHERE created_at > datetime('now','-1 day')
           GROUP BY status ORDER BY c DESC"""
    ).fetchall()
    problems = conn.execute(
        """SELECT wb_order_id, status, error_json FROM orders
           WHERE created_at > datetime('now','-1 day')
             AND status IN ('failed','no_stock','ekb_only','no_mapping')
           ORDER BY wb_order_id"""
    ).fetchall()
    conn.close()

    total = sum(r["c"] for r in by_status)
    lines = [f"Заказов за сутки: {total}", ""]
    lines += [f"  {r['status']}: {r['c']}" for r in by_status]
    if problems:
        lines += ["", "Проблемные:"]
        lines += [f"  WB {p['wb_order_id']} [{p['status']}] {p['error_json'] or ''}"
                  for p in problems]

    body = "\n".join(lines)
    to = config.ALERT_TO or config.SMTP_USER or ""
    send_email(to, "[wb-lutner] Вечерний отчёт", body)
    log.info("daily summary sent: total=%s problems=%s", total, len(problems))
    return 0


if __name__ == "__main__":
    sys.exit(run())
