"""
ozon_main.py — забирает необработанные отправления FBS из Ozon и создаёт
соответствующие заказы в Lutner.

Отличия от WB:
  - posting_number — СТРОКА (не число)
  - в одном отправлении может быть НЕСКОЛЬКО позиций -> один заказ в Lutner
    со всеми товарами
  - берём только отправления НАШЕГО склада (OZON_WAREHOUSE_ID)

Правила даты отгрузки — те же, что для WB (дедлайн 7:00 МСК, выходные -> ПН).
Комментарий: "Ozon, отгрузка DD.MM.YYYY"

Идемпотентность: PRIMARY KEY ozon_orders.posting_number.

Cron: */5 * * * * flock -n /tmp/ozon-main.lock -c 'cd /opt/wb-lutner && venv/bin/python ozon_main.py'
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from lib import config, db, lutner_api, ozon_api
from lib.logging_setup import get_logger
from lib.mailer import alert

log = get_logger("ozon_main")
MSK = timezone(timedelta(hours=3))

# Защита от повторной/ложной обработки:
#  - берём только отправления в статусах awaiting_packaging (ждёт сборки)
#    и awaiting_deliver (собрано, ждёт отгрузки); всё, что уже дальше по
#    цепочке (delivering, delivered, ...) или отменено, — товар ушёл без нас,
#    создавать заказ поздно.
#  - не создаём заказы по отправлениям старше STALE_HOURS (догон после сбоя).
PROCESSABLE_STATUSES = {"awaiting_packaging", "awaiting_deliver"}
STALE_HOURS = 24


def _shipment_date(posting: dict) -> str:
    """
    Дата отгрузки по времени отправления Ozon (in_process_at, UTC), в МСК.
    Дедлайн приёма «день в день» у Lutner — 8:00 МСК:
      до 07:00 МСК -> сегодня;  после 07:01 -> следующий день.
    Выходные переносятся на понедельник.
    """
    created = posting.get("in_process_at") or posting.get("created_at")
    dt = None
    if created:
        try:
            dt = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(MSK)
        except ValueError:
            log.warning("не разобрал дату=%r, беру текущее время", created)
    if dt is None:
        dt = datetime.now(MSK)

    ship = dt.date()
    if (dt.hour, dt.minute) > (7, 0):
        ship += timedelta(days=1)

    while ship.weekday() >= 5:  # сб/вс -> понедельник
        ship += timedelta(days=1)
    return ship.strftime("%d.%m.%Y")


def _save(conn, posting_number, status, *, lutner_id=None, comment=None,
          items=None, error=None):
    conn.execute(
        """INSERT INTO ozon_orders
               (posting_number, lutner_order_id, status, comment,
                items_json, error_json, created_at)
           VALUES (?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(posting_number) DO UPDATE SET
               lutner_order_id=excluded.lutner_order_id,
               status=excluded.status, comment=excluded.comment,
               items_json=excluded.items_json, error_json=excluded.error_json""",
        (posting_number, lutner_id, status, comment,
         json.dumps(items, ensure_ascii=False) if items is not None else None,
         json.dumps(error, ensure_ascii=False) if error is not None else None),
    )
    conn.commit()


def process_posting(posting: dict, dry_run: bool) -> str:
    pn = posting.get("posting_number")
    products = posting.get("products") or []

    conn = db.get_conn()
    try:
        if conn.execute("SELECT 1 FROM ozon_orders WHERE posting_number=?",
                        (pn,)).fetchone():
            return "skip"

        if not products:
            _save(conn, pn, "failed", error={"reason": "no products in posting"})
            alert("Ozon: отправление без товаров", f"posting {pn}")
            return "failed"

        # Собираем позиции: offer_id Ozon -> артикул Lutner + проверка остатка
        lutner_items: dict[str, int] = {}
        items_info = []
        problems = []
        for p in products:
            offer_id = (p.get("offer_id") or "").strip()
            qty = int(p.get("quantity") or 1)

            row = conn.execute(
                """SELECT m.lutner_article AS article, m.lutner_uuid AS uuid,
                          COALESCE(s.store_spb,0) AS spb, COALESCE(s.store_ekb,0) AS ekb
                   FROM ozon_mapping m
                   LEFT JOIN stock s ON s.uuid = m.lutner_uuid
                   WHERE m.offer_id = ?""",
                (offer_id,),
            ).fetchone()

            if not row:
                problems.append({"offer_id": offer_id, "reason": "no_mapping"})
                continue
            if row["spb"] < qty:
                problems.append({"offer_id": offer_id, "article": row["article"],
                                 "reason": "no_stock_spb",
                                 "spb": row["spb"], "ekb": row["ekb"], "need": qty})
                continue

            lutner_items[row["article"]] = lutner_items.get(row["article"], 0) + qty
            items_info.append({"offer_id": offer_id, "article": row["article"],
                               "qty": qty})

        if problems:
            _save(conn, pn, "failed", items=items_info, error=problems)
            alert("Ozon: заказ не создан",
                  f"posting {pn}: проблемные позиции:\n{problems}")
            return "failed"

        comment = f"Ozon, отгрузка {_shipment_date(posting)}, Отправление {pn}"

        if dry_run:
            # dry-run НЕ записывает в ozon_orders — иначе эти отправления
            # навсегда попадут в skip и реальный прогон их не обработает
            log.info("[dry-run] would create Lutner order for Ozon %s (%s)",
                     pn, lutner_items)
            return "dry_run"

        payload = {
            "profile": int(config.LUTNER_PROFILE_ID) if config.LUTNER_PROFILE_ID else None,
            "warehouse": config.LUTNER_WAREHOUSE,
            "dropshipping": True,
            "dropshipping_count": 1,
            "items": lutner_items,
            "comment": comment,
        }
        resp = lutner_api.create_order(payload)
        errors = resp.get("errors") if isinstance(resp, dict) else None
        if errors:
            _save(conn, pn, "failed", comment=comment, items=items_info, error=errors)
            alert("Lutner отклонил заказ (Ozon)", f"posting {pn}: {errors}")
            return "failed"

        lutner_id = str(resp.get("order_id") or resp.get("id") or "")
        _save(conn, pn, "created", lutner_id=lutner_id, comment=comment,
              items=items_info)
        log.info("created Lutner order %s for Ozon posting %s", lutner_id, pn)
        return "created"

    except Exception as e:  # noqa: BLE001
        try:
            _save(conn, pn, "failed", error={"exc": str(e)})
        except Exception:
            log.exception("не удалось записать ошибку по %s", pn)
        log.exception("process_posting %s failed", pn)
        alert("ozon_main: ошибка обработки", f"posting {pn}: {e}")
        return "failed"
    finally:
        conn.close()


def _posting_age_hours(posting: dict) -> float | None:
    """Возраст отправления в часах (от in_process_at/created_at, UTC)."""
    created = posting.get("in_process_at") or posting.get("created_at")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def run(dry_run: bool) -> int:
    config.require("OZON_WAREHOUSE_ID")
    our_wh = str(config.OZON_WAREHOUSE_ID)

    try:
        res = ozon_api.postings_unfulfilled(limit=100)
    except Exception as e:  # noqa: BLE001
        log.exception("postings_unfulfilled failed")
        alert("ozon_main: не удалось получить отправления", str(e))
        return 1

    all_postings = res.get("postings") or []

    # Только отправления нашего склада (склад Lutner).
    postings = [
        p for p in all_postings
        if str((p.get("delivery_method") or {}).get("warehouse_id")) == our_wh
    ]
    skipped = len(all_postings) - len(postings)
    if skipped:
        log.info("пропущено %s отправлений с других складов (наш: %s)",
                 skipped, our_wh)

    conn = db.get_conn()
    try:
        known = {r[0] for r in
                 conn.execute("SELECT posting_number FROM ozon_orders")}
    finally:
        conn.close()

    # Разбираем новые отправления до создания заказов:
    # чужой статус / слишком старые — фиксируем как пропуск и не трогаем.
    fresh, wrong_status, stale = [], [], []
    for p in postings:
        pn = p.get("posting_number")
        if not pn or pn in known:
            continue
        if (p.get("status") or "") not in PROCESSABLE_STATUSES:
            wrong_status.append(p)
            continue
        age = _posting_age_hours(p)
        if age is not None and age > STALE_HOURS:
            stale.append(p)
            continue
        fresh.append(p)

    if wrong_status or stale:
        conn = db.get_conn()
        try:
            for p in wrong_status:
                _save(conn, p.get("posting_number"), "skipped_status",
                      comment=f"status={p.get('status')}")
            for p in stale:
                _save(conn, p.get("posting_number"), "skipped_stale",
                      comment="in_process_at="
                              f"{p.get('in_process_at') or p.get('created_at')}")
        finally:
            conn.close()
        log.info("пропущено без создания заказов: по статусу=%s, старые=%s",
                 len(wrong_status), len(stale))
    if stale:
        alert("Ozon: пропущены старые отправления",
              f"{len(stale)} шт. старше {STALE_HOURS}ч — заказы НЕ создавались, "
              "проверьте вручную:\n"
              + "\n".join(str(p.get("posting_number")) for p in stale))

    stats: dict[str, int] = {"skipped_status": len(wrong_status),
                             "skipped_stale": len(stale)}
    for p in fresh:
        r = process_posting(p, dry_run)
        stats[r] = stats.get(r, 0) + 1
    log.info("cycle done: fetched=%s %s", len(postings), stats)
    db.set_state("last_cycle_ozon", datetime.now(timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="не отправлять реальный POST /order/ в Lutner")
    args = ap.parse_args()
    sys.exit(run(args.dry_run))
