"""
check_heartbeat.py — Этап 4. Алерт, если stock-webhook молчит >15 минут.

Cron: */10 * * * * flock -n /tmp/heartbeat.lock -c 'cd /opt/wb-lutner && venv/bin/python check_heartbeat.py'
"""
from __future__ import annotations

import sys

from lib import db
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("heartbeat")
THRESHOLD_MIN = 15


def run() -> int:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT updated_at, (julianday('now') - julianday(updated_at)) * 24 * 60 AS age_min "
        "FROM system_state WHERE key='last_stock_webhook'"
    ).fetchone()
    conn.close()

    if not row:
        log.warning("нет ни одного webhook")
        alert("Webhook НЕ ПРИХОДИЛ НИ РАЗУ", "system_state.last_stock_webhook отсутствует")
        return 1

    age = row["age_min"]
    if age is not None and age > THRESHOLD_MIN:
        log.warning("webhook молчит %.1f мин", age)
        alert("Webhook молчит",
              f"Последний webhook от Lutner был {age:.0f} мин назад "
              f"(порог {THRESHOLD_MIN}). Проверь nginx/gunicorn/сеть Lutner.")
        return 1

    log.info("heartbeat ok (age=%.1f min)", age or 0)
    return 0


if __name__ == "__main__":
    sys.exit(run())
