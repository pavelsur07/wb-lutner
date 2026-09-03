"""
daily_summary.py — вечерний отчёт на ALERT_TO: сводка по WB и Ozon за сутки.

Cron: 0 20 * * * flock -n /tmp/summary.lock -c 'cd /opt/wb-lutner && venv/bin/python daily_summary.py'
"""
from __future__ import annotations

import sys

from lib import config, db
from lib.logging_setup import get_logger
from lib.mailer import send_email

log = get_logger("daily_summary")

PROBLEM_STATUSES = ("failed", "no_stock", "ekb_only", "no_mapping",
                    "pending_supply")


def _section(conn, table: str, id_col: str, title: str):
    by_status = conn.execute(
        f"""SELECT status, COUNT(*) c FROM {table}
            WHERE created_at > datetime('now','-1 day')
            GROUP BY status ORDER BY c DESC"""
    ).fetchall()
    placeholders = ",".join("?" * len(PROBLEM_STATUSES))
    problems = conn.execute(
        f"""SELECT {id_col} AS oid, status, error_json FROM {table}
            WHERE created_at > datetime('now','-1 day')
              AND status IN ({placeholders})
            ORDER BY {id_col}""",
        PROBLEM_STATUSES,
    ).fetchall()

    total = sum(r["c"] for r in by_status)
    lines = [f"=== {title} ===", f"Заказов за сутки: {total}"]
    lines += [f"  {r['status']}: {r['c']}" for r in by_status] or ["  (нет)"]
    if problems:
        lines += ["", "  Проблемные:"]
        lines += [f"    {p['oid']} [{p['status']}] {p['error_json'] or ''}"
                  for p in problems]
    lines.append("")
    return lines, total, len(problems)


def _health(conn) -> list[str]:
    lines = ["=== Здоровье системы ==="]
    for key, label in (
        ("last_stock_webhook", "Последний webhook Lutner"),
        ("last_catalog_sync", "Синхронизация каталога"),
        ("last_stock_to_wb", "Остатки -> WB"),
        ("last_stock_to_ozon", "Остатки -> Ozon"),
    ):
        row = conn.execute(
            "SELECT value, updated_at FROM system_state WHERE key=?", (key,)
        ).fetchone()
        if row:
            lines.append(f"  {label}: {row['value']} ({row['updated_at']})")
        else:
            lines.append(f"  {label}: нет данных")

    cat = conn.execute("SELECT COUNT(*) c FROM catalog").fetchone()["c"]
    wb_map = conn.execute("SELECT COUNT(*) c FROM mapping").fetchone()["c"]
    oz_map = conn.execute("SELECT COUNT(*) c FROM ozon_mapping").fetchone()["c"]
    lines += [
        f"  Каталог Lutner: {cat} товаров",
        f"  Связок WB: {wb_map} | Ozon: {oz_map}",
        "",
    ]
    return lines


def run() -> int:
    conn = db.get_conn()
    try:
        wb_lines, wb_total, wb_prob = _section(
            conn, "orders", "wb_order_id", "Wildberries")
        oz_lines, oz_total, oz_prob = _section(
            conn, "ozon_orders", "posting_number", "Ozon")
        health = _health(conn)
    finally:
        conn.close()

    body = "\n".join(wb_lines + oz_lines + health)
    subject = (f"[wb-lutner] Отчёт: WB {wb_total}, Ozon {oz_total}"
               + (f", проблем {wb_prob + oz_prob}" if wb_prob + oz_prob else ""))

    to = config.ALERT_TO or config.SMTP_USER or ""
    send_email(to, subject, body)
    log.info("daily summary sent: wb=%s ozon=%s problems=%s",
             wb_total, oz_total, wb_prob + oz_prob)
    return 0


if __name__ == "__main__":
    sys.exit(run())
